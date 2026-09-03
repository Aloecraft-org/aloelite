//! `put -r` / `get -r`: a whole tree, one file at a time, as a walk over
//! single-node operations — the loop lives here and the Mount API is
//! untouched, exactly as in the reference. Memory is bounded by one chunk.
//!
//! Destination follows `cp -r`: when DST already exists as a container
//! (put) or a directory (get), SRC lands INSIDE it as `DST/<basename>`;
//! otherwise DST becomes the tree's new root. Two facade conveniences the
//! verbs share live here too, [`mkdir`] and [`put_bytes`], with the same
//! semantics as the Python `Mount.mkdir` / `Mount.put`.

use std::collections::HashSet;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};

use aloelite_core::ops;
use aloelite_core::types::{MountId, NodeType, WriteMode};
use aloelite_core::{Db, FsError};

use crate::fail::{Result, fail};
use crate::text::n;

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------
//
// Entry points: put_tree, get_tree, put_file, get_file, put_bytes, mkdir,
// join, note. Configurable: CHUNK.

/// Streaming granularity for file transfers.
pub const CHUNK: usize = 1 << 20;

/// Join mount-relative segments, collapsing empties. Always absolute.
pub fn join(base: &str, parts: &[&str]) -> String {
    let mut segs: Vec<&str> = base.split('/').filter(|s| !s.is_empty()).collect();
    for p in parts {
        segs.extend(p.split('/').filter(|s| !s.is_empty()));
    }
    format!("/{}", segs.join("/"))
}

/// A non-fatal remark (a skipped node): stderr, so stdout stays clean.
pub fn note(msg: &str) {
    eprintln!("aloelite: {msg}");
}

/// `Mount.mkdir`: create a container; `parents` creates missing
/// intermediates; if a visible node already exists at `path`, `exist_ok`
/// accepts it when it is a container (never minting a hidden duplicate),
/// otherwise `container_exists`.
pub fn mkdir(db: &mut Db, m: &MountId, path: &str, parents: bool, exist_ok: bool) -> Result<()> {
    match ops::stat(db, m, path) {
        Ok(found) => {
            if exist_ok && found.kind == NodeType::Container {
                return Ok(());
            }
            return Err(FsError::ContainerExists {
                name: path.to_owned(),
            }
            .into());
        }
        Err(FsError::NotFound { .. }) | Err(FsError::NotAContainer { .. }) => {}
        Err(e) => return Err(e.into()),
    }
    if parents {
        let segs: Vec<&str> = path.split('/').filter(|s| !s.is_empty()).collect();
        for i in 1..segs.len() {
            let parent = format!("/{}", segs[..i].join("/"));
            if !ops::exists(db, m, &parent)? {
                ops::create_container(db, m, &parent)?;
            }
        }
    }
    ops::create_container(db, m, path)?;
    Ok(())
}

/// `Mount.put`: append if asked, replace if the entry exists, create it
/// otherwise. Each branch is one atomic operation.
pub fn put_bytes(db: &mut Db, m: &MountId, path: &str, data: &[u8], append: bool) -> Result<()> {
    let exists = ops::exists(db, m, path)?;
    if append {
        if exists {
            ops::append(db, m, path, data)?;
        } else {
            ops::create_entry(db, m, path, Some(data))?;
        }
    } else if exists {
        ops::write_all(db, m, path, data)?;
    } else {
        ops::create_entry(db, m, path, Some(data))?;
    }
    Ok(())
}

/// One local file → one entry, streamed.
pub fn put_file(db: &mut Db, m: &MountId, local: &Path, dst: &str) -> Result<()> {
    let mut f = std::fs::File::open(local)?;
    let mut w = ops::open_write(db, m, dst, WriteMode::Truncate, None)?;
    let mut buf = vec![0u8; CHUNK];
    loop {
        let got = f.read(&mut buf)?;
        if got == 0 {
            break;
        }
        w.write(db, &buf[..got])?;
    }
    w.close(db)?;
    Ok(())
}

/// One entry → one local file, streamed.
pub fn get_file(db: &mut Db, m: &MountId, src: &str, local: &Path) -> Result<()> {
    let mut r = ops::open_read(db, m, src)?;
    let mut out = std::fs::File::create(local)?;
    loop {
        let chunk = r.read(db, Some(CHUNK))?;
        if chunk.is_empty() {
            break;
        }
        out.write_all(&chunk)?;
    }
    r.close(db)?;
    Ok(())
}

/// `put -r SRC DST`: a local directory tree into the volume.
pub fn put_tree(db: &mut Db, m: &MountId, src: &str, dst: &str, append: bool) -> Result<i32> {
    if src == "-" {
        return fail("put -r: stdin is not a directory");
    }
    if append {
        return fail("put -r: --append does not apply to a tree");
    }
    let src_path = Path::new(src);
    if !src_path.is_dir() {
        return fail(format!("{src}: not a directory (drop -r)"));
    }
    let name = std::path::absolute(src_path)?
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    let root = tree_root_remote(db, m, dst, &name)?;
    mkdir(db, m, &root, true, true)?;
    let (mut files, mut dirs, mut skipped) = (0, 0, 0);
    for (rel, local, is_dir) in local_walk(src_path)? {
        let dst = join(&root, &[&rel]);
        if is_dir {
            if local.symlink_metadata()?.file_type().is_symlink() {
                note(&format!(
                    "{}: symlinked directory, skipped",
                    local.display()
                ));
                skipped += 1;
                continue;
            }
            mkdir(db, m, &dst, true, true)?;
            dirs += 1;
        } else if local.is_file() {
            // follows symlinks: copy what they point at
            put_file(db, m, &local, &dst)?;
            files += 1;
        } else {
            note(&format!("{}: not a regular file, skipped", local.display()));
            skipped += 1;
        }
    }
    Ok(summary("put", files, dirs, skipped, "container", None))
}

/// `get -r SRC DST`: a container tree out to a local directory.
pub fn get_tree(db: &mut Db, m: &MountId, src: &str, dst: Option<&str>) -> Result<i32> {
    let dst = match dst {
        None | Some("-") => return fail("get -r: needs a local directory destination, not stdout"),
        Some(d) => d,
    };
    let st = match ops::stat(db, m, src) {
        Ok(st) => st,
        Err(FsError::NotFound { .. }) => return fail(format!("{src}: no such path")),
        Err(e) => return Err(e.into()),
    };
    if st.kind != NodeType::Container {
        return fail(format!("{src}: not a container (drop -r)"));
    }
    let name = src.split('/').rfind(|s| !s.is_empty()).unwrap_or("");
    let root = tree_root_local(Path::new(dst), name)?;
    std::fs::create_dir_all(&root)?;
    let (mut files, mut dirs, mut skipped) = (0, 0, 0);
    let mut pruned: HashSet<String> = HashSet::new(); // containers refused, with their subtrees
    for (rel, path, is_dir) in remote_walk(db, m, src)? {
        let (head, name) = rel.rsplit_once('/').unwrap_or(("", rel.as_str()));
        if pruned.contains(head) {
            skipped += 1;
            if is_dir {
                pruned.insert(rel.clone());
            }
            continue;
        }
        // Names are opaque segments in the store — '..' and separators are
        // legal there and would escape `root` here, so they never reach the
        // local filesystem.
        if name == "."
            || name == ".."
            || name.contains('/')
            || name.contains(std::path::MAIN_SEPARATOR)
        {
            note(&format!("{path}: unsafe local name '{name}', skipped"));
            skipped += 1;
            if is_dir {
                pruned.insert(rel.clone());
            }
            continue;
        }
        let mut local = root.clone();
        for seg in rel.split('/') {
            local.push(seg);
        }
        if is_dir {
            std::fs::create_dir_all(&local)?;
            dirs += 1;
        } else {
            get_file(db, m, &path, &local)?;
            files += 1;
        }
    }
    Ok(summary(
        "get",
        files,
        dirs,
        skipped,
        "directory",
        Some("directories"),
    ))
}

// ---------------------------------------------------------------------------
// depth: the walks and cp -r's destination rule
// ---------------------------------------------------------------------------

/// `(relpath, localpath, is_dir)` for everything under a local directory,
/// parents before children, each directory's subdirectories listed before
/// its files (`os.walk`'s order). Symlinked directories are listed but not
/// descended into, so a cycle can never hang the transfer.
fn local_walk(root: &Path) -> Result<Vec<(String, PathBuf, bool)>> {
    fn visit(dir: &Path, rel: &str, out: &mut Vec<(String, PathBuf, bool)>) -> Result<()> {
        let mut dirs = Vec::new();
        let mut files = Vec::new();
        for entry in std::fs::read_dir(dir)? {
            let entry = entry?;
            let name = entry.file_name().to_string_lossy().into_owned();
            // is_dir follows symlinks, as os.walk's split does
            if entry.path().is_dir() {
                dirs.push(name);
            } else {
                files.push(name);
            }
        }
        dirs.sort();
        files.sort();
        let child_rel = |name: &str| {
            if rel.is_empty() {
                name.to_owned()
            } else {
                format!("{rel}/{name}")
            }
        };
        for d in &dirs {
            out.push((child_rel(d), dir.join(d), true));
        }
        for f in &files {
            out.push((child_rel(f), dir.join(f), false));
        }
        for d in &dirs {
            let path = dir.join(d);
            if !path.symlink_metadata()?.file_type().is_symlink() {
                visit(&path, &child_rel(d), out)?;
            }
        }
        Ok(())
    }
    let mut out = Vec::new();
    visit(root, "", &mut out)?;
    Ok(out)
}

/// `(relpath, path, is_dir)` for everything under a container, parents
/// before children (breadth-first, so a container always exists before
/// anything it holds).
fn remote_walk(db: &mut Db, m: &MountId, root: &str) -> Result<Vec<(String, String, bool)>> {
    let mut out = Vec::new();
    let mut queue = std::collections::VecDeque::from([(String::new(), root.to_owned())]);
    while let Some((rel, cur)) = queue.pop_front() {
        for e in ops::list(db, m, &cur)? {
            if !e.visible {
                continue;
            }
            let is_dir = e.kind == NodeType::Container;
            let child = if rel.is_empty() {
                e.name.clone()
            } else {
                format!("{rel}/{}", e.name)
            };
            let path = join(&cur, &[&e.name]);
            out.push((child.clone(), path.clone(), is_dir));
            if is_dir {
                queue.push_back((child, path));
            }
        }
    }
    Ok(out)
}

/// cp -r's destination rule, remote side: an existing container receives
/// the tree as a child; a missing path becomes the tree's root itself.
fn tree_root_remote(db: &mut Db, m: &MountId, dst: &str, name: &str) -> Result<String> {
    if name.is_empty() {
        return Ok(dst.to_owned());
    }
    match ops::stat(db, m, dst) {
        Err(FsError::NotFound { .. }) => Ok(dst.to_owned()),
        Err(FsError::NotAContainer { .. }) => {
            fail(format!("{dst}: path descends through an entry"))
        }
        Err(e) => Err(e.into()),
        Ok(st) if st.kind != NodeType::Container => {
            fail(format!("{dst}: exists and is not a container"))
        }
        Ok(_) => Ok(join(dst, &[name])),
    }
}

/// cp -r's destination rule, local side.
fn tree_root_local(dst: &Path, name: &str) -> Result<PathBuf> {
    if name.is_empty() {
        return Ok(dst.to_owned());
    }
    if dst.is_dir() {
        return Ok(dst.join(name));
    }
    if dst.exists() {
        return fail(format!("{}: exists and is not a directory", dst.display()));
    }
    Ok(dst.to_owned())
}

fn summary(
    verb: &str,
    files: usize,
    dirs: usize,
    skipped: usize,
    unit: &str,
    units: Option<&str>,
) -> i32 {
    let tail = if skipped > 0 {
        format!(", {skipped} skipped")
    } else {
        String::new()
    };
    println!(
        "{verb}: {}, {}{tail}",
        n(files, "file", None),
        n(dirs, unit, units)
    );
    0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn join_collapses_and_stays_absolute() {
        assert_eq!(join("/", &["a"]), "/a");
        assert_eq!(join("/code/", &["sub//deep"]), "/code/sub/deep");
        assert_eq!(join("", &[]), "/");
    }
}
