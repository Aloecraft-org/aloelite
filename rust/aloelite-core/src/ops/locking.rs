//! Locking: a lock with no open descriptor.
//!
//! A lock taken here is a FIRST-CLASS OBJECT: it exists with no descriptor
//! and outlives any single operation. `open_write` still mints its own lock
//! when none is supplied — right for a POSIX write, where the lock's whole
//! life is the open file — but a protocol whose LOCK, PUT and UNLOCK arrive
//! as separate requests (WebDAV class 2) needs the lock durable and the
//! descriptor transient, which is the inversion these three provide.
//!
//! Nothing here actively invalidates a lock: validity is derived from the
//! mount's validity and the ttl (ACC-9/10), so renewal is just moving
//! `expires_at`, and reclamation stays `prune`'s job.

use rusqlite::named_params;

use crate::db::Db;
use crate::errors::{FsError, Result};
use crate::records::LockInfo;
use crate::resolve::resolve;
use crate::templates::mutation::{CREATE_LOCK, RELEASE_LOCK, RENEW_LOCK};
use crate::templates::resolution::GET_LOCK;
use crate::templates::validation::CHECK_ANY_LOCK;
use crate::types::{LockId, MountId, NodeId};

use super::require_mount;

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// Take a standalone exclusive lock on a leaf or container.
///
/// Refused if ANY valid lock exists on the node — including one this mount
/// already holds. Self-exclusion is deliberate here and deliberately absent
/// from the write paths: a second row for one (mount, node) would make
/// `unlock` ambiguous about which lock it released. `ttl_ms: None` never
/// expires; such a lock is reclaimed only when its mount ends (ACC-9).
pub fn lock(db: &mut Db, mount: &MountId, path: &str, ttl_ms: Option<i64>) -> Result<LockInfo> {
    db.txn(|db| {
        let m = require_mount(db, mount, false)?;
        let node = resolve(db, &m.mount_point, path)?.node;
        let held: Option<i64> = db.scalar(CHECK_ANY_LOCK, named_params! { ":node": node })?;
        if held.unwrap_or(0) != 0 {
            return Err(FsError::LockHeld { node: node.0 });
        }
        let lock_id = LockId(db.gen_id());
        let now = db.now_ns();
        let expires = ttl_ms.map(|ttl| now + ttl * 1_000_000);
        let created_at = db.now_ns();
        db.run(
            CREATE_LOCK,
            named_params! {
                ":id": lock_id,
                ":mount": mount,
                ":node": node,
                ":expires_at": expires,
                ":created_at": created_at,
            },
        )?;
        Ok(LockInfo {
            id: lock_id,
            mount: mount.clone(),
            node,
            expires_at: expires,
        })
    })
}

/// Release a lock this mount holds. Not idempotent: releasing a lock that is
/// already gone is `lock_invalid`, since succeeding silently would hide a
/// client that believes it still holds something it does not.
pub fn unlock(db: &mut Db, mount: &MountId, lock: &LockId) -> Result<()> {
    db.txn(|db| {
        require_mount(db, mount, false)?;
        require_own_lock(db, mount, lock)?;
        db.run(RELEASE_LOCK, named_params! { ":lock": lock })?;
        Ok(())
    })
}

/// Extend (or clear) a lock's ttl, keeping its id — which is what lets a
/// client carry one token across many requests (the WebDAV lock-token
/// shape).
pub fn renew_lock(
    db: &mut Db,
    mount: &MountId,
    lock: &LockId,
    ttl_ms: Option<i64>,
) -> Result<LockInfo> {
    db.txn(|db| {
        require_mount(db, mount, false)?;
        let row = require_own_lock(db, mount, lock)?;
        let now = db.now_ns();
        let expires = ttl_ms.map(|ttl| now + ttl * 1_000_000);
        db.run(
            RENEW_LOCK,
            named_params! { ":lock": lock, ":expires_at": expires },
        )?;
        Ok(LockInfo {
            id: lock.clone(),
            mount: mount.clone(),
            node: row.node,
            expires_at: expires,
        })
    })
}

// ---------------------------------------------------------------------------
// depth
// ---------------------------------------------------------------------------

pub(crate) struct OwnedLock {
    pub node: NodeId,
}

/// Fetch a lock the caller is entitled to act on, or refuse.
///
/// Ownership is checked, not just validity: a lock is mount-scoped (ACC-6),
/// so a mount releasing or renewing another mount's lock would defeat the
/// whole point. `lock_held` is the honest answer for a live lock belonging
/// to someone else — it IS held, by another mount — while a lock that has
/// expired, been released, or lost its mount is `lock_invalid`.
pub(crate) fn require_own_lock(db: &mut Db, mount: &MountId, lock: &LockId) -> Result<OwnedLock> {
    struct Row {
        mount: MountId,
        node: NodeId,
        valid: bool,
    }
    let row = db.one(GET_LOCK, named_params! { ":lock": lock }, |r| {
        Ok(Row {
            mount: r.get("mount_id")?,
            node: r.get("node_id")?,
            valid: r.get::<_, i64>("valid")? != 0,
        })
    })?;
    match row {
        Some(row) if row.valid => {
            if &row.mount != mount {
                return Err(FsError::LockHeld { node: row.node.0 });
            }
            Ok(OwnedLock { node: row.node })
        }
        _ => Err(FsError::LockInvalid {
            msg: format!("lock {lock} is not valid"),
        }),
    }
}
