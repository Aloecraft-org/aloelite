//! The plug-in under a real Extism host: what `cargo test` cannot reach.
//!
//! `aloelite-extism/tests/plugin.rs` drives everything on the plug-in's own
//! side of the ABI. What is left is the ABI itself and the host's part of the
//! bargain — WASI enabled, a path granted, a memory cap big enough for
//! Argon2id — and none of that exists until something loads the module. This
//! is that something, and it is a test: it asserts, and it exits non-zero.
//!
//! Usage: `harness <plugin.wasm> <scratch-dir>`.

use std::path::PathBuf;

use extism::{Manifest, Plugin, Wasm};
use rmpv::Value as Mp;

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// Plug-in memory, in 64 KiB pages. Argon2id at the format's pinned factors
/// wants 64 MiB of it; 96 MiB is the floor that leaves room for the engine.
const PAGES: u32 = 2048;

/// A cap that is not enough, to prove the failure a host would otherwise
/// meet in production and misread as a key problem.
const TOO_FEW_PAGES: u32 = 1024;

fn main() {
    let mut args = std::env::args().skip(1);
    let wasm = args.next().expect("usage: harness <plugin.wasm> <dir>");
    let dir = args.next().expect("usage: harness <plugin.wasm> <dir>");

    let volume = PathBuf::from(&dir).join("harness.fs");
    let _ = std::fs::remove_file(&volume);
    let manifest = |pages: u32| {
        Manifest::new([Wasm::file(&wasm)])
            .with_allowed_path(dir.clone(), "/vol")
            .with_memory_max(pages)
    };

    // -- the ABI, and the host's part of the bargain ----------------------
    let mut p = Plugin::new(manifest(PAGES), [], true).expect("the module loads with WASI");
    let names = ok(&mut p, "fs_operations", &Mp::Nil);
    assert!(
        names.as_array().expect("an array").len() >= 50,
        "operations answers the shared table"
    );

    ok(
        &mut p,
        "fs_open",
        &map(&[("path", text("/vol/harness.fs"))]),
    );
    let vol = call(
        &mut p,
        "create_volume",
        map(&[
            ("name", text("harness")),
            ("pin", Mp::Binary(b"hunter2".to_vec())),
            ("enc_mode", text("convergent")),
        ]),
    )
    .expect("create_volume");
    let id = field(&vol, "id");
    let m = call(
        &mut p,
        "mount",
        map(&[
            ("volume", id.clone()),
            ("pin", Mp::Binary(b"hunter2".to_vec())),
        ]),
    )
    .expect("mount");
    let m = m.as_str().expect("a mount id").to_owned();

    // -- the types the wire promises --------------------------------------
    call(
        &mut p,
        "create_container",
        map(&[("mount", text(&m)), ("path", text("/notes"))]),
    )
    .expect("create_container");
    let node = call(
        &mut p,
        "create_entry",
        map(&[
            ("mount", text(&m)),
            ("path", text("/notes/hello.txt")),
            ("data", Mp::Binary(b"the filesystem is one file".to_vec())),
        ]),
    )
    .expect("create_entry");

    let back = call(
        &mut p,
        "read_all",
        map(&[("mount", text(&m)), ("path", text("/notes/hello.txt"))]),
    )
    .expect("read_all");
    assert_eq!(
        back,
        Mp::Binary(b"the filesystem is one file".to_vec()),
        "bytes cross as bin"
    );

    const TS: i64 = 1_789_243_772_929_739_743;
    call(
        &mut p,
        "set_mtime",
        map(&[("mount", text(&m)), ("node", node), ("ts_ns", Mp::from(TS))]),
    )
    .expect("set_mtime");
    let info = call(
        &mut p,
        "stat",
        map(&[("mount", text(&m)), ("path", text("/notes/hello.txt"))]),
    )
    .expect("stat");
    assert_eq!(
        field(&info, "modified_at"),
        Mp::from(TS),
        "nanoseconds cross exactly"
    );

    // Unmount before closing: a mount row is durable, so an rw mount left
    // behind would meet the next instance as `mount_conflict` (D-4) rather
    // than as the key check this is about to make.
    call(&mut p, "unmount", map(&[("mount", text(&m))])).expect("unmount");
    ok(&mut p, "fs_close", &Mp::Nil);
    let bytes = std::fs::metadata(&volume)
        .expect("the volume is a host file")
        .len();

    // -- it is a file, and it outlives the instance ------------------------
    let mut two = Plugin::new(manifest(PAGES), [], true).unwrap();
    ok(
        &mut two,
        "fs_open",
        &map(&[("path", text("/vol/harness.fs"))]),
    );
    let vols = call(&mut two, "list_volumes", Mp::Nil).expect("list_volumes");
    let id2 = field(&vols.as_array().unwrap()[0], "id");
    assert_eq!(id2, id, "the same volume, read back by another instance");

    let wrong = call(
        &mut two,
        "mount",
        map(&[
            ("volume", id2.clone()),
            ("pin", Mp::Binary(b"wrong".to_vec())),
        ]),
    );
    assert_eq!(
        wrong.unwrap_err().0,
        "bad_key",
        "the engine refuses the PIN, not the host"
    );

    let m2 = call(
        &mut two,
        "mount",
        map(&[("volume", id2), ("pin", Mp::Binary(b"hunter2".to_vec()))]),
    )
    .expect("mount");
    let again = call(
        &mut two,
        "read_all",
        map(&[("mount", m2), ("path", text("/notes/hello.txt"))]),
    )
    .expect("read_all");
    assert_eq!(again, Mp::Binary(b"the filesystem is one file".to_vec()));

    // -- the sandbox -------------------------------------------------------
    let mut three = Plugin::new(manifest(PAGES), [], true).unwrap();
    let escape = envelope(
        &mut three,
        "fs_open",
        &map(&[("path", text("/etc/passwd"))]),
    );
    assert!(
        escape.is_err(),
        "the manifest grants one directory and no other"
    );

    // -- the other storage shape, with no filesystem at all ----------------
    // A manifest that grants nothing: the plug-in can reach no host path, and
    // the volume is bytes the host handed it and takes back.
    let sealed = || Manifest::new([Wasm::file(&wasm)]).with_memory_max(PAGES);
    let mut s1 = Plugin::new(sealed(), [], true).unwrap();
    let denied = envelope(
        &mut s1,
        "fs_open",
        &map(&[("path", text("/vol/harness.fs"))]),
    );
    assert!(denied.is_err(), "nothing is granted, so nothing opens");

    ok(
        &mut s1,
        "fs_open_image",
        &map(&[("image", Mp::Binary(Vec::new()))]),
    );
    let vol =
        call(&mut s1, "create_volume", map(&[("name", text("sealed"))])).expect("create_volume");
    let ms = call(&mut s1, "mount", map(&[("volume", field(&vol, "id"))])).expect("mount");
    let ms = ms.as_str().expect("a mount id").to_owned();
    call(
        &mut s1,
        "create_entry",
        map(&[
            ("mount", text(&ms)),
            ("path", text("/kept")),
            ("data", Mp::Binary(b"no path needed".to_vec())),
        ]),
    )
    .expect("create_entry");
    let image = match ok(&mut s1, "fs_snapshot", &Mp::Nil) {
        Mp::Binary(b) => b,
        other => panic!("a snapshot is bin, got {other}"),
    };
    assert_eq!(
        &image[..15],
        b"SQLite format 3",
        "the snapshot is the database"
    );

    // A second instance, also granted nothing, built from those bytes alone.
    let mut s2 = Plugin::new(sealed(), [], true).unwrap();
    ok(
        &mut s2,
        "fs_open_image",
        &map(&[("image", Mp::Binary(image.clone()))]),
    );
    let kept = call(
        &mut s2,
        "read_all",
        map(&[("mount", text(&ms)), ("path", text("/kept"))]),
    )
    .expect("read_all");
    assert_eq!(kept, Mp::Binary(b"no path needed".to_vec()));

    // -- the memory floor --------------------------------------------------
    let mut small = Plugin::new(manifest(TOO_FEW_PAGES), [], true).unwrap();
    ok(&mut small, "fs_open_memory", &Mp::Nil);
    let oom = small.call::<&[u8], &[u8]>(
        "fs_call",
        &encode(&map(&[
            ("op", text("create_volume")),
            ("args", map(&[("pin", Mp::Binary(b"hunter2".to_vec()))])),
        ])),
    );
    assert!(
        oom.is_err(),
        "at {} MiB Argon2id has nowhere to run; a host that caps this low must learn it here",
        TOO_FEW_PAGES as u64 * 64 / 1024
    );

    println!(
        "ok  ({} operations, volume {bytes} bytes on a granted path, {} bytes as an \
         image with none granted, {} MiB cap; {} MiB is not enough)",
        names.as_array().unwrap().len(),
        image.len(),
        PAGES as u64 * 64 / 1024,
        TOO_FEW_PAGES as u64 * 64 / 1024,
    );
}

// ---------------------------------------------------------------------------
// depth: the envelope, from the host's side
// ---------------------------------------------------------------------------

/// One `call`, unwrapped: `Ok` is the result, `Err` is `(code, message)`.
fn call(p: &mut Plugin, op: &str, args: Mp) -> Result<Mp, (String, String)> {
    envelope(p, "fs_call", &map(&[("op", text(op)), ("args", args)]))
}

/// An export that must succeed.
fn ok(p: &mut Plugin, export: &str, input: &Mp) -> Mp {
    envelope(p, export, input).unwrap_or_else(|(c, m)| panic!("{export}: {c}: {m}"))
}

fn envelope(p: &mut Plugin, export: &str, input: &Mp) -> Result<Mp, (String, String)> {
    let out = p
        .call::<&[u8], &[u8]>(export, &encode(input))
        .unwrap_or_else(|e| panic!("{export} trapped: {e:?}"));
    let v = rmpv::decode::read_value(&mut &out[..]).expect("the reply is MessagePack");
    let m = v.as_map().expect("the reply is a map");
    assert_eq!(m.len(), 1, "the reply carries exactly one of ok / error");
    match m[0].0.as_str() {
        Some("ok") => Ok(m[0].1.clone()),
        Some("error") => Err((
            field(&m[0].1, "code").as_str().unwrap().to_owned(),
            field(&m[0].1, "message").as_str().unwrap().to_owned(),
        )),
        other => panic!("unexpected reply key {other:?}"),
    }
}

fn map(fields: &[(&str, Mp)]) -> Mp {
    Mp::Map(fields.iter().map(|(k, v)| (text(k), v.clone())).collect())
}

fn text(s: &str) -> Mp {
    Mp::String(s.into())
}

fn encode(v: &Mp) -> Vec<u8> {
    let mut out = Vec::new();
    rmpv::encode::write_value(&mut out, v).unwrap();
    out
}

fn field(v: &Mp, name: &str) -> Mp {
    v.as_map()
        .unwrap_or_else(|| panic!("not a record: {v}"))
        .iter()
        .find(|(k, _)| k.as_str() == Some(name))
        .unwrap_or_else(|| panic!("no field {name} in {v}"))
        .1
        .clone()
}
