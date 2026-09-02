//! Path resolution — the first thing every implementation writes, and the
//! thing the whole flat layer is built on.
//!
//! [`resolve`] walks a whole path in ONE query via the
//! `resolution.resolve_path` recursive CTE, starting at the mount's mount
//! point. [`resolve_parent`] is the same walk stopped one segment short,
//! returning (container, final name) for the create / move / rename
//! operations that need "the parent container, keep the final name".
//!
//! Path semantics are decided HERE, once, so every implementation mirrors
//! exactly one set of rules:
//!   * paths are mount-relative; `''` and `'/'` both denote the mount point
//!   * leading/trailing slashes are ignored; empty segments (`//`) collapse
//!   * resolution sees only VISIBLE nodes (NODE-5) — the greatest-uuid7
//!     sibling wins — so hidden same-name siblings are unreachable by path;
//!     `*_by_id` variants exist for those
//!   * a miss at any segment is `not_found`
//!   * a non-final segment that resolves to a leaf is `not_a_container`
//!   * `.` and `..` are ORDINARY NAMES, not navigation. They are never
//!     interpreted, so a path can never climb above the mount point it
//!     started at — which is what confines a subtree mount to its subtree.
//!     That containment is a security boundary.

use rusqlite::named_params;

use crate::db::Db;
use crate::errors::{FsError, Result};
use crate::templates::resolution::{GET_NODE, RESOLVE_PATH};
use crate::types::{NodeId, NodeType};

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Resolved {
    pub node: NodeId,
    pub kind: NodeType,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Parent {
    pub container: NodeId,
    pub name: String,
}

/// Normalize a mount-relative path into clean segments. `''` and `'/'` give
/// an empty list; repeated and trailing slashes collapse. No `.`/`..`
/// handling: they are ordinary names and will simply not be found.
pub fn split_path(path: &str) -> Vec<&str> {
    path.split('/').filter(|s| !s.is_empty()).collect()
}

/// Resolve a full mount-relative path to its node and type, so callers can
/// enforce container/leaf expectations without a second lookup.
pub fn resolve(db: &mut Db, mount_point: &NodeId, path: &str) -> Result<Resolved> {
    let segments = split_path(path);
    if segments.is_empty() {
        // '' or '/' is the mount point itself; report its type for symmetry.
        let kind = db
            .one(GET_NODE, named_params! { ":node": mount_point }, |r| {
                Ok(r.get::<_, NodeType>("type")?)
            })?
            .ok_or_else(|| {
                FsError::not_found(format!("mount point {mount_point} does not exist"))
            })?;
        return Ok(Resolved {
            node: mount_point.clone(),
            kind,
        });
    }
    walk(db, mount_point, &segments)
}

/// Resolve a path to (its parent container, its final name). The parent is
/// walked and must be a container; the final name is NOT looked up (it may
/// not exist yet).
pub fn resolve_parent(db: &mut Db, mount_point: &NodeId, path: &str) -> Result<Parent> {
    let segments = split_path(path);
    let Some((final_name, head)) = segments.split_last() else {
        return Err(FsError::not_found(
            "cannot take the parent of the mount point root",
        ));
    };
    if head.is_empty() {
        // the parent is the mount point itself
        return Ok(Parent {
            container: mount_point.clone(),
            name: (*final_name).to_owned(),
        });
    }
    let found = walk(db, mount_point, head)?;
    if found.kind != NodeType::Container {
        // the deepest head segment resolved to a leaf; walk() only reports
        // not_a_container for segments it had to descend THROUGH
        return Err(FsError::NotAContainer { node: found.node.0 });
    }
    Ok(Parent {
        container: found.node,
        name: (*final_name).to_owned(),
    })
}

// ---------------------------------------------------------------------------
// depth: the one-query walk
// ---------------------------------------------------------------------------

struct WalkRow {
    idx: i64,
    seg: Option<String>,
    parent_id: Option<String>,
    node_id: Option<NodeId>,
    node_type: Option<String>,
}

/// Resolve non-empty `segments` beneath `root` in one query. The CTE records
/// where it stopped, so the last row carries the diagnosis.
fn walk(db: &mut Db, root: &NodeId, segments: &[&str]) -> Result<Resolved> {
    let rows = db.all(
        RESOLVE_PATH,
        named_params! { ":root": root, ":path": segments.join("/") },
        |r| {
            Ok(WalkRow {
                idx: r.get("idx")?,
                seg: r.get("seg")?,
                parent_id: r.get("parent_id")?,
                node_id: r.get("node_id")?,
                node_type: r.get("node_type")?,
            })
        },
    )?;
    // the walk always consumes at least one segment
    let last = rows
        .last()
        .ok_or_else(|| FsError::internal("resolve_path returned no rows"))?;
    let Some(node) = &last.node_id else {
        return Err(FsError::not_found(format!(
            "no visible child {:?} in {}",
            last.seg.as_deref().unwrap_or(""),
            last.parent_id.as_deref().unwrap_or("?")
        )));
    };
    if (last.idx as usize) < segments.len() {
        // stopped early: only a non-container can halt the walk mid-path
        return Err(FsError::NotAContainer {
            node: node.0.clone(),
        });
    }
    let kind = last
        .node_type
        .as_deref()
        .and_then(NodeType::parse)
        .ok_or_else(|| FsError::corrupt(format!("node {node} has an unknown type")))?;
    Ok(Resolved {
        node: node.clone(),
        kind,
    })
}

#[cfg(test)]
mod tests {
    use super::split_path;

    #[test]
    fn split_collapses_slashes_and_keeps_dots_as_names() {
        assert_eq!(split_path(""), Vec::<&str>::new());
        assert_eq!(split_path("/"), Vec::<&str>::new());
        assert_eq!(split_path("//a///b/"), vec!["a", "b"]);
        assert_eq!(split_path("/a/../b"), vec!["a", "..", "b"]);
    }
}
