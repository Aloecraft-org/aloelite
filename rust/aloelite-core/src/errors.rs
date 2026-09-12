//! The closed error set of `mount-api.yaml`, as one enum.
//!
//! Every operation's `raises` draws from these variants and nothing else;
//! `code()` is the spec's snake_case name, which is what the conformance
//! runner compares (`raises: lock_held`) and what a binding maps onto its
//! own vocabulary (errno for FUSE, status codes for HTTP). The spec's set is
//! closed — adding a variant here means adding it to `mount-api.yaml` and to
//! every other implementation, in that order.
//!
//! Variants carry the context the reference attaches as keyword arguments
//! (`NotFound(node=...)`, `LockHeld(node=...)`); it is diagnostic, never
//! matched on. Three variants exist only on this side of the boundary:
//! [`FsError::Sqlite`] wraps the driver, [`FsError::Internal`] is a contract
//! violation this crate detected in itself, and [`FsError::Usage`] is a
//! caller-contract violation the reference reports as a `ValueError` (a
//! closed descriptor, a negative seek). None has a spec code because none
//! is supposed to reach a caller who did nothing wrong; a binding maps the
//! last to its own invalid-argument error (EINVAL, TypeError).

use crate::crypto::BadKey;

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// The result type every fallible engine call returns.
pub type Result<T> = std::result::Result<T, FsError>;

#[derive(Debug, thiserror::Error)]
pub enum FsError {
    // -- the closed set, in mount-api.yaml's order -------------------------
    #[error("not found: {msg}")]
    NotFound { msg: String },
    #[error("not a container: {node}")]
    NotAContainer { node: String },
    #[error("not an entry: {node}")]
    NotAnEntry { node: String },
    #[error("a name was required and was empty")]
    Nameless,
    #[error("reparent would place {moving} under its own descendant {new_parent}")]
    WouldCycle { moving: String, new_parent: String },
    #[error("operation would span volumes")]
    VolumeMismatch,
    #[error("container {node} is not empty")]
    NotEmpty { node: String },
    #[error("mount invalid: {msg}")]
    MountInvalid { msg: String },
    #[error("mount point {node} is archived")]
    MountPointArchived { node: String },
    #[error("lock held on {node} by another mount")]
    LockHeld { node: String },
    #[error("lock invalid: {msg}")]
    LockInvalid { msg: String },
    #[error("corrupt: {msg}")]
    Corrupt { msg: String },
    #[error("unsupported: {msg}")]
    Unsupported { msg: String },
    #[error("container already exists at {name}")]
    ContainerExists { name: String },
    #[error("already exists: {msg}")]
    AlreadyExists { msg: String },
    #[error("read-only mount")]
    ReadOnly,
    #[error("an rw mount already covers this subtree (pass allow_overlap to stack)")]
    MountConflict { conflicting_mount: String },
    #[error("bad key: the supplied PIN or token did not unlock the volume")]
    BadKey,
    #[error("encryption required: {msg}")]
    EncryptionRequired { msg: String },

    // -- this side of the boundary only -----------------------------------
    #[error("sqlite: {0}")]
    Sqlite(#[from] rusqlite::Error),
    #[error("internal contract violation: {0}")]
    Internal(String),
    #[error("caller contract violated: {0}")]
    Usage(String),
}

impl FsError {
    /// The spec's error code, or `None` for the two engine-side variants.
    pub fn code(&self) -> Option<&'static str> {
        use FsError::*;
        Some(match self {
            NotFound { .. } => "not_found",
            NotAContainer { .. } => "not_a_container",
            NotAnEntry { .. } => "not_an_entry",
            Nameless => "nameless",
            WouldCycle { .. } => "would_cycle",
            VolumeMismatch => "volume_mismatch",
            NotEmpty { .. } => "not_empty",
            MountInvalid { .. } => "mount_invalid",
            MountPointArchived { .. } => "mount_point_archived",
            LockHeld { .. } => "lock_held",
            LockInvalid { .. } => "lock_invalid",
            Corrupt { .. } => "corrupt",
            Unsupported { .. } => "unsupported",
            ContainerExists { .. } => "container_exists",
            AlreadyExists { .. } => "already_exists",
            ReadOnly => "read_only",
            MountConflict { .. } => "mount_conflict",
            BadKey => "bad_key",
            EncryptionRequired { .. } => "encryption_required",
            Sqlite(_) | Internal(_) | Usage(_) => return None,
        })
    }

    /// Every spec code, in spec order. The projection test holds this
    /// against `mount-api.yaml`'s `errors:` block in both directions.
    pub const CODES: &'static [&'static str] = &[
        "not_found",
        "not_a_container",
        "not_an_entry",
        "nameless",
        "would_cycle",
        "volume_mismatch",
        "not_empty",
        "mount_invalid",
        "mount_point_archived",
        "lock_held",
        "lock_invalid",
        "corrupt",
        "unsupported",
        "container_exists",
        "already_exists",
        "read_only",
        "mount_conflict",
        "bad_key",
        "encryption_required",
    ];

    pub fn not_found(msg: impl Into<String>) -> Self {
        FsError::NotFound { msg: msg.into() }
    }
    pub fn corrupt(msg: impl Into<String>) -> Self {
        FsError::Corrupt { msg: msg.into() }
    }
    pub fn unsupported(msg: impl Into<String>) -> Self {
        FsError::Unsupported { msg: msg.into() }
    }
    pub fn internal(msg: impl Into<String>) -> Self {
        FsError::Internal(msg.into())
    }
    pub fn usage(msg: impl Into<String>) -> Self {
        FsError::Usage(msg.into())
    }
}

impl From<BadKey> for FsError {
    fn from(_: BadKey) -> Self {
        FsError::BadKey
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_spec_variant_reports_its_code_and_codes_are_unique() {
        let mut seen = std::collections::HashSet::new();
        for c in FsError::CODES {
            assert!(seen.insert(*c), "duplicate code {c}");
        }
        assert_eq!(FsError::Nameless.code(), Some("nameless"));
        assert_eq!(FsError::BadKey.code(), Some("bad_key"));
        assert_eq!(FsError::from(BadKey).code(), Some("bad_key"));
        assert_eq!(FsError::internal("x").code(), None);
        assert_eq!(FsError::usage("x").code(), None);
    }
}
