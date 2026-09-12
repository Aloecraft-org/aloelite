//! Streaming: descriptor lifecycle. The descriptor itself lives in
//! [`crate::descriptor`]; these two open it.

use rusqlite::named_params;

use crate::db::Db;
use crate::descriptor::{Descriptor, WriterSetup};
use crate::errors::{FsError, Result};
use crate::resolve::resolve;
use crate::templates::mutation::CREATE_LOCK;
use crate::types::{FdId, LockId, MountId, NodeType, WriteMode};

use super::locking::require_own_lock;
use super::structural::create_entry_internal;
use super::{lock_held, require_mount};

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

pub fn open_read(db: &mut Db, mount: &MountId, path: &str) -> Result<Descriptor> {
    let m = require_mount(db, mount, false)?;
    let found = resolve(db, &m.mount_point, path)?;
    if found.kind == NodeType::Container {
        return Err(FsError::NotAnEntry { node: found.node.0 });
    }
    let (version, size) = db.read_content_meta(&found.node)?.unwrap_or((0, 0));
    let cs = db.chunk_size_of(&m.volume)?;
    let fd = FdId(db.gen_id());
    Ok(Descriptor::reader(
        fd, found.node, m.volume, cs, version, size,
    ))
}

/// Open a leaf for streaming writes under an exclusive lock, creating it on
/// a miss unless the mode is append.
///
/// `lock` supplies an EXISTING lock from [`super::lock`] instead of minting
/// one, and the difference is LIFETIME: a minted lock is owned by the
/// descriptor and released on close/abort, a supplied one outlives it and
/// goes only on `unlock`. A supplied lock must belong to this mount and to
/// this node, else `lock_held` / `lock_invalid`.
pub fn open_write(
    db: &mut Db,
    mount: &MountId,
    path: &str,
    mode: WriteMode,
    lock: Option<&LockId>,
) -> Result<Descriptor> {
    db.txn(|db| {
        let m = require_mount(db, mount, true)?;
        let node = match resolve(db, &m.mount_point, path) {
            Ok(found) if found.kind == NodeType::Container => {
                return Err(FsError::NotAnEntry { node: found.node.0 });
            }
            Ok(found) => found.node,
            // safe creation inside this same transaction (the MountId is
            // passed, not the resolved Mount: create re-validates it)
            Err(FsError::NotFound { .. }) if mode == WriteMode::Truncate => {
                create_entry_internal(db, mount, path, None)?
            }
            Err(e) => return Err(e),
        };
        let (lock, owns_lock) = match lock {
            Some(supplied) => {
                let row = require_own_lock(db, mount, supplied)?;
                if row.node != node {
                    // a lock authorises writes to ONE node; guards the
                    // copy-paste error of reusing a token from another path
                    return Err(FsError::LockInvalid {
                        msg: format!("lock {supplied} is not a lock on {node}"),
                    });
                }
                (supplied.clone(), false)
            }
            None => {
                if lock_held(db, &node, mount)? {
                    return Err(FsError::LockHeld { node: node.0 });
                }
                let minted = LockId(db.gen_id());
                let created_at = db.now_ns();
                db.run(
                    CREATE_LOCK,
                    named_params! {
                        ":id": minted,
                        ":mount": mount,
                        ":node": node,
                        ":expires_at": Option::<i64>::None,
                        ":created_at": created_at,
                    },
                )?;
                (minted, true)
            }
        };
        let cs = db.chunk_size_of(&m.volume)?;
        // Append carries the prior version's FULL leading chunks forward
        // unchanged and rebuilds only from the partial final chunk; truncate
        // starts empty. Both stream chunk-by-chunk with bounded memory.
        let mut setup = WriterSetup {
            lock,
            owns_lock,
            carry_src: 0,
            carry_full: 0,
            pending: Vec::new(),
            position: 0,
        };
        if mode == WriteMode::Append {
            let (src_version, size) = db.read_content_meta(&node)?.unwrap_or((0, 0));
            let size = size.max(0) as u64;
            let cs64 = cs as u64;
            setup.carry_src = src_version;
            setup.carry_full = size / cs64;
            if size > 0 && !size.is_multiple_of(cs64) {
                let idx = (size / cs64) as i64;
                let tail = db.read_chunk_range(&node, src_version, idx, idx)?;
                setup.pending = tail.into_iter().next().map_or_else(Vec::new, |(_, d)| d);
            }
            setup.position = size;
        }
        let fd = FdId(db.gen_id());
        Ok(Descriptor::writer(fd, node, m.volume, cs, setup))
    })
}
