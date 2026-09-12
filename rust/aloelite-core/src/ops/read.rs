//! Read / resolution (read-only).

use rusqlite::named_params;

use crate::db::Db;
use crate::errors::{FsError, Result};
use crate::records::{DirEntry, NodeInfo};
use crate::resolve::{resolve, split_path};
use crate::templates::resolution::{GET_NODE, LIST_CHILDREN};
use crate::types::{MountId, NodeId, NodeType};

use super::{build_path, require_mount};

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

pub fn stat(db: &mut Db, mount: &MountId, path: &str) -> Result<NodeInfo> {
    let m = require_mount(db, mount, false)?;
    let node = resolve(db, &m.mount_point, path)?.node;
    stat_by_id(db, mount, &node)
}

/// Reaches hidden same-name siblings (NODE-5) that no path resolves to.
pub fn stat_by_id(db: &mut Db, mount: &MountId, node: &NodeId) -> Result<NodeInfo> {
    require_mount(db, mount, false)?;
    db.one(
        GET_NODE,
        named_params! { ":node": node },
        NodeInfo::from_row,
    )?
    .ok_or_else(|| FsError::not_found(format!("node {node} does not exist")))
}

pub fn exists(db: &mut Db, mount: &MountId, path: &str) -> Result<bool> {
    let m = require_mount(db, mount, false)?;
    match resolve(db, &m.mount_point, path) {
        Ok(_) => Ok(true),
        // a missing segment, or descending through a non-container, both
        // mean the path does not resolve to anything
        Err(FsError::NotFound { .. } | FsError::NotAContainer { .. }) => Ok(false),
        Err(e) => Err(e),
    }
}

/// Children of a container with NODE-5 visibility resolved; hidden
/// same-name siblings appear with `visible: false`.
pub fn list(db: &mut Db, mount: &MountId, path: &str) -> Result<Vec<DirEntry>> {
    let m = require_mount(db, mount, false)?;
    let found = resolve(db, &m.mount_point, path)?;
    if found.kind != NodeType::Container {
        return Err(FsError::NotAContainer { node: found.node.0 });
    }
    // stamp the normalized listing path as each entry's fetch context
    let cwd = format!("/{}", split_path(path).join("/"));
    db.all(
        LIST_CHILDREN,
        named_params! { ":container": found.node },
        |r| DirEntry::from_row(r, &cwd),
    )
}

pub fn read_all(db: &mut Db, mount: &MountId, path: &str) -> Result<Vec<u8>> {
    let m = require_mount(db, mount, false)?;
    let found = resolve(db, &m.mount_point, path)?;
    if found.kind == NodeType::Container {
        return Err(FsError::NotAnEntry { node: found.node.0 });
    }
    db.read_content_bytes(&found.node)
}

/// Mount-relative path of a node (`/` = the mount point).
pub fn path_of(db: &mut Db, mount: &MountId, node: &NodeId) -> Result<String> {
    let m = require_mount(db, mount, false)?;
    build_path(db, node, Some(&m.mount_point))
}
