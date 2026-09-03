//! End to end through the built binary, against a real file: the port of
//! `tests/test_cli.py`, case for case, plus the no-verb create/status and
//! the stdin shortcuts. Fixtures are made with the engine directly, as the
//! reference's are.

use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicU32, Ordering};

use aloelite_core::crypto::EncMode;
use aloelite_core::ops::{self, MountOptions};

static SEQ: AtomicU32 = AtomicU32::new(0);

// ---------------------------------------------------------------------------
// harness
// ---------------------------------------------------------------------------

struct Out {
    code: i32,
    out: String,
    err: String,
    bytes: Vec<u8>,
}

fn scratch(label: &str) -> PathBuf {
    let n = SEQ.fetch_add(1, Ordering::Relaxed);
    let dir = std::env::temp_dir().join(format!("aloelite-cli-{label}-{}-{n}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

/// A file with one volume `vol`, like the reference's `fsfile` fixture.
fn fsfile(dir: &Path) -> String {
    let path = dir.join("t.fs");
    let mut db = aloelite_store::file::open(&path).unwrap();
    ops::create_volume(&mut db, Some("vol"), 1 << 20, None, EncMode::Convergent).unwrap();
    db.close().unwrap();
    path.to_string_lossy().into_owned()
}

fn run_with(env: &[(&str, &str)], stdin: Option<&[u8]>, args: &[&str]) -> Out {
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_aloelite"));
    cmd.args(args)
        .env_remove("ALOELITE_FILE")
        .stdin(if stdin.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    for (k, v) in env {
        cmd.env(k, v);
    }
    let mut child = cmd.spawn().unwrap();
    if let Some(data) = stdin {
        child.stdin.take().unwrap().write_all(data).unwrap();
    }
    let output = child.wait_with_output().unwrap();
    Out {
        code: output.status.code().unwrap_or(-1),
        out: String::from_utf8_lossy(&output.stdout).into_owned(),
        err: String::from_utf8_lossy(&output.stderr).into_owned(),
        bytes: output.stdout,
    }
}

fn run(args: &[&str]) -> Out {
    run_with(&[], None, args)
}

/// A local sample tree: nesting, an empty dir, a big file, an empty file.
fn sample_tree(root: &Path) -> PathBuf {
    std::fs::create_dir_all(root.join("sub/deep")).unwrap();
    std::fs::create_dir_all(root.join("empty")).unwrap();
    std::fs::write(root.join("a.txt"), b"alpha").unwrap();
    std::fs::write(root.join("sub/big.bin"), vec![b'x'; 3 << 20]).unwrap(); // > CHUNK: streams
    std::fs::write(root.join("sub/deep/zero"), b"").unwrap();
    root.to_owned()
}

// ---------------------------------------------------------------------------
// the reference's cases
// ---------------------------------------------------------------------------

#[test]
fn roundtrip() {
    let dir = scratch("roundtrip");
    let f = fsfile(&dir);
    let src = dir.join("in.txt");
    std::fs::write(&src, b"hello cli").unwrap();
    assert_eq!(
        run(&["-f", &f, "put", src.to_str().unwrap(), "/a.txt"]).code,
        0
    );
    assert_eq!(run(&["-f", &f, "mkdir", "-p", "/d/e"]).code, 0);
    assert_eq!(run(&["-f", &f, "mv", "/a.txt", "/d/a.txt"]).code, 0);
    let out = dir.join("out.txt");
    assert_eq!(
        run(&["-f", &f, "get", "/d/a.txt", out.to_str().unwrap()]).code,
        0
    );
    assert_eq!(std::fs::read(&out).unwrap(), b"hello cli");
    let ls = run(&["-f", &f, "ls", "/d"]);
    assert_eq!(ls.code, 0);
    assert!(ls.out.contains("/d/a.txt"), "{}", ls.out);
    assert_eq!(run(&["-f", &f, "rm", "-r", "/d"]).code, 0);
}

#[test]
fn volume_selection() {
    let dir = scratch("volsel");
    let f = fsfile(&dir);
    let mut db = aloelite_store::file::open(&f).unwrap();
    let second =
        ops::create_volume(&mut db, Some("second"), 1 << 20, None, EncMode::Convergent).unwrap();
    db.close().unwrap();
    assert_eq!(run(&["-f", &f, "ls"]).code, 1, "refuses to guess");
    assert_eq!(run(&["-f", &f, "-v", "second", "ls"]).code, 0);
    let vid = second.id.0;
    assert_eq!(run(&["-f", &f, "-v", &vid, "ls"]).code, 0, "dashed id");
    assert_eq!(
        run(&["-f", &f, "-v", &vid.replace('-', ""), "ls"]).code,
        0,
        "bare hex"
    );
    assert_eq!(run(&["-f", &f, "-v", "nope", "ls"]).code, 1);
}

#[test]
fn encrypted_pin_env() {
    let dir = scratch("enc");
    let plain = fsfile(&dir);
    let enc = dir.join("enc.fs");
    let mut db = aloelite_store::file::open(&enc).unwrap();
    ops::create_volume(
        &mut db,
        Some("vault"),
        1 << 20,
        Some(b"s3cret"),
        EncMode::Convergent,
    )
    .unwrap();
    db.close().unwrap();
    let enc = enc.to_str().unwrap();
    let env = [("ALOE_PIN", "s3cret")];
    assert_eq!(
        run_with(
            &env,
            None,
            &["-f", enc, "--pin-env", "ALOE_PIN", "mkdir", "/d"]
        )
        .code,
        0
    );
    let wrong = run(&["-f", enc, "--pin", "wrong", "ls"]);
    assert_eq!(wrong.code, 1, "BadKey -> exit 1");
    assert!(wrong.err.contains("wrong PIN"), "{}", wrong.err);
    // a pin flag against an UNencrypted volume: early, pointed error
    let early = run_with(&env, None, &["-f", &plain, "--pin-env", "ALOE_PIN", "ls"]);
    assert_eq!(early.code, 1);
    assert!(early.err.contains("not encrypted"), "{}", early.err);
    // and `pin check` against the vault
    assert_eq!(
        run_with(
            &env,
            None,
            &["-f", enc, "--pin-env", "ALOE_PIN", "pin", "check"]
        )
        .code,
        0
    );
    assert_eq!(run(&["-f", enc, "--pin", "wrong", "pin", "check"]).code, 1);
}

#[test]
fn new_verbs() {
    let dir = scratch("verbs");
    let f = fsfile(&dir);
    let src = dir.join("in.txt");
    std::fs::write(&src, b"data").unwrap();
    run(&["-f", &f, "put", src.to_str().unwrap(), "/a.txt"]);
    let cat = run(&["-f", &f, "cat", "/a.txt"]);
    assert_eq!(cat.code, 0);
    assert_eq!(cat.bytes, b"data");
    assert_eq!(run(&["-f", &f, "cp", "/a.txt", "/b.txt"]).code, 0);
    let stat = run(&["-f", &f, "stat", "/b.txt"]);
    assert_eq!(stat.code, 0);
    assert!(stat.out.contains("type:     entry"), "{}", stat.out);
    assert!(stat.out.contains("size:     4"), "{}", stat.out);
    run(&["-f", &f, "mkdir", "-p", "/d/e"]);
    let tree = run(&["-f", &f, "tree"]);
    assert_eq!(tree.code, 0);
    assert!(
        tree.out.contains("├── ") || tree.out.contains("└── "),
        "{}",
        tree.out
    );
    assert!(tree.out.contains("d/\n"), "{}", tree.out);
    let long = run(&["-f", &f, "ls", "-l", "/"]);
    assert!(long.out.contains("e           4  /a.txt"), "{}", long.out);
    assert!(long.out.contains("c           -  /d"), "{}", long.out);
}

#[test]
fn put_get_recursive_roundtrip() {
    let dir = scratch("rec");
    let f = fsfile(&dir);
    let src = sample_tree(&dir.join("proj"));
    // dst does not exist -> dst IS the tree root
    let put = run(&["-f", &f, "put", "-r", src.to_str().unwrap(), "/code"]);
    assert_eq!(put.code, 0, "{}", put.err);
    assert!(put.out.contains("3 files, 3 containers"), "{}", put.out);
    let ls = run(&["-f", &f, "ls", "/code/sub"]);
    assert!(ls.out.contains("/code/sub/big.bin"), "{}", ls.out);
    let out = dir.join("out");
    assert_eq!(
        run(&["-f", &f, "get", "-r", "/code", out.to_str().unwrap()]).code,
        0
    );
    assert_eq!(std::fs::read(out.join("a.txt")).unwrap(), b"alpha");
    assert_eq!(
        std::fs::read(out.join("sub/big.bin")).unwrap(),
        vec![b'x'; 3 << 20]
    );
    assert_eq!(std::fs::read(out.join("sub/deep/zero")).unwrap(), b"");
    assert!(
        out.join("empty").is_dir(),
        "empty containers survive the round trip"
    );
}

#[test]
fn recursive_nests_into_existing_destination() {
    let dir = scratch("nest");
    let f = fsfile(&dir);
    let src = sample_tree(&dir.join("proj"));
    run(&["-f", &f, "mkdir", "/backup"]);
    // dst exists -> src lands INSIDE it, cp -r style
    assert_eq!(
        run(&["-f", &f, "put", "-r", src.to_str().unwrap(), "/backup"]).code,
        0
    );
    let ls = run(&["-f", &f, "ls", "/backup"]);
    assert!(ls.out.contains("/backup/proj"), "{}", ls.out);
    let dest = dir.join("dest");
    std::fs::create_dir(&dest).unwrap();
    assert_eq!(
        run(&[
            "-f",
            &f,
            "get",
            "-r",
            "/backup/proj",
            dest.to_str().unwrap()
        ])
        .code,
        0
    );
    assert_eq!(std::fs::read(dest.join("proj/a.txt")).unwrap(), b"alpha");
}

#[cfg(unix)]
#[test]
fn put_recursive_skips_what_it_cannot_carry() {
    let dir = scratch("skips");
    let f = fsfile(&dir);
    let src = sample_tree(&dir.join("proj"));
    std::os::unix::fs::symlink("a.txt", src.join("link.txt")).unwrap(); // file symlink: content is copied
    std::os::unix::fs::symlink(&src, src.join("loop")).unwrap(); // dir symlink: skipped, never descended
    let put = run(&["-f", &f, "put", "-r", src.to_str().unwrap(), "/code"]);
    assert_eq!(put.code, 0, "{}", put.err);
    assert!(
        put.out.contains("4 files, 3 containers, 1 skipped"),
        "{}",
        put.out
    );
    assert!(
        put.err.contains("symlinked directory, skipped"),
        "{}",
        put.err
    );
}

#[test]
fn recursive_rejects_the_wrong_shape() {
    let dir = scratch("shape");
    let f = fsfile(&dir);
    let src = sample_tree(&dir.join("proj"));
    let s = src.to_str().unwrap();
    let z = dir.join("z");
    let z = z.to_str().unwrap();
    run(&["-f", &f, "put", "-r", s, "/code"]);
    let r = run(&["-f", &f, "put", s, "/x"]); // dir without -r
    assert_eq!(r.code, 1);
    assert!(r.err.contains("use -r"), "{}", r.err);
    let r = run(&["-f", &f, "get", "/code", z]); // container
    assert_eq!(r.code, 1);
    assert!(r.err.contains("use -r"), "{}", r.err);
    assert_eq!(
        run(&[
            "-f",
            &f,
            "put",
            "-r",
            src.join("a.txt").to_str().unwrap(),
            "/x"
        ])
        .code,
        1
    ); // a file
    assert_eq!(run(&["-f", &f, "put", "-r", "-", "/x"]).code, 1); // stdin has no tree
    assert_eq!(run(&["-f", &f, "put", "-r", s, "/x", "--append"]).code, 1);
    assert_eq!(run(&["-f", &f, "get", "-r", "/code/a.txt", z]).code, 1);
    assert_eq!(run(&["-f", &f, "get", "-r", "/code", "-"]).code, 1); // no tree to stdout
    assert_eq!(run(&["-f", &f, "get", "-r", "/nope", z]).code, 1);
    assert_eq!(run(&["-f", &f, "put", "-r", s, "/code/a.txt"]).code, 1); // dst is an entry
}

#[test]
fn get_recursive_never_escapes_the_destination() {
    // '..' is an ordinary name in the store; it must not become one locally.
    let dir = scratch("escape");
    let f = fsfile(&dir);
    let mut db = aloelite_store::file::open(&f).unwrap();
    let vol = ops::list_volumes(&mut db).unwrap().remove(0).id;
    let m = ops::mount(&mut db, &vol, &MountOptions::default()).unwrap();
    ops::create_container(&mut db, &m, "/evil").unwrap();
    ops::create_container(&mut db, &m, "/evil/..").unwrap(); // a container literally named '..'
    ops::create_entry(&mut db, &m, "/evil/../child", Some(b"escaped")).unwrap();
    ops::create_entry(&mut db, &m, "/evil/ok.txt", Some(b"fine")).unwrap();
    ops::unmount(&mut db, &m).unwrap();
    db.close().unwrap();
    let out = dir.join("safe");
    let r = run(&["-f", &f, "get", "-r", "/evil", out.to_str().unwrap()]);
    assert_eq!(r.code, 0, "{}", r.err);
    assert!(r.err.contains("unsafe local name"), "{}", r.err);
    assert!(r.out.contains("2 skipped"), "{}", r.out); // the '..' container AND its subtree
    let names: Vec<String> = std::fs::read_dir(&out)
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    assert_eq!(names, vec!["ok.txt"]);
    assert!(!dir.join("child").exists());
}

#[test]
fn file_from_env() {
    let dir = scratch("env");
    let f = fsfile(&dir);
    let r = run_with(&[("ALOELITE_FILE", f.as_str())], None, &["volumes"]);
    assert_eq!(r.code, 0);
    assert!(r.out.contains("vol"), "{}", r.out);
    let r = run(&["volumes"]);
    assert_eq!(r.code, 1, "no file, no env -> clear error");
    assert!(r.err.contains("no file given"), "{}", r.err);
}

#[test]
fn prune() {
    let dir = scratch("prune");
    let f = fsfile(&dir);
    assert_eq!(run(&["-f", &f, "ls"]).code, 0); // retires a mount (prunable state)
    let r = run(&["-f", &f, "prune", "--vacuum"]);
    assert_eq!(r.code, 0, "{}", r.err);
    assert!(
        r.out.contains("pruned:") && r.out.contains("vacuumed"),
        "{}",
        r.out
    );
}

#[test]
fn volumes_and_mounts() {
    let dir = scratch("vm");
    let f = fsfile(&dir);
    assert_eq!(run(&["-f", &f, "ls"]).code, 0); // mints a mount row
    let v = run(&["-f", &f, "volumes"]);
    assert_eq!(v.code, 0);
    assert!(
        v.out.contains("vol") && v.out.contains("plain"),
        "{}",
        v.out
    );
    assert_eq!(
        run(&["-f", &f, "volume", "ls"]).out,
        v.out,
        "`volume ls` is an alias"
    );
    let m = run(&["-f", &f, "mounts", "--all"]);
    assert_eq!(m.code, 0);
    assert!(m.out.contains("unmounted"), "{}", m.out); // session-per-invocation retired it
    assert!(m.out.contains("vol:/"), "{}", m.out);
}

// ---------------------------------------------------------------------------
// beyond the reference's file: the no-verb mode, stdin shortcuts, usage
// ---------------------------------------------------------------------------

#[test]
fn no_verb_creates_then_reports() {
    let dir = scratch("bare");
    let f = dir.join("new.fs");
    let f = f.to_str().unwrap();
    let first = run(&["-f", f]);
    assert_eq!(first.code, 0, "{}", first.err);
    assert!(
        first.out.contains(&format!("{f}: created")),
        "{}",
        first.out
    );
    assert!(
        first
            .out
            .contains("volume 'main' created (default, unencrypted)"),
        "{}",
        first.out
    );
    let second = run(&["-f", f]);
    assert_eq!(second.code, 0);
    assert!(
        second.out.contains("1 volume(s)") && second.out.contains("main"),
        "{}",
        second.out
    );
    // a verb against a file that does not exist is a pointed error, not a create
    let missing = run(&["-f", dir.join("nope.fs").to_str().unwrap(), "ls"]);
    assert_eq!(missing.code, 1);
    assert!(missing.err.contains("no such file"), "{}", missing.err);
    // and `volume create` with a pin makes an encrypted one
    let created = run(&["-f", f, "--pin", "pw", "volume", "create", "vault"]);
    assert_eq!(created.code, 0, "{}", created.err);
    assert!(
        created.out.contains("created volume 'vault' (encrypted)"),
        "{}",
        created.out
    );
    assert_eq!(
        run(&["-f", f, "--pin", "pw", "volume", "create", "vault"]).code,
        1,
        "names are unique"
    );
}

#[test]
fn stdin_shortcuts_and_put_from_stdin() {
    let dir = scratch("stdin");
    let f = fsfile(&dir);
    assert_eq!(
        run_with(&[], Some(b"hello\n"), &["-f", &f, "--in", "/note.txt"]).code,
        0
    );
    assert_eq!(
        run_with(&[], Some(b"more\n"), &["-f", &f, "--append", "/note.txt"]).code,
        0
    );
    assert_eq!(run(&["-f", &f, "cat", "/note.txt"]).bytes, b"hello\nmore\n");
    assert_eq!(
        run_with(&[], Some(b"piped"), &["-f", &f, "put", "-", "/p.bin"]).code,
        0
    );
    assert_eq!(run(&["-f", &f, "get", "/p.bin", "-"]).bytes, b"piped");
    assert_eq!(
        run_with(&[], Some(b"x"), &["-f", &f, "--in", "/a", "--append", "/b"]).code,
        1
    );
}

#[test]
fn usage_errors_exit_2_and_help_exits_0() {
    let dir = scratch("usage");
    let f = fsfile(&dir);
    assert_eq!(run(&["-f", &f, "frobnicate"]).code, 2);
    assert_eq!(run(&["-f", &f, "cp", "/only"]).code, 2);
    assert_eq!(run(&["-f", &f, "ls", "--bogus"]).code, 2);
    let help = run(&["--help"]);
    assert_eq!(help.code, 0);
    assert!(
        help.out.contains("usage: aloelite") && help.out.contains("  put "),
        "{}",
        help.out
    );
    let version = run(&["--version"]);
    assert_eq!(version.code, 0);
    assert!(version.out.starts_with("aloelite "), "{}", version.out);
    let fuse = run(&["fuse", "-f", &f, "/mnt"]);
    assert_eq!(fuse.code, 1);
    assert!(fuse.err.contains("aloelite-fuse"), "{}", fuse.err);
}
