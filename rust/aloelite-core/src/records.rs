//! Record models: the plain-data return shapes of `mount-api.yaml`'s
//! `records` section.
//!
//! What the operations return and what a binding projects onto its own
//! idiom (a JS object, an FFI struct, a FUSE attr). Deliberately dumb: no
//! behavior, no database access. Every record serializes with `serde` so
//! the conformance runner can compare it field-by-field without knowing its
//! type, and so a wasm binding can hand it to JavaScript as-is.
//!
//! `from_row` applies the database→model conventions in one place — SQLite
//! has no bool (0/1 integers), emits enum tokens as text, and hands NODE-6
//! metadata back as a JSON string — rather than scattering them through the
//! operations.

use std::collections::BTreeMap;

use rusqlite::Row;
use serde::Serialize;
use serde::ser::SerializeStruct;

use crate::errors::{FsError, Result};
use crate::types::{EdgeId, LockId, MountId, MountState, NodeId, NodeType, Timestamp, VolumeId};

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct VolumeInfo {
    pub id: VolumeId,
    pub name: Option<String>,
    pub root: Option<NodeId>,
    pub api_version: i64,
    pub created_at: Timestamp,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct NodeInfo {
    pub id: NodeId,
    #[serde(rename = "type")]
    pub kind: NodeType,
    pub name: String,
    pub created_at: Timestamp,
    /// The node's own content/metadata change, NOT placement: a move does not
    /// bump it.
    pub modified_at: Timestamp,
    pub volume: Option<VolumeId>,
    /// Materialized content total; `None` for a container.
    pub size: Option<i64>,
    /// CV-3 committed version pointer. Advances on every commit, so unlike
    /// `modified_at` it cannot alias two writes: a strong HTTP validator.
    /// `None` for anything with no content row.
    pub version: Option<i64>,
    /// NODE-6 shallow annotation map; empty when unset, never null.
    pub metadata: BTreeMap<String, String>,
    pub uid: Option<i64>,
    pub gid: Option<i64>,
    pub mode: Option<i64>,
    pub atime: Option<Timestamp>,
    pub ctime: Option<Timestamp>,
    /// DERIVED count of active placements; >1 means hardlinked.
    pub nlink: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DirEntry {
    pub node: NodeId,
    pub name: String,
    pub kind: NodeType,
    pub visible: bool,
    pub edge: EdgeId,
    /// The listing context: the container path `list` was called with,
    /// stamped by the host at fetch time. Not a column — where the entry was
    /// SEEN, since placement can change and NODE-5 allows several names.
    pub current_directory: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct MountInfo {
    pub id: MountId,
    pub volume: VolumeId,
    pub mount_point: NodeId,
    /// The mount point's volume-absolute path, recomputed on read; `None`
    /// when the anchor no longer resolves (ACC-5).
    pub mount_path: Option<String>,
    pub state: MountState,
    pub expires_at: Option<Timestamp>,
    pub created_at: Timestamp,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct LockInfo {
    pub id: LockId,
    pub mount: MountId,
    pub node: NodeId,
    pub expires_at: Option<Timestamp>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Anomaly {
    pub kind: String,
    /// A bare string, not a typed id: `health_anomaly` emits the id of
    /// whatever kind of thing the anomaly is about.
    pub id: String,
}

/// Committed-version integrity sweep; empty `problems` means clean.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct VerifyReport {
    pub entries_checked: usize,
    pub chunks_checked: usize,
    pub problems: Vec<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub struct PruneReport {
    pub nodes_pruned: usize,
    pub locks_pruned: usize,
}

/// CV-7: superseded/aborted manifest versions dropped, and pool chunks
/// reclaimed because no retained version referenced them.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub struct ContentPruneReport {
    pub versions_pruned: usize,
    pub chunks_pruned: usize,
}

// ---------------------------------------------------------------------------
// depth: row → record conventions
// ---------------------------------------------------------------------------

impl VolumeInfo {
    pub(crate) fn from_row(r: &Row<'_>) -> Result<Self> {
        Ok(VolumeInfo {
            id: r.get("volume_id")?,
            name: r.get("name")?,
            root: r.get("root_node_id")?,
            api_version: r.get("api_version")?,
            created_at: r.get("created_at")?,
        })
    }
}

impl NodeInfo {
    /// From a `resolution.get_node` row (which already coalesces
    /// `modified_at` → `created_at` and the a/ctime fallbacks in SQL).
    pub(crate) fn from_row(r: &Row<'_>) -> Result<Self> {
        let created_at: Timestamp = r.get("created_at")?;
        let modified_at: Option<Timestamp> = r.get("modified_at")?;
        let raw_meta: Option<String> = r.get("metadata")?;
        let metadata = match raw_meta {
            None => BTreeMap::new(),
            Some(text) => serde_json::from_str(&text).map_err(|e| {
                FsError::corrupt(format!("node metadata is not a {{string:string}} map: {e}"))
            })?,
        };
        let nlink: Option<i64> = r.get("nlink")?;
        Ok(NodeInfo {
            id: r.get("node_id")?,
            kind: r.get("type")?,
            name: r.get("name")?,
            created_at,
            modified_at: modified_at.unwrap_or(created_at),
            volume: r.get("volume_id")?,
            size: r.get("size")?,
            version: r.get("version")?,
            metadata,
            uid: r.get("uid")?,
            gid: r.get("gid")?,
            mode: r.get("mode")?,
            atime: r.get("atime")?,
            ctime: r.get("ctime")?,
            // the reference reads `nlink or 1`: a node with no active
            // placement still reports 1, never 0
            nlink: match nlink {
                Some(n) if n > 0 => n,
                _ => 1,
            },
        })
    }
}

impl DirEntry {
    pub(crate) fn from_row(r: &Row<'_>, current_directory: &str) -> Result<Self> {
        let visible: i64 = r.get("visible")?;
        Ok(DirEntry {
            node: r.get("node_id")?,
            name: r.get("name")?,
            kind: r.get("type")?,
            visible: visible != 0,
            edge: r.get("edge_id")?,
            current_directory: current_directory.to_owned(),
        })
    }

    /// Full path as seen from the listing: `current_directory` joined with
    /// `name`. Contextual to the fetch, not a stored property of the node.
    pub fn path(&self) -> String {
        format!(
            "{}/{}",
            self.current_directory.trim_end_matches('/'),
            self.name
        )
    }
}

// The computed `path` rides along in the serialized form, as the reference's
// `model_dump()` includes it.
impl Serialize for DirEntry {
    fn serialize<S: serde::Serializer>(&self, s: S) -> std::result::Result<S::Ok, S::Error> {
        let mut st = s.serialize_struct("DirEntry", 7)?;
        st.serialize_field("node", &self.node)?;
        st.serialize_field("name", &self.name)?;
        st.serialize_field("type", &self.kind)?;
        st.serialize_field("visible", &self.visible)?;
        st.serialize_field("edge", &self.edge)?;
        st.serialize_field("current_directory", &self.current_directory)?;
        st.serialize_field("path", &self.path())?;
        st.end()
    }
}

impl MountInfo {
    pub(crate) fn from_row(r: &Row<'_>, mount_path: Option<String>) -> Result<Self> {
        Ok(MountInfo {
            id: r.get("mount_id")?,
            volume: r.get("volume_id")?,
            mount_point: r.get("mount_point")?,
            mount_path,
            state: r.get("state")?,
            expires_at: r.get("expires_at")?,
            created_at: r.get("created_at")?,
        })
    }
}

impl Anomaly {
    pub(crate) fn from_row(r: &Row<'_>) -> Result<Self> {
        Ok(Anomaly {
            kind: r.get("kind")?,
            id: r.get("id")?,
        })
    }
}

impl VerifyReport {
    pub fn ok(&self) -> bool {
        self.problems.is_empty()
    }
}
