//! Structural operations: create, place, rename, annotate (path-first,
//! atomic, mutating).

use std::collections::BTreeMap;

use rusqlite::named_params;

use crate::db::Db;
use crate::errors::{FsError, Result};
use crate::resolve::{resolve, resolve_parent};
use crate::templates::mutation::{
    ARCHIVE_PLACEMENT, RENAME_NODE_IF_SOLE, RENAME_PLACEMENT, SET_ATIME, SET_METADATA,
    SET_MODIFIED_AT, SET_OWNER, SET_RETENTION_KEEP, XATTR_GET, XATTR_LIST, XATTR_REMOVE, XATTR_SET,
};
use crate::templates::resolution::{GET_NODE, RESOLVE_SEGMENT};
use crate::templates::validation::CHECK_CYCLE;
use crate::types::{MountId, NodeId, NodeType};

use super::{
    link_child, lock_held, meta_to_json, new_node, put_initial_content, require_mount, require_name,
};

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

pub fn create_container(db: &mut Db, mount: &MountId, path: &str) -> Result<NodeId> {
    db.txn(|db| {
        let m = require_mount(db, mount, true)?;
        let parent = resolve_parent(db, &m.mount_point, path)?;
        require_name(&parent.name)?;
        let node = new_node(db, &m, NodeType::Container, &parent.name, None, None, None)?;
        link_child(db, &m, &parent.container, &node, None)?;
        Ok(node)
    })
}

pub fn create_entry(
    db: &mut Db,
    mount: &MountId,
    path: &str,
    data: Option<&[u8]>,
) -> Result<NodeId> {
    db.txn(|db| create_entry_internal(db, mount, path, data))
}

/// Rename the placement the path walked (D-5). For a hardlinked node this
/// renames only that directory entry; the node's own name follows along
/// only while it has a single placement (the era-1-identical common case).
pub fn rename(db: &mut Db, mount: &MountId, path: &str, name: &str) -> Result<()> {
    db.txn(|db| {
        let m = require_mount(db, mount, true)?;
        require_name(name)?;
        let parent = resolve_parent(db, &m.mount_point, path)?;
        let node = resolve(db, &m.mount_point, path)?.node;
        // ACC-11: renaming edits the directory entry, which IS a placement
        // change -- the same answer move() gives.
        if lock_held(db, &node, mount)? {
            return Err(FsError::LockHeld { node: node.0 });
        }
        db.run(
            RENAME_PLACEMENT,
            named_params! { ":parent": parent.container, ":node": node, ":old_name": parent.name, ":name": name },
        )?;
        db.run(RENAME_NODE_IF_SOLE, named_params! { ":node": node, ":name": name })?;
        Ok(())
    })
}

/// Hardlink (era 2 / D-5): place the node `src` resolves to under `dst`'s
/// parent as an additional active placement, named by `dst`'s final segment.
/// Containers are refused (PI-1 keeps the container graph a tree); an
/// existing `dst` is `already_exists`.
pub fn link(db: &mut Db, mount: &MountId, src: &str, dst: &str) -> Result<()> {
    db.txn(|db| {
        let m = require_mount(db, mount, true)?;
        let found = resolve(db, &m.mount_point, src)?;
        if found.kind == NodeType::Container {
            return Err(FsError::NotAnEntry { node: found.node.0 });
        }
        // ACC-11: a hardlink adds a placement to the locked node and changes
        // its nlink -- milder than rename, but the same class.
        if lock_held(db, &found.node, mount)? {
            return Err(FsError::LockHeld { node: found.node.0 });
        }
        let parent = resolve_parent(db, &m.mount_point, dst)?;
        require_name(&parent.name)?;
        let taken = db.one(
            RESOLVE_SEGMENT,
            named_params! { ":container": parent.container, ":name": parent.name },
            |_| Ok(()),
        )?;
        if taken.is_some() {
            return Err(FsError::AlreadyExists {
                msg: format!("{:?} already exists at the link target", parent.name),
            });
        }
        let own_name: Option<String> =
            db.one(GET_NODE, named_params! { ":node": found.node }, |r| {
                Ok(r.get("name")?)
            })?;
        let differs = own_name.as_deref() != Some(parent.name.as_str());
        let override_name = if differs {
            Some(parent.name.as_str())
        } else {
            None
        };
        link_child(db, &m, &parent.container, &found.node, override_name)?;
        Ok(())
    })
}

/// Create a symlink / fifo / socket leaf (era 2 / D-3). `data` is the
/// symlink target; fifo/socket carry an empty content row.
pub fn create_special(
    db: &mut Db,
    mount: &MountId,
    path: &str,
    kind: NodeType,
    data: &[u8],
) -> Result<NodeId> {
    if matches!(kind, NodeType::Container | NodeType::Entry) {
        return Err(FsError::unsupported(format!(
            "create_special is for special types, not {kind}"
        )));
    }
    db.txn(|db| {
        let m = require_mount(db, mount, true)?;
        let parent = resolve_parent(db, &m.mount_point, path)?;
        require_name(&parent.name)?;
        let node = new_node(db, &m, kind, &parent.name, None, None, None)?;
        put_initial_content(db, &m, &node, data)?;
        link_child(db, &m, &parent.container, &node, None)?;
        Ok(node)
    })
}

/// chown/chmod (era 2): set any of uid/gid/mode, `None` leaves a field
/// unchanged; mode is masked to 07777. Bumps ctime (trigger), never
/// `modified_at`.
pub fn set_owner(
    db: &mut Db,
    mount: &MountId,
    path: &str,
    uid: Option<i64>,
    gid: Option<i64>,
    mode: Option<i64>,
) -> Result<()> {
    db.txn(|db| {
        let m = require_mount(db, mount, true)?;
        let node = resolve(db, &m.mount_point, path)?.node;
        db.run(
            SET_OWNER,
            named_params! { ":node": node, ":uid": uid, ":gid": gid, ":mode": mode.map(|m| m & 0o7777) },
        )?;
        Ok(())
    })
}

/// utimens' atime half, and the ONLY atime writer (noatime semantics).
pub fn set_atime(db: &mut Db, mount: &MountId, node: &NodeId, ts_ns: i64) -> Result<()> {
    db.txn(|db| {
        require_mount(db, mount, true)?;
        db.run(SET_ATIME, named_params! { ":node": node, ":ts": ts_ns })?;
        Ok(())
    })
}

/// Stamp a node's `modified_at` directly (the FUSE utimens path). An unknown
/// id is tolerated as a no-op so callers may stamp optimistically.
pub fn set_mtime(db: &mut Db, mount: &MountId, node: &NodeId, ts_ns: i64) -> Result<()> {
    db.txn(|db| {
        require_mount(db, mount, true)?;
        db.run(
            SET_MODIFIED_AT,
            named_params! { ":node": node, ":ts": ts_ns },
        )?;
        Ok(())
    })
}

pub fn set_xattr(db: &mut Db, mount: &MountId, path: &str, name: &str, value: &[u8]) -> Result<()> {
    db.txn(|db| {
        let m = require_mount(db, mount, true)?;
        let node = resolve(db, &m.mount_point, path)?.node;
        db.run(
            XATTR_SET,
            named_params! { ":node": node, ":name": name, ":value": value },
        )?;
        Ok(())
    })
}

/// The attribute's bytes, or `None` if unset (a host maps that to ENODATA).
pub fn get_xattr(db: &mut Db, mount: &MountId, path: &str, name: &str) -> Result<Option<Vec<u8>>> {
    let m = require_mount(db, mount, false)?;
    let node = resolve(db, &m.mount_point, path)?.node;
    db.one(
        XATTR_GET,
        named_params! { ":node": node, ":name": name },
        |r| Ok(r.get("value")?),
    )
}

pub fn list_xattrs(db: &mut Db, mount: &MountId, path: &str) -> Result<Vec<String>> {
    let m = require_mount(db, mount, false)?;
    let node = resolve(db, &m.mount_point, path)?.node;
    db.all(XATTR_LIST, named_params! { ":node": node }, |r| {
        Ok(r.get("name")?)
    })
}

/// `true` if removed; `false` if the attribute did not exist (ENODATA).
pub fn remove_xattr(db: &mut Db, mount: &MountId, path: &str, name: &str) -> Result<bool> {
    db.txn(|db| {
        let m = require_mount(db, mount, true)?;
        let node = resolve(db, &m.mount_point, path)?.node;
        Ok(db.run(XATTR_REMOVE, named_params! { ":node": node, ":name": name })? > 0)
    })
}

/// Replace the NODE-6 map wholesale on a container or a leaf; empty clears
/// it. Does NOT bump `modified_at` — annotation is not content.
pub fn set_metadata(
    db: &mut Db,
    mount: &MountId,
    path: &str,
    metadata: &BTreeMap<String, String>,
) -> Result<()> {
    db.txn(|db| {
        let m = require_mount(db, mount, true)?;
        let node = resolve(db, &m.mount_point, path)?.node;
        if lock_held(db, &node, mount)? {
            return Err(FsError::LockHeld { node: node.0 });
        }
        db.run(
            SET_METADATA,
            named_params! { ":node": node, ":metadata": meta_to_json(Some(metadata)) },
        )?;
        Ok(())
    })
}

/// CV-6 keep-last-N policy for superseded versions; `None` keeps all.
/// Enforced only by `prune_content`, never by the write path.
pub fn set_retention(db: &mut Db, mount: &MountId, path: &str, keep: Option<i64>) -> Result<()> {
    db.txn(|db| {
        let m = require_mount(db, mount, true)?;
        let found = resolve(db, &m.mount_point, path)?;
        if found.kind == NodeType::Container {
            return Err(FsError::NotAnEntry { node: found.node.0 });
        }
        db.run(
            SET_RETENTION_KEEP,
            named_params! { ":node": found.node, ":keep": keep },
        )?;
        Ok(())
    })
}

/// The spec's `move` (a Rust keyword): archive the placement `src` walked
/// and create a new edge under `dst`'s parent, renaming if the final name
/// differs (OP-3). `modified_at` is untouched — placement is not content.
pub fn move_(db: &mut Db, mount: &MountId, src: &str, dst: &str) -> Result<()> {
    db.txn(|db| {
        let m = require_mount(db, mount, true)?;
        let found = resolve(db, &m.mount_point, src)?;
        if lock_held(db, &found.node, mount)? {
            return Err(FsError::LockHeld { node: found.node.0 });
        }
        let src_parent = resolve_parent(db, &m.mount_point, src)?;
        let parent = resolve_parent(db, &m.mount_point, dst)?;
        require_name(&parent.name)?;
        if found.kind == NodeType::Container {
            let cycles: Option<i64> = db.scalar(
                CHECK_CYCLE,
                named_params! { ":moving": found.node, ":new_parent": parent.container },
            )?;
            if cycles.unwrap_or(0) != 0 {
                return Err(FsError::WouldCycle {
                    moving: found.node.0,
                    new_parent: parent.container.0,
                });
            }
        }
        // archive THE placement the src path walked (D-5): with hardlinks a
        // node can hold several, and rename(2) moves one directory entry
        let moved = db.run(
            ARCHIVE_PLACEMENT,
            named_params! { ":parent": src_parent.container, ":node": found.node, ":name": src_parent.name },
        )?;
        if moved == 0 {
            return Err(FsError::not_found(format!(
                "node {} has no active placement to move",
                found.node
            )));
        }
        let own_name: Option<String> =
            db.one(GET_NODE, named_params! { ":node": found.node }, |r| Ok(r.get("name")?))?;
        let renamed = own_name.is_some_and(|n| n != parent.name);
        let override_name = if renamed { Some(parent.name.as_str()) } else { None };
        link_child(db, &m, &parent.container, &found.node, override_name)?;
        if renamed {
            // keep the common (sole-placement) case era-1-identical: the
            // node's own name follows the move; hardlinked nodes keep theirs
            db.run(RENAME_NODE_IF_SOLE, named_params! { ":node": found.node, ":name": parent.name })?;
        }
        Ok(())
    })
}

// ---------------------------------------------------------------------------
// depth
// ---------------------------------------------------------------------------

/// Create an entry inside the caller's transaction (shared with
/// `open_write`, which creates on a miss).
pub(crate) fn create_entry_internal(
    db: &mut Db,
    mount: &MountId,
    path: &str,
    data: Option<&[u8]>,
) -> Result<NodeId> {
    let m = require_mount(db, mount, true)?;
    let parent = resolve_parent(db, &m.mount_point, path)?;
    require_name(&parent.name)?;
    let node = new_node(db, &m, NodeType::Entry, &parent.name, None, None, None)?;
    // Establish content at creation: empty => version 0 / zero chunks; with
    // data => version 1 staged. INSERTs, so modified_at stays == created_at.
    put_initial_content(db, &m, &node, data.unwrap_or(&[]))?;
    link_child(db, &m, &parent.container, &node, None)?;
    Ok(node)
}
