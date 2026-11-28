//! Whole-content and ranged writes: one new version per call, committed
//! pointer swapped atomically. A version records only the chunks it changes
//! (CV-4r1); the rest stay with the version beneath it.
//! `write_ranges` lands many ranges in that one version; `write_range` is
//! its one-range case.

use rusqlite::named_params;

use crate::content::split_chunks;
use crate::db::Db;
use crate::errors::{FsError, Result};
use crate::resolve::resolve;
use crate::templates::mutation::UPDATE_CONTENT;
use crate::types::{MountId, NodeId, NodeType};

use super::{lock_held, require_mount};

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// Atomic full-content replace (IO-2). A new write produces NEW chunks and
/// never mutates a pooled chunk in place (CV-2).
pub fn write_all(db: &mut Db, mount: &MountId, path: &str, data: &[u8]) -> Result<()> {
    db.txn(|db| {
        let m = require_mount(db, mount, true)?;
        let node = writable_leaf(db, &m.mount_point, path, mount)?;
        let version = db.alloc_version(&node)?;
        let size = db.stage_chunks(&node, version, &m.volume, data)?;
        swap_pointer(db, &node, version, size as i64)
    })
}

/// Atomically append and return the new size. Only the partial tail plus
/// the new bytes are re-chunked and recorded; the full leading chunks stay
/// with the prior version and get no row in the new one (CV-1/CV-4r1).
pub fn append(db: &mut Db, mount: &MountId, path: &str, data: &[u8]) -> Result<u64> {
    if data.is_empty() {
        let m = require_mount(db, mount, true)?;
        let node = leaf(db, &m.mount_point, path)?;
        return Ok(db
            .read_content_meta(&node)?
            .map_or(0, |(_, s)| s.max(0) as u64));
    }
    db.txn(|db| {
        let m = require_mount(db, mount, true)?;
        let node = writable_leaf(db, &m.mount_point, path, mount)?;
        let cs = db.chunk_size_of(&m.volume)? as i64;
        let (src_version, size) = db.read_content_meta(&node)?.unwrap_or((0, 0));
        let new_version = db.alloc_version(&node)?;
        let full = size / cs;
        let partial = size % cs;
        // rebuild from the old partial tail + new data, re-chunked from `full`
        let mut tail = Vec::new();
        if partial > 0 {
            tail = first_chunk(db, &node, src_version, full)?;
        }
        tail.extend_from_slice(data);
        stage_from(db, &node, new_version, full, &tail, cs as usize)?;
        let new_size = size + data.len() as i64;
        swap_pointer(db, &node, new_version, new_size)?;
        Ok(new_size as u64)
    })
}

/// Atomically overwrite `[offset, offset+len)`, zero-filling any gap past
/// EOF; returns the new size. The one-range case of [`write_ranges`], which
/// documents the chunk accounting. Each call is one commit and one new
/// version; a caller with several ranges to land together (a FUSE flush)
/// passes them to [`write_ranges`] instead.
pub fn write_range(
    db: &mut Db,
    mount: &MountId,
    path: &str,
    offset: u64,
    data: &[u8],
) -> Result<u64> {
    write_ranges(db, mount, path, &[(offset, data)])
}

/// Atomically overwrite several byte ranges as ONE new content version, as
/// if [`write_range`] had been applied to each in order (later ranges win
/// where they overlap); returns the new size. Empty ranges contribute
/// nothing, and with nothing to write the entry is left untouched.
///
/// A chunk is rebuilt only if a range touches it, or if it lies between the
/// prior EOF chunk and the new end (a short final chunk is rebuilt, a gap
/// zero-fills). Every other chunk is left to the prior version: the new one
/// writes no row for it (CV-4r1), so the cost does not depend on the size of
/// the entry. Rebuilt chunks are read, patched and
/// staged one at a time, so memory is one chunk plus the ranges, however
/// far apart they are. Chunk alignment is preserved, which keeps the
/// uniform-chunks-except-last invariant the ranged reader depends on.
pub fn write_ranges(
    db: &mut Db,
    mount: &MountId,
    path: &str,
    ranges: &[(u64, &[u8])],
) -> Result<u64> {
    let pairs: Vec<(i64, &[u8])> = ranges
        .iter()
        .filter(|(_, d)| !d.is_empty())
        .map(|(o, d)| (*o as i64, *d))
        .collect();
    if pairs.is_empty() {
        let m = require_mount(db, mount, true)?;
        let node = leaf(db, &m.mount_point, path)?;
        return Ok(db
            .read_content_meta(&node)?
            .map_or(0, |(_, s)| s.max(0) as u64));
    }
    db.txn(|db| {
        let m = require_mount(db, mount, true)?;
        let node = writable_leaf(db, &m.mount_point, path, mount)?;
        let cs = db.chunk_size_of(&m.volume)? as i64;
        let (src_version, size) = db.read_content_meta(&node)?.unwrap_or((0, 0));
        let end = pairs
            .iter()
            .map(|(o, d)| o + d.len() as i64)
            .max()
            .unwrap_or(0);
        let new_size = size.max(end);
        let new_version = db.alloc_version(&node)?;
        let src_last = if size > 0 { (size - 1) / cs } else { -1 };

        // depth: which chunks are rebuilt, as merged [lo, hi] index runs
        let mut spans: Vec<(i64, i64)> = pairs
            .iter()
            .map(|(o, d)| (o / cs, (o + d.len() as i64 - 1) / cs))
            .collect();
        if new_size > size {
            // rebuild from the chunk holding the prior EOF to the new end
            spans.push((size / cs, (new_size - 1) / cs));
        }
        let rebuild = merge_spans(spans);

        // depth: rebuild touched chunks one at a time
        for &(lo, hi) in &rebuild {
            for index in lo..=hi {
                let start = index * cs;
                let length = cs.min(new_size - start) as usize;
                let mut buf = vec![0u8; length];
                if index <= src_last {
                    let old = first_chunk(db, &node, src_version, index)?;
                    let n = old.len().min(length);
                    buf[..n].copy_from_slice(&old[..n]);
                }
                let stop = start + length as i64;
                for &(o, d) in &pairs {
                    let s = o.max(start);
                    let e = (o + d.len() as i64).min(stop);
                    if s < e {
                        buf[(s - start) as usize..(e - start) as usize]
                            .copy_from_slice(&d[(s - o) as usize..(e - o) as usize]);
                    }
                }
                db.stage_chunk(&node, new_version, index, &buf)?;
            }
        }
        swap_pointer(db, &node, new_version, new_size)?;
        Ok(new_size as u64)
    })
}

/// Sorted, merged chunk-index runs; touching runs fold into one.
fn merge_spans(mut spans: Vec<(i64, i64)>) -> Vec<(i64, i64)> {
    spans.sort_unstable();
    let mut out: Vec<(i64, i64)> = Vec::new();
    for (lo, hi) in spans {
        match out.last_mut() {
            Some(last) if lo <= last.1 + 1 => last.1 = last.1.max(hi),
            _ => out.push((lo, hi)),
        }
    }
    out
}

/// Atomically set a leaf's size. Shrink trims the boundary chunk and writes
/// nothing else (the new version's chunk count hides the rest); grow
/// zero-fills, rebuilding only the prior short final chunk. No-op when the size is unchanged.
pub fn truncate(db: &mut Db, mount: &MountId, path: &str, size: u64) -> Result<()> {
    db.txn(|db| {
        let m = require_mount(db, mount, true)?;
        let node = writable_leaf(db, &m.mount_point, path, mount)?;
        let cs = db.chunk_size_of(&m.volume)? as i64;
        let (src_version, cur) = db.read_content_meta(&node)?.unwrap_or((0, 0));
        let size = size as i64;
        if size == cur {
            return Ok(());
        }
        let new_version = db.alloc_version(&node)?;
        if size < cur {
            let full = size / cs;
            let rem = size % cs;
            if rem > 0 {
                let chunk = first_chunk(db, &node, src_version, full)?;
                db.stage_chunk(&node, new_version, full, &chunk[..rem as usize])?;
            }
        } else {
            let full = cur / cs;
            let mut pad = Vec::new();
            if cur % cs > 0 {
                pad = first_chunk(db, &node, src_version, full)?;
            }
            pad.resize((size - full * cs) as usize, 0);
            stage_from(db, &node, new_version, full, &pad, cs as usize)?;
        }
        swap_pointer(db, &node, new_version, size)
    })
}

// ---------------------------------------------------------------------------
// depth
// ---------------------------------------------------------------------------

fn leaf(db: &mut Db, mount_point: &NodeId, path: &str) -> Result<NodeId> {
    let found = resolve(db, mount_point, path)?;
    if found.kind == NodeType::Container {
        return Err(FsError::NotAnEntry { node: found.node.0 });
    }
    Ok(found.node)
}

/// The leaf at `path`, refused with `lock_held` if another mount holds it.
fn writable_leaf(db: &mut Db, mount_point: &NodeId, path: &str, mount: &MountId) -> Result<NodeId> {
    let node = leaf(db, mount_point, path)?;
    if lock_held(db, &node, mount)? {
        return Err(FsError::LockHeld { node: node.0 });
    }
    Ok(node)
}

/// The bytes of chunk `index` of `version` (empty when absent).
fn first_chunk(db: &mut Db, node: &NodeId, version: i64, index: i64) -> Result<Vec<u8>> {
    Ok(db
        .read_chunk_range(node, version, index, index)?
        .into_iter()
        .next()
        .map_or_else(Vec::new, |(_, d)| d))
}

/// Re-chunk `bytes` and stage them at consecutive indexes from `index`.
fn stage_from(
    db: &mut Db,
    node: &NodeId,
    version: i64,
    mut index: i64,
    bytes: &[u8],
    cs: usize,
) -> Result<()> {
    for chunk in split_chunks(bytes, cs) {
        db.stage_chunk(node, version, index, chunk)?;
        index += 1;
    }
    Ok(())
}

/// Swap the committed pointer (CV-3); the touch trigger bumps `modified_at`.
fn swap_pointer(db: &mut Db, node: &NodeId, version: i64, size: i64) -> Result<()> {
    db.run(
        UPDATE_CONTENT,
        named_params! { ":node": node, ":version": version, ":size": size, ":hash": Option::<Vec<u8>>::None },
    )?;
    Ok(())
}
