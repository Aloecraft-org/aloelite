//! One function per verb, the no-verb status/create, and the dispatch that
//! opens the file, mounts for a mount-scoped verb, runs, unmounts, closes.
//! Output formats are the reference's, line for line, because `test_cli.py`
//! (ported to `tests/cli.rs`) and people's eyes both read them.

use std::collections::BTreeMap;
use std::io::{IsTerminal, Read, Write};
use std::path::Path;

use aloelite_core::crypto::EncMode;
use aloelite_core::ops::{self, MountOptions};
use aloelite_core::types::{MountId, NodeType};
use aloelite_core::{Db, FsError};

use crate::args::{self, Command, Globals, Outcome, Scope};
use crate::fail::{Fail, Result, fail};
use crate::pin::{self, read_pin};
use crate::text::{minute_utc, py_dict};
use crate::transfer::{self, join};
use crate::volume::{enc_label, is_encrypted, resolve_volume_name, select_volume};

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------
//
// Entry point: run(argv) -> exit code. Configurable: DEFAULT_CHUNK_SIZE,
// DEFAULT_VOLUME. Fan-out: the two match blocks in `dispatch` (file-scoped
// verbs, then mount-scoped verbs), one arm per verb in args::VERBS.

/// Chunk size of a volume this command creates (the spec default).
pub const DEFAULT_CHUNK_SIZE: usize = 1 << 20;

/// The volume `aloelite -f FILE` creates on a fresh file.
pub const DEFAULT_VOLUME: &str = "main";

/// Parse, run, and return the process exit code, printing what the
/// reference prints: usage errors on stderr with exit 2, failures as one
/// `aloelite: <why>` line with exit 1.
pub fn run(argv: &[String]) -> i32 {
    match args::parse(argv) {
        Err(msg) => {
            eprint!("{}", args::usage());
            eprintln!("aloelite: error: {msg}");
            2
        }
        Ok(Outcome::Help) => {
            print!("{}", args::usage());
            0
        }
        Ok(Outcome::Version) => {
            println!("aloelite {}", env!("CARGO_PKG_VERSION"));
            0
        }
        Ok(Outcome::Delegated(name)) => {
            match name.as_str() {
                "fuse" => eprintln!("aloelite: 'fuse' is its own program here: run aloelite-fuse"),
                other => eprintln!(
                    "aloelite: '{other}' is the Python distribution's aloelite-{other}; this build has no {other} front-end"
                ),
            }
            1
        }
        Ok(Outcome::Run(g, cmd)) => match execute(&g, cmd.as_ref()) {
            Ok(code) => code,
            Err(e) => {
                eprintln!("aloelite: {}", e.message());
                1
            }
        },
    }
}

// ---------------------------------------------------------------------------
// depth: the session
// ---------------------------------------------------------------------------

fn execute(g: &Globals, cmd: Option<&Command>) -> Result<i32> {
    let Some(file) = g.file.as_deref() else {
        return fail("no file given: pass -f or set ALOELITE_FILE");
    };
    let bare = cmd.is_none() && g.in_path.is_none() && g.append_path.is_none();
    if !bare && !Path::new(file).exists() {
        return fail(format!(
            "{file}: no such file (run 'aloelite -f {file}' to create it)"
        ));
    }
    let mut db = aloelite_store::file::open(file)?;
    let result = dispatch(&mut db, g, cmd, file);
    let closed = db.close();
    match (result, closed) {
        (Err(e), _) => Err(e),
        (Ok(code), Ok(())) => Ok(code),
        (Ok(_), Err(e)) => Err(e.into()),
    }
}

fn dispatch(db: &mut Db, g: &Globals, cmd: Option<&Command>, file: &str) -> Result<i32> {
    let Some(cmd) = cmd else {
        if g.in_path.is_some() || g.append_path.is_some() {
            if g.in_path.is_some() && g.append_path.is_some() {
                return fail("--in and --append are mutually exclusive");
            }
            let data = stdin_unless_terminal()?;
            return with_session(db, g, |db, m| {
                match (&g.append_path, &g.in_path) {
                    (Some(path), _) => transfer::put_bytes(db, m, path, &data, true)?,
                    (None, Some(path)) => transfer::put_bytes(db, m, path, &data, false)?,
                    (None, None) => unreachable!(),
                }
                Ok(0)
            });
        }
        if !ops::list_volumes(db)?.is_empty() && g.any_pin_flag() {
            eprintln!(
                "aloelite: note: this file already exists; pin flags have no effect on the status view"
            );
        }
        return status(db, g, file);
    };
    match cmd.verb.scope {
        Scope::File => match cmd.verb.name {
            "volumes" => volumes(db),
            "mounts" => mounts(db, cmd.flag("all")),
            "prune" => prune(db, g, cmd.flag("vacuum")),
            "volume" => match cmd.sub.map(|s| s.name) {
                Some("ls") => volumes(db),
                Some("create") => volume_create(db, g, cmd.arg("name").unwrap_or_default()),
                _ => fail("unknown volume command"),
            },
            "pin" => match cmd.sub.map(|s| s.name) {
                Some("check") => pin_check(db, g),
                _ => fail("unknown pin command"),
            },
            other => fail(format!("unknown command {other}")),
        },
        Scope::Mount => with_session(db, g, |db, m| match cmd.verb.name {
            "ls" => ls(db, m, cmd.arg("path").unwrap_or("/"), cmd.flag("long")),
            "put" => put(db, m, cmd),
            "get" => get(db, m, cmd),
            "cat" => cat(db, m, cmd.arg("path").unwrap_or_default()),
            "cp" => {
                ops::copy(
                    db,
                    m,
                    cmd.arg("src").unwrap_or_default(),
                    cmd.arg("dst").unwrap_or_default(),
                )?;
                Ok(0)
            }
            "stat" => stat(db, m, cmd.arg("path").unwrap_or_default()),
            "tree" => tree(db, m, cmd.arg("path").unwrap_or("/")),
            "mkdir" => {
                let p = cmd.flag("parents");
                transfer::mkdir(db, m, cmd.arg("path").unwrap_or_default(), p, p)?;
                Ok(0)
            }
            "rm" => {
                let path = cmd.arg("path").unwrap_or_default();
                if cmd.flag("recursive") {
                    ops::remove_recursive(db, m, path)?;
                } else {
                    ops::remove(db, m, path)?;
                }
                Ok(0)
            }
            "mv" => {
                ops::move_(
                    db,
                    m,
                    cmd.arg("src").unwrap_or_default(),
                    cmd.arg("dst").unwrap_or_default(),
                )?;
                Ok(0)
            }
            other => fail(format!("unknown command {other}")),
        }),
    }
}

/// Mount for the call, run `f`, unmount whatever happened.
fn with_session<T>(
    db: &mut Db,
    g: &Globals,
    f: impl FnOnce(&mut Db, &MountId) -> Result<T>,
) -> Result<T> {
    let m = mount_session(db, g)?;
    let result = f(db, &m);
    let unmounted = ops::unmount(db, &m);
    match (result, unmounted) {
        (Err(e), _) => Err(e),
        (Ok(v), Ok(())) => Ok(v),
        (Ok(_), Err(e)) => Err(e.into()),
    }
}

/// Select the volume, settle the PIN question (a plain volume refuses pin
/// flags early; an encrypted one prompts when no flag was given and a
/// terminal exists), mount.
fn mount_session(db: &mut Db, g: &Globals) -> Result<MountId> {
    let vol = select_volume(db, g.volume.as_deref())?;
    let encrypted = is_encrypted(db, &vol)?;
    if !encrypted && g.any_pin_flag() {
        return fail(
            "this volume is not encrypted; drop --pin / --pin-file / --pin-env \
             (or pick an encrypted volume with -v)",
        );
    }
    let mut pin = read_pin(
        g.pin.as_ref(),
        g.pin_file.as_deref(),
        g.pin_env.as_deref(),
        false,
    )?;
    if encrypted && pin.is_none() {
        if !pin::tty_available() {
            return fail(
                "volume is encrypted; supply --pin-file or --pin-env (no controlling terminal to prompt)",
            );
        }
        pin = Some(pin::prompt(false)?);
    }
    Ok(ops::mount(
        db,
        &vol,
        &MountOptions {
            pin: pin.as_deref(),
            ..MountOptions::default()
        },
    )?)
}

fn stdin_unless_terminal() -> Result<Vec<u8>> {
    let mut stdin = std::io::stdin();
    if stdin.is_terminal() {
        return Ok(Vec::new());
    }
    let mut data = Vec::new();
    stdin.read_to_end(&mut data)?;
    Ok(data)
}

// ---------------------------------------------------------------------------
// depth: mount-scoped verbs
// ---------------------------------------------------------------------------

fn ls(db: &mut Db, m: &MountId, path: &str, long: bool) -> Result<i32> {
    for e in ops::list(db, m, path)? {
        if !e.visible {
            continue;
        }
        let full = join(&e.current_directory, &[&e.name]);
        if long {
            let size = if e.kind == NodeType::Entry {
                ops::stat_by_id(db, m, &e.node)?
                    .size
                    .map_or("-".to_owned(), |s| s.to_string())
            } else {
                "-".to_owned()
            };
            let letter = e.kind.as_str().chars().next().unwrap_or('?');
            println!("{letter}  {size:>10}  {full}");
        } else {
            println!(
                "{full}{}",
                if e.kind == NodeType::Container {
                    "/"
                } else {
                    ""
                }
            );
        }
    }
    Ok(0)
}

fn put(db: &mut Db, m: &MountId, cmd: &Command) -> Result<i32> {
    let src = cmd.arg("src").unwrap_or_default();
    let dst = cmd.arg("dst").unwrap_or_default();
    let append = cmd.flag("append");
    if cmd.flag("recursive") {
        return transfer::put_tree(db, m, src, dst, append);
    }
    if src == "-" {
        let mut data = Vec::new();
        std::io::stdin().read_to_end(&mut data)?;
        transfer::put_bytes(db, m, dst, &data, append)?;
        return Ok(0);
    }
    if Path::new(src).is_dir() {
        return fail(format!("{src}: is a directory (use -r)"));
    }
    if append {
        // atomic append per call
        let data = std::fs::read(src)?;
        transfer::put_bytes(db, m, dst, &data, true)?;
        return Ok(0);
    }
    transfer::put_file(db, m, Path::new(src), dst)?;
    Ok(0)
}

fn get(db: &mut Db, m: &MountId, cmd: &Command) -> Result<i32> {
    let src = cmd.arg("src").unwrap_or_default();
    let dst = cmd.arg("dst");
    if cmd.flag("recursive") {
        return transfer::get_tree(db, m, src, dst);
    }
    let mut r = match ops::open_read(db, m, src) {
        Ok(r) => r,
        Err(FsError::NotAnEntry { .. }) => return fail(format!("{src}: is a container (use -r)")),
        Err(e) => return Err(e.into()),
    };
    let mut out: Box<dyn Write> = match dst {
        None | Some("-") => Box::new(std::io::stdout().lock()),
        Some(path) => Box::new(std::fs::File::create(path)?),
    };
    loop {
        let chunk = r.read(db, Some(transfer::CHUNK))?;
        if chunk.is_empty() {
            break;
        }
        out.write_all(&chunk)?;
    }
    out.flush()?;
    r.close(db)?;
    Ok(0)
}

fn cat(db: &mut Db, m: &MountId, path: &str) -> Result<i32> {
    let mut r = ops::open_read(db, m, path)?;
    let mut out = std::io::stdout().lock();
    loop {
        let chunk = r.read(db, Some(transfer::CHUNK))?;
        if chunk.is_empty() {
            break;
        }
        out.write_all(&chunk)?;
    }
    out.flush()?;
    r.close(db)?;
    Ok(0)
}

fn stat(db: &mut Db, m: &MountId, path: &str) -> Result<i32> {
    let st = ops::stat(db, m, path)?;
    println!("path:     {path}");
    println!("id:       {}", st.id.0);
    println!("type:     {}", st.kind.as_str());
    println!(
        "size:     {}",
        st.size.map_or("-".to_owned(), |s| s.to_string())
    );
    println!("created:  {}", st.created_at);
    println!("modified: {}", st.modified_at);
    if !st.metadata.is_empty() {
        println!("metadata: {}", py_dict(&st.metadata));
    }
    Ok(0)
}

fn tree(db: &mut Db, m: &MountId, root: &str) -> Result<i32> {
    fn walk(db: &mut Db, m: &MountId, path: &str, prefix: &str) -> Result<()> {
        let entries: Vec<_> = ops::list(db, m, path)?
            .into_iter()
            .filter(|e| e.visible)
            .collect();
        let count = entries.len();
        for (i, e) in entries.iter().enumerate() {
            let last = i + 1 == count;
            let branch = if last { "└── " } else { "├── " };
            let is_dir = e.kind == NodeType::Container;
            println!(
                "{prefix}{branch}{}{}",
                e.name,
                if is_dir { "/" } else { "" }
            );
            if is_dir {
                let deeper = format!("{prefix}{}", if last { "    " } else { "│   " });
                walk(db, m, &join(path, &[&e.name]), &deeper)?;
            }
        }
        Ok(())
    }
    let root = if root.is_empty() { "/" } else { root };
    println!("{root}");
    walk(db, m, root, "")?;
    Ok(0)
}

// ---------------------------------------------------------------------------
// depth: file-scoped verbs and the no-verb status/create
// ---------------------------------------------------------------------------

fn volume_line(db: &Db, v: &aloelite_core::records::VolumeInfo) -> String {
    format!(
        "{}  {:<9}  {}  {}",
        v.id.0,
        enc_label(db, &v.id),
        minute_utc(Some(v.created_at)),
        v.name.as_deref().unwrap_or("(unnamed)")
    )
}

fn volumes(db: &mut Db) -> Result<i32> {
    for v in ops::list_volumes(db)? {
        println!("{}", volume_line(db, &v));
    }
    Ok(0)
}

fn mounts(db: &mut Db, all: bool) -> Result<i32> {
    let names: BTreeMap<String, Option<String>> = ops::list_volumes(db)?
        .into_iter()
        .map(|v| (v.id.0, v.name))
        .collect();
    for i in ops::list_mounts(db, None, all)? {
        let volume = names
            .get(&i.volume.0)
            .and_then(|n| n.clone())
            .unwrap_or_else(|| i.volume.0[..8].to_owned());
        let label = format!("{volume}:{}", i.mount_path.as_deref().unwrap_or("?"));
        println!(
            "{}  {:<9}  {}  {label}",
            i.id.0,
            i.state.as_str(),
            minute_utc(Some(i.created_at))
        );
    }
    Ok(0)
}

fn prune(db: &mut Db, g: &Globals, vacuum: bool) -> Result<i32> {
    let vol = match g.volume.as_deref() {
        Some(r) => Some(select_volume(db, Some(r))?),
        None => None,
    };
    let r1 = ops::prune(db, vol.as_ref())?;
    let r2 = ops::prune_content(db, vol.as_ref())?;
    println!(
        "pruned: {} nodes, {} locks, {} versions, {} chunks",
        r1.nodes_pruned, r1.locks_pruned, r2.versions_pruned, r2.chunks_pruned
    );
    if vacuum {
        db.connection()
            .execute_batch("VACUUM")
            .map_err(|e| Fail::Engine(FsError::Sqlite(e)))?;
        println!("vacuumed");
    }
    Ok(0)
}

fn volume_create(db: &mut Db, g: &Globals, name: &str) -> Result<i32> {
    let pin = read_pin(
        g.pin.as_ref(),
        g.pin_file.as_deref(),
        g.pin_env.as_deref(),
        true,
    )?;
    if resolve_volume_name(db, name)?.is_some() {
        return fail(format!("a volume named '{name}' already exists"));
    }
    let v = ops::create_volume(
        db,
        Some(name),
        DEFAULT_CHUNK_SIZE,
        pin.as_deref(),
        EncMode::Convergent,
    )?;
    let enc = if pin.is_some() {
        "encrypted"
    } else {
        "unencrypted"
    };
    println!(
        "created volume '{}' ({enc})  {}",
        v.name.as_deref().unwrap_or(name),
        v.id.0
    );
    Ok(0)
}

fn pin_check(db: &mut Db, g: &Globals) -> Result<i32> {
    let vol = select_volume(db, g.volume.as_deref())?;
    if !is_encrypted(db, &vol)? {
        return fail("volume is not encrypted; nothing to check");
    }
    let mut pin = read_pin(
        g.pin.as_ref(),
        g.pin_file.as_deref(),
        g.pin_env.as_deref(),
        false,
    )?;
    if pin.is_none() {
        if !pin::tty_available() {
            return fail("no PIN given and no terminal to prompt");
        }
        pin = Some(pin::prompt(false)?);
    }
    // mount + unmount: the full Argon2id verification
    match ops::mount(
        db,
        &vol,
        &MountOptions {
            pin: pin.as_deref(),
            ..MountOptions::default()
        },
    ) {
        Ok(m) => ops::unmount(db, &m)?,
        Err(FsError::BadKey) => return fail("wrong PIN"),
        Err(e) => return Err(e.into()),
    }
    println!("ok");
    Ok(0)
}

fn status(db: &mut Db, g: &Globals, file: &str) -> Result<i32> {
    let vols = ops::list_volumes(db)?;
    if vols.is_empty() {
        let pin = read_pin(
            g.pin.as_ref(),
            g.pin_file.as_deref(),
            g.pin_env.as_deref(),
            true,
        )?;
        ops::create_volume(
            db,
            Some(DEFAULT_VOLUME),
            DEFAULT_CHUNK_SIZE,
            pin.as_deref(),
            EncMode::Convergent,
        )?;
        let enc = if pin.is_some() {
            "encrypted"
        } else {
            "unencrypted"
        };
        println!("{file}: created");
        println!("  volume '{DEFAULT_VOLUME}' created (default, {enc})");
        if pin.is_none() {
            println!("  for an encrypted volume: aloelite --pin -f {file} volume create NAME");
        }
        println!("try: aloelite -f {file} ls /");
        return Ok(0);
    }
    let size = std::fs::metadata(file)?.len();
    println!("{file}: {size} bytes, {} volume(s)", vols.len());
    for v in &vols {
        println!("  {}", volume_line(db, v));
    }
    Ok(0)
}
