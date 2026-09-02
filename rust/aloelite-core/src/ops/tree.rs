//! Subtree operations: remove, remove_recursive, and the shared
//! copy / pack / unpack walk.
//!
//! `recursive.enumerate_subtree` gives rows TOP-DOWN in canonical order
//! (depth, edge_id, node_id) over ACTIVE edges only. A parent always precedes
//! its children, so a single forward pass threading an old→new id map
//! suffices: a child attaches to the new id its parent already received. The
//! walk order is a CONFORMANCE REQUIREMENT — every implementation must walk
//! it identically so new-id sequences (and future merkle hashes) match.
//!
//! The MsgPack pack blob is a CROSS-IMPLEMENTATION CONTRACT whose layout,
//! version gate and byte rules live in [`crate::pack`], pinned by
//! `conformance/vectors/pack-v1.json`. What lives here is the walk that
//! feeds the codec — one entry per placement, canonical order — and the id
//! remapping on the way back in.

use std::collections::HashMap;

use rusqlite::named_params;

use crate::db::Db;
use crate::errors::{FsError, Result};
use crate::pack::{self as packfmt, PackNode};
use crate::records::NodeInfo;
use crate::resolve::{resolve, resolve_parent};
use crate::templates::mutation::{
    ARCHIVE_EDGE, ARCHIVE_PLACEMENT, COPY_CHUNK_REFS, CREATE_CONTENT,
};
use crate::templates::recursive::{ARCHIVE_SUBTREE, ENUMERATE_SUBTREE};
use crate::templates::resolution::{GET_ACTIVE_PARENT, GET_CONTENT_META, GET_NODE};
use crate::templates::validation::{CHECK_EMPTY, CHECK_LOCK_HELD_SUBTREE};
use crate::types::{EdgeId, MountId, NodeId, NodeType};

use super::{link_child, lock_held, new_node, put_initial_content, require_mount, require_name};

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// Detach a leaf or empty container: archive the placement the path walked
/// (OP-5). Refuses a non-empty container with `not_empty`.
pub fn remove(db: &mut Db, mount: &MountId, path: &str) -> Result<()> {
    db.txn(|db| {
        let m = require_mount(db, mount, true)?;
        let found = resolve(db, &m.mount_point, path)?;
        if lock_held(db, &found.node, mount)? {
            return Err(FsError::LockHeld { node: found.node.0 });
        }
        if found.kind == NodeType::Container {
            let has_children: Option<i64> =
                db.scalar(CHECK_EMPTY, named_params! { ":container": found.node })?;
            if has_children.unwrap_or(0) != 0 {
                return Err(FsError::NotEmpty { node: found.node.0 });
            }
        }
        // archive THE placement the path walked (D-5): unlink(2) on a
        // hardlinked node removes one directory entry; the node survives,
        // reachable through its other placements, until none remain
        let parent = resolve_parent(db, &m.mount_point, path)?;
        let removed = db.run(
            ARCHIVE_PLACEMENT,
            named_params! { ":parent": parent.container, ":node": found.node, ":name": parent.name },
        )?;
        if removed == 0 {
            return Err(FsError::not_found(format!("node {} has no active placement", found.node)));
        }
        Ok(())
    })
}

/// Archive a whole active subtree in one statement (set-based).
pub fn remove_recursive(db: &mut Db, mount: &MountId, path: &str) -> Result<()> {
    db.txn(|db| {
        let m = require_mount(db, mount, true)?;
        let node = resolve(db, &m.mount_point, path)?.node;
        // Checked over the WHOLE subtree: this destroys every member, so a
        // lock anywhere below is a lock on something this statement would
        // archive. The offender is named -- actionable on a deep tree.
        subtree_unlocked(db, &node, mount)?;
        db.run(ARCHIVE_SUBTREE, named_params! { ":root": node })?;
        Ok(())
    })
}

/// Copy a subtree: fresh ids, source `created_at`/`modified_at`/metadata
/// preserved (OP-4), content re-referenced from the immutable pool rather
/// than re-hashed (dedup preserved). Active edges only.
pub fn copy(db: &mut Db, mount: &MountId, src: &str, dst: &str) -> Result<NodeId> {
    db.txn(|db| {
        let m = require_mount(db, mount, true)?;
        let src_node = resolve(db, &m.mount_point, src)?.node;
        let dst_parent = resolve_parent(db, &m.mount_point, dst)?;
        require_name(&dst_parent.name)?;
        let rows = enumerate_subtree(db, &src_node)?;
        let mut idmap: HashMap<NodeId, NodeId> = HashMap::new();
        let mut new_root: Option<NodeId> = None;
        for r in &rows {
            let info = node_info(db, &r.node_id)?;
            // The EFFECTIVE name at this placement (D-5): a hardlinked node
            // is enumerated once per placement and each may be named
            // differently; the node's own name is only its default.
            let (name, parent): (&str, &NodeId) = match &r.parent_id {
                None => (&dst_parent.name, &dst_parent.container),
                Some(p) => (
                    &r.name,
                    idmap
                        .get(p)
                        .ok_or_else(|| FsError::corrupt("subtree walk saw a child before its parent"))?,
                ),
            };
            let new_id = new_node(
                db,
                &m,
                info.kind,
                name,
                Some(info.created_at),
                Some(info.modified_at),
                Some(&info.metadata),
            )?;
            if info.kind.is_leaf() {
                let (sv, ssize) = db
                    .one(GET_CONTENT_META, named_params! { ":node": r.node_id }, |row| {
                        Ok((row.get::<_, i64>("version")?, row.get::<_, i64>("size")?))
                    })?
                    .unwrap_or((0, 0));
                db.run(
                    CREATE_CONTENT,
                    named_params! { ":node": new_id, ":version": sv, ":size": ssize, ":hash": Option::<Vec<u8>>::None },
                )?;
                if sv > 0 {
                    db.run(
                        COPY_CHUNK_REFS,
                        named_params! { ":dst": new_id, ":dst_version": sv, ":src": r.node_id, ":src_version": sv },
                    )?;
                }
            }
            link_child(db, &m, parent, &new_id, None)?;
            if r.parent_id.is_none() {
                new_root = Some(new_id.clone());
            }
            idmap.insert(r.node_id.clone(), new_id);
        }
        new_root.ok_or_else(|| FsError::corrupt(format!("empty subtree enumeration under {src_node}")))
    })
}

/// Consolidate a container's subtree into a single packed entry that
/// supersedes the original placement (OP-6, TX-2). ACC-11 is checked
/// transitively, as for `remove_recursive`.
pub fn pack(db: &mut Db, mount: &MountId, path: &str) -> Result<NodeId> {
    db.txn(|db| {
        let m = require_mount(db, mount, true)?;
        let found = resolve(db, &m.mount_point, path)?;
        if found.kind != NodeType::Container {
            return Err(FsError::NotAContainer { node: found.node.0 });
        }
        let node = found.node;
        subtree_unlocked(db, &node, mount)?;
        let placement = active_parent(db, &node)?.ok_or_else(|| {
            FsError::not_found(format!("cannot pack {node}: no active placement"))
        })?;
        let pack_name = node_info(db, &node)?.name;

        let rows = enumerate_subtree(db, &node)?;
        let mut index: HashMap<NodeId, i64> = HashMap::new();
        let mut nodes: Vec<PackNode> = Vec::with_capacity(rows.len());
        for r in &rows {
            let info = node_info(db, &r.node_id)?;
            let i = nodes.len() as i64;
            index.insert(r.node_id.clone(), i);
            let p = match &r.parent_id {
                None => -1,
                Some(p) => *index.get(p).ok_or_else(|| {
                    FsError::corrupt("subtree walk saw a child before its parent")
                })?,
            };
            nodes.push(PackNode {
                p,
                t: info.kind.as_str().to_owned(),
                // effective name at this placement, as for copy above
                n: r.name.clone(),
                c: Some(info.created_at),
                m: Some(info.modified_at),
                // NODE-6: carry metadata when present; omit the common empty
                // case to keep the blob small
                x: (!info.metadata.is_empty()).then(|| info.metadata.clone()),
                d: if info.kind.is_leaf() {
                    Some(db.read_content_bytes(&r.node_id)?)
                } else {
                    None
                },
            });
        }
        let blob = packfmt::encode(&nodes);

        // supersede: archive the original subtree, then place the packed
        // entry. The blob flows through the chunker like any payload, so
        // there is no blob-size ceiling.
        db.run(ARCHIVE_SUBTREE, named_params! { ":root": node })?;
        let packed = new_node(db, &m, NodeType::Entry, &pack_name, None, None, None)?;
        put_initial_content(db, &m, &packed, &blob)?;
        link_child(db, &m, &placement.parent, &packed, None)?;
        Ok(packed)
    })
}

/// Restore a packed entry's subtree, superseding the packed entry (OP-7).
/// Serialized refs are remapped to freshly minted ids.
pub fn unpack(db: &mut Db, mount: &MountId, path: &str) -> Result<()> {
    db.txn(|db| {
        let m = require_mount(db, mount, true)?;
        let found = resolve(db, &m.mount_point, path)?;
        if found.kind == NodeType::Container {
            return Err(FsError::NotAnEntry { node: found.node.0 });
        }
        let node = found.node;
        // ACC-11: unpack archives the packed entry's placement -- the same
        // change remove() makes, non-transitive since it is a single leaf.
        if lock_held(db, &node, mount)? {
            return Err(FsError::LockHeld { node: node.0 });
        }
        let blob = db.read_content_bytes(&node)?;
        // the version gate lives in the codec: a newer blob is unsupported,
        // a malformed one corrupt, before a single node is read
        let nodes = packfmt::decode(&blob)?;

        let placement = active_parent(db, &node)?.ok_or_else(|| {
            FsError::not_found(format!("packed entry {node} has no active placement"))
        })?;
        db.run(ARCHIVE_EDGE, named_params! { ":edge": placement.edge })?;
        let mut idmap: Vec<NodeId> = Vec::with_capacity(nodes.len());
        for pn in &nodes {
            let kind = NodeType::parse(&pn.t).ok_or_else(|| {
                FsError::corrupt(format!("pack node has unknown type {:?}", pn.t))
            })?;
            let target_parent: NodeId = if pn.p == -1 {
                placement.parent.clone()
            } else {
                usize::try_from(pn.p)
                    .ok()
                    .and_then(|i| idmap.get(i))
                    .cloned()
                    .ok_or_else(|| {
                        FsError::corrupt(format!("pack node refers to parent {} before it", pn.p))
                    })?
            };
            // NODE-6: tolerant read -- a blob written before metadata existed
            // has no "x" key, which restores as an empty map
            let new_id = new_node(db, &m, kind, &pn.n, pn.c, pn.m, pn.x.as_ref())?;
            if kind.is_leaf() {
                put_initial_content(db, &m, &new_id, pn.d.as_deref().unwrap_or(&[]))?;
            }
            link_child(db, &m, &target_parent, &new_id, None)?;
            idmap.push(new_id);
        }
        Ok(())
    })
}

// ---------------------------------------------------------------------------
// depth: the walk primitives and the pack codec
// ---------------------------------------------------------------------------

struct SubtreeRow {
    node_id: NodeId,
    parent_id: Option<NodeId>,
    /// The effective name at this placement: `coalesce(edge.name, node.name)`.
    name: String,
}

fn enumerate_subtree(db: &mut Db, root: &NodeId) -> Result<Vec<SubtreeRow>> {
    db.all(ENUMERATE_SUBTREE, named_params! { ":root": root }, |r| {
        Ok(SubtreeRow {
            node_id: r.get("node_id")?,
            parent_id: r.get("parent_id")?,
            name: r.get("name")?,
        })
    })
}

fn node_info(db: &mut Db, node: &NodeId) -> Result<NodeInfo> {
    db.one(
        GET_NODE,
        named_params! { ":node": node },
        NodeInfo::from_row,
    )?
    .ok_or_else(|| FsError::corrupt(format!("subtree member {node} has no node row")))
}

struct Placement {
    parent: NodeId,
    edge: EdgeId,
}

fn active_parent(db: &mut Db, node: &NodeId) -> Result<Option<Placement>> {
    db.one(GET_ACTIVE_PARENT, named_params! { ":node": node }, |r| {
        Ok(Placement {
            parent: r.get("parent_id")?,
            edge: r.get("edge_id")?,
        })
    })
}

/// `lock_held` over the whole subtree under `root`, naming the offender.
fn subtree_unlocked(db: &mut Db, root: &NodeId, mount: &MountId) -> Result<()> {
    let locked: Option<NodeId> = db.scalar(
        CHECK_LOCK_HELD_SUBTREE,
        named_params! { ":root": root, ":mount": mount },
    )?;
    match locked {
        Some(node) => Err(FsError::LockHeld { node: node.0 }),
        None => Ok(()),
    }
}
