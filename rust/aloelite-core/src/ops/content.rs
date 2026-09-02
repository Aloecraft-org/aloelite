//! Whole-content and ranged writes: one new version per call, committed
//! pointer swapped atomically, unchanged chunks carried by reference.

use rusqlite::named_params;

use crate::content::split_chunks;
use crate::db::Db;
use crate::errors::{FsError, Result};
use crate::resolve::resolve;
use crate::templates::mutation::{COPY_CHUNK_REFS_RANGE, UPDATE_CONTENT};
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

/// Atomically append and return the new size. The prior version's full
/// leading chunks are carried BY REFERENCE; only the partial tail plus the
/// new bytes are re-chunked (CV-1/CV-4).
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
        if full > 0 {
            carry(db, &node, new_version, src_version, 0, full - 1)?;
        }
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
/// EOF; returns the new size. Chunk alignment is preserved (an overwrite
/// never shifts bytes), so full chunks either side of the window carry by
/// reference and only the window is read, patched, re-chunked and staged.
pub fn write_range(
    db: &mut Db,
    mount: &MountId,
    path: &str,
    offset: u64,
    data: &[u8],
) -> Result<u64> {
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
        let offset = offset as i64;
        let end = offset + data.len() as i64;
        let new_size = size.max(end);
        let new_version = db.alloc_version(&node)?;

        let lo = offset / cs;
        let hi = (end - 1) / cs;
        // The window reaches down to the chunk containing the prior EOF when
        // writing at/past it (rebuild a short final chunk; zero-fill a gap).
        let window_lo = lo.min(size / cs);
        let src_last = if size > 0 { (size - 1) / cs } else { -1 };

        if window_lo > 0 {
            carry(db, &node, new_version, src_version, 0, window_lo - 1)?;
        }
        if src_last > hi {
            carry(db, &node, new_version, src_version, hi + 1, src_last)?;
        }

        let base = window_lo * cs;
        let mut buf: Vec<u8> = Vec::new();
        let win_src_hi = hi.min(src_last);
        if win_src_hi >= window_lo {
            for (_, chunk) in db.read_chunk_range(&node, src_version, window_lo, win_src_hi)? {
                buf.extend_from_slice(&chunk);
            }
        }
        let start = (offset - base) as usize;
        let stop = (end - base) as usize;
        if start > buf.len() {
            buf.resize(start, 0);
        }
        if stop > buf.len() {
            buf.resize(stop, 0);
        }
        buf[start..stop].copy_from_slice(data);
        stage_from(db, &node, new_version, window_lo, &buf, cs as usize)?;
        swap_pointer(db, &node, new_version, new_size)?;
        Ok(new_size as u64)
    })
}

/// Atomically set a leaf's size. Shrink carries full leading chunks by
/// reference and trims the boundary chunk; grow zero-fills, rebuilding only
/// the prior short final chunk. No-op when the size is unchanged.
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
            if full > 0 {
                carry(db, &node, new_version, src_version, 0, full - 1)?;
            }
            let rem = size % cs;
            if rem > 0 {
                let chunk = first_chunk(db, &node, src_version, full)?;
                db.stage_chunk(&node, new_version, full, &chunk[..rem as usize])?;
            }
        } else {
            let full = cur / cs;
            if full > 0 {
                carry(db, &node, new_version, src_version, 0, full - 1)?;
            }
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

/// Carry chunks `[lo, hi]` of `src_version` into `dst_version` by reference.
fn carry(
    db: &mut Db,
    node: &NodeId,
    dst_version: i64,
    src_version: i64,
    lo: i64,
    hi: i64,
) -> Result<()> {
    db.run(
        COPY_CHUNK_REFS_RANGE,
        named_params! { ":node": node, ":dst_version": dst_version, ":src_version": src_version, ":lo": lo, ":hi": hi },
    )?;
    Ok(())
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
