//! The flat Mount API operation layer — the FFI surface.
//!
//! Every operation is a free function over a [`Db`] (the current, transient
//! connection), a [`MountId`] (the durable identity that brokers access), and
//! plain values; it returns a record from [`crate::records`] or an
//! [`FsError`]. No object state, no ergonomic sugar — a facade can be built
//! on top of this in any binding. One transaction per action ([`Db::txn`]);
//! each mutating operation is atomic on its own.
//!
//! The reference implementation (`aloelite/operations.py`) is mirrored
//! function-for-function, in the spec's groups, and the conformance suite
//! drives both. The two pieces with genuine host logic — path resolution
//! ([`crate::resolve`]) and the copy/pack/unpack subtree walk ([`tree`]) —
//! are the parts most worth pinning in conformance.

use std::collections::BTreeMap;

use rusqlite::named_params;

use crate::crypto::EncMode;
use crate::db::Db;
use crate::errors::{FsError, Result};
use crate::templates::mutation::{CREATE_CONTENT, CREATE_EDGE, CREATE_NODE};
use crate::templates::resolution::{GET_VALID_MOUNT, PATH_OF};
use crate::templates::validation::CHECK_LOCK_HELD;
use crate::types::{Access, EdgeId, MountId, NodeId, NodeType, VolumeId};

// ---------------------------------------------------------------------------
// surface: the operations, in mount-api.yaml's groups
// ---------------------------------------------------------------------------

pub use content::{append, truncate, write_all, write_range};
pub use locking::{lock, renew_lock, unlock};
pub use maintenance::{health_check, prune, prune_content, verify};
pub use read::{exists, list, path_of, read_all, stat, stat_by_id};
pub use session::{
    MountOptions, change_pin, create_volume, list_mounts, list_volumes, mount, mount_info,
    renew_mount, unmount,
};
pub use streaming::{open_read, open_write};
pub use structural::{
    create_container, create_entry, create_special, get_xattr, link, list_xattrs, move_,
    remove_xattr, rename, set_atime, set_metadata, set_mtime, set_owner, set_retention, set_xattr,
};
pub use tree::{PACK_FMT, PACK_VER, copy, pack, remove, remove_recursive, unpack};

mod content;
mod locking;
mod maintenance;
mod read;
mod session;
mod streaming;
mod structural;
mod tree;

// ---------------------------------------------------------------------------
// depth: the mount precondition and the helpers every group shares
// ---------------------------------------------------------------------------

/// A validated mount: the anchor and volume every operation works within.
pub(crate) struct Mount {
    pub mount_point: NodeId,
    pub volume: VolumeId,
}

/// Resolve a mount to its anchor + volume, or `mount_invalid`.
///
/// A mount is untrusted-until-validated: it may have been unmounted or
/// expired, possibly from another connection, so this runs first in every
/// operation (ACC-1/4/5). It is also where the connection's cipher is checked
/// against the volume (ENC-3): the cipher lives on the CONNECTION while
/// volumes live in the FILE, so the two can disagree — attaching to a mount
/// installs no cipher, and mounting a second volume replaces the one already
/// installed. Refusing here makes that a closed error instead of ciphertext
/// served as plaintext or plaintext written into an encrypted volume.
pub(crate) fn require_mount(db: &mut Db, mount: &MountId, write: bool) -> Result<Mount> {
    struct Row {
        volume: VolumeId,
        mount_point: NodeId,
        access: Access,
        enc_mode: String,
    }
    let row = db
        .one(GET_VALID_MOUNT, named_params! { ":mount": mount }, |r| {
            Ok(Row {
                volume: r.get("volume_id")?,
                mount_point: r.get("mount_point")?,
                access: r.get("access")?,
                enc_mode: r.get("enc_mode")?,
            })
        })?
        .ok_or_else(|| FsError::MountInvalid {
            msg: format!("mount {mount} is not valid"),
        })?;
    let enc_mode = EncMode::parse(&row.enc_mode).ok_or_else(|| {
        FsError::corrupt(format!(
            "volume {} has enc_mode {:?}",
            row.volume, row.enc_mode
        ))
    })?;
    let encrypted = enc_mode != EncMode::None;
    if encrypted != db.cipher.encrypts() {
        return Err(FsError::EncryptionRequired {
            msg: format!(
                "volume is {:?} but this connection has {} encryption key installed; mount the volume (with a PIN if it is encrypted) before operating on it",
                row.enc_mode,
                if encrypted { "no" } else { "an" }
            ),
        });
    }
    if write && row.access == Access::Ro {
        return Err(FsError::ReadOnly);
    }
    Ok(Mount {
        mount_point: row.mount_point,
        volume: row.volume,
    })
}

/// Host→SQL: a shallow {string:string} map serializes to a JSON string for
/// `jsonb()` storage; an empty or absent map stays NULL (NODE-6: NULL == {}).
pub(crate) fn meta_to_json(metadata: Option<&BTreeMap<String, String>>) -> Option<String> {
    match metadata {
        Some(m) if !m.is_empty() => Some(serde_json::to_string(m).expect("string map serializes")),
        _ => None,
    }
}

pub(crate) fn new_node(
    db: &mut Db,
    m: &Mount,
    kind: NodeType,
    name: &str,
    created_at: Option<i64>,
    modified_at: Option<i64>,
    metadata: Option<&BTreeMap<String, String>>,
) -> Result<NodeId> {
    // host-supplied, never SQL-side; `None` only arrives from callers
    // preserving a source value
    let created_at = created_at.unwrap_or_else(|| db.now_ns());
    let id = db.create_monotonic(
        CREATE_NODE,
        Some(&m.volume),
        named_params! {
            ":type": kind,
            ":name": name,
            ":created_at": created_at,
            ":modified_at": modified_at,
            ":volume": m.volume,
            ":metadata": meta_to_json(metadata),
        },
    )?;
    Ok(NodeId(id))
}

/// Place `child` under `parent`. `name` is the D-5 placement-name override —
/// `Some` only for hardlinks minted under a different name; ordinary
/// placements resolve through the node's own name.
pub(crate) fn link_child(
    db: &mut Db,
    m: &Mount,
    parent: &NodeId,
    child: &NodeId,
    name: Option<&str>,
) -> Result<EdgeId> {
    let id = db.create_monotonic(
        CREATE_EDGE,
        Some(&m.volume),
        named_params! { ":from_id": parent, ":to_id": child, ":volume": m.volume, ":name": name },
    )?;
    Ok(EdgeId(id))
}

/// Establish a leaf's content at birth via INSERTs (`create_content` +
/// staged chunks). Used by create/pack/unpack — never bumps `modified_at`.
/// Empty data ⇒ committed version 0 with zero chunks; otherwise version 1.
pub(crate) fn put_initial_content(
    db: &mut Db,
    m: &Mount,
    node: &NodeId,
    data: &[u8],
) -> Result<()> {
    let version: i64 = if data.is_empty() { 0 } else { 1 };
    db.run(
        CREATE_CONTENT,
        named_params! {
            ":node": node,
            ":version": version,
            ":size": data.len() as i64,
            ":hash": Option::<Vec<u8>>::None,
        },
    )?;
    if !data.is_empty() {
        db.stage_chunks(node, version, &m.volume, data)?;
    }
    Ok(())
}

pub(crate) fn require_name(name: &str) -> Result<()> {
    if name.is_empty() {
        return Err(FsError::Nameless);
    }
    Ok(())
}

/// Whether another mount holds a valid lock on `node` (ACC-6/7/11).
pub(crate) fn lock_held(db: &mut Db, node: &NodeId, mount: &MountId) -> Result<bool> {
    let held: Option<i64> = db.scalar(
        CHECK_LOCK_HELD,
        named_params! { ":node": node, ":mount": mount },
    )?;
    Ok(held.unwrap_or(0) != 0)
}

/// Volume-absolute path of a node (root = `/`). Used for `mount_path`.
pub(crate) fn abs_path(db: &mut Db, node: &NodeId) -> Result<String> {
    build_path(db, node, None)
}

/// Walk `path_of` upward and assemble the path down from `stop_at` (the
/// mount point) or from the volume root when `None`.
pub(crate) fn build_path(db: &mut Db, node: &NodeId, stop_at: Option<&NodeId>) -> Result<String> {
    struct Up {
        node_id: NodeId,
        name: String,
        is_root: bool,
    }
    let rows = db.all(PATH_OF, named_params! { ":node": node }, |r| {
        Ok(Up {
            node_id: r.get("node_id")?,
            name: r.get("name")?,
            is_root: r.get::<_, i64>("is_root")? != 0,
        })
    })?;
    let Some(last) = rows.last() else {
        return Err(FsError::not_found(format!("node {node} does not exist")));
    };
    if !last.is_root {
        // walk hit the depth bound or a null parent without reaching a root
        return Err(FsError::corrupt(format!(
            "ancestor chain of {node} does not reach a volume root"
        )));
    }
    let mut names: Vec<&str> = Vec::new();
    let mut reached = stop_at.is_none();
    for r in &rows {
        if let Some(stop) = stop_at
            && &r.node_id == stop
        {
            reached = true;
            break;
        }
        if stop_at.is_none() && r.is_root {
            break; // root is '/', don't include its name
        }
        names.push(&r.name);
    }
    if !reached {
        return Err(FsError::not_found(format!(
            "node {node} is not under the mount point"
        )));
    }
    names.reverse();
    Ok(format!("/{}", names.join("/")))
}
