//! The spec's operations, as data.
//!
//! One table rather than fifty methods, for the reason `tests/projection.rs`
//! makes good on: a table can be held against `mount-api.yaml` in both
//! directions, so an operation cannot be added to one and forgotten in the
//! other, and a parameter name cannot drift in silence. Every frontend
//! dispatches from this one table, so they cannot drift from each other
//! either.

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// One operation `call` accepts: its spec name and the argument names it
/// takes — the spec's parameter names, minus `fs`, which is the handle.
pub struct Op {
    pub name: &'static str,
    pub args: &'static [&'static str],
}

/// Every Mount API operation a frontend dispatches, in the spec's groups
/// and order. `open` is not here — opening a volume is how a frontend
/// builds a handle in the first place, and what it takes differs per
/// storage model — and the session `close` is the handle's own method; the
/// `close` below is the streaming one.
pub const OPS: &[Op] = &[
    // -- session -----------------------------------------------------------
    Op {
        name: "create_volume",
        args: &["name", "chunk_size", "pin", "enc_mode"],
    },
    Op {
        name: "change_pin",
        args: &["volume", "old_pin", "new_pin"],
    },
    Op {
        name: "list_volumes",
        args: &[],
    },
    Op {
        name: "mount",
        args: &[
            "volume",
            "at",
            "ttl_ms",
            "pin",
            "access",
            "principal",
            "allow_overlap",
        ],
    },
    Op {
        name: "unmount",
        args: &["mount"],
    },
    Op {
        name: "renew_mount",
        args: &["mount", "ttl_ms"],
    },
    Op {
        name: "mount_info",
        args: &["mount"],
    },
    Op {
        name: "list_mounts",
        args: &["volume", "include_unmounted"],
    },
    // -- structural --------------------------------------------------------
    Op {
        name: "create_container",
        args: &["mount", "path"],
    },
    Op {
        name: "create_entry",
        args: &["mount", "path", "data"],
    },
    Op {
        name: "write_all",
        args: &["mount", "path", "data"],
    },
    Op {
        name: "append",
        args: &["mount", "path", "data"],
    },
    Op {
        name: "truncate",
        args: &["mount", "path", "size"],
    },
    Op {
        name: "write_range",
        args: &["mount", "path", "offset", "data"],
    },
    Op {
        name: "link",
        args: &["mount", "from", "to"],
    },
    Op {
        name: "create_special",
        args: &["mount", "path", "type", "data"],
    },
    Op {
        name: "set_owner",
        args: &["mount", "path", "uid", "gid", "mode"],
    },
    Op {
        name: "set_atime",
        args: &["mount", "node", "ts_ns"],
    },
    Op {
        name: "set_xattr",
        args: &["mount", "path", "name", "value"],
    },
    Op {
        name: "get_xattr",
        args: &["mount", "path", "name"],
    },
    Op {
        name: "list_xattrs",
        args: &["mount", "path"],
    },
    Op {
        name: "remove_xattr",
        args: &["mount", "path", "name"],
    },
    Op {
        name: "set_metadata",
        args: &["mount", "path", "metadata"],
    },
    Op {
        name: "set_mtime",
        args: &["mount", "node", "ts_ns"],
    },
    Op {
        name: "set_retention",
        args: &["mount", "path", "keep"],
    },
    Op {
        name: "move",
        args: &["mount", "from", "to"],
    },
    Op {
        name: "copy",
        args: &["mount", "from", "to"],
    },
    Op {
        name: "rename",
        args: &["mount", "path", "name"],
    },
    Op {
        name: "remove",
        args: &["mount", "path"],
    },
    Op {
        name: "remove_recursive",
        args: &["mount", "path"],
    },
    Op {
        name: "pack",
        args: &["mount", "path"],
    },
    Op {
        name: "unpack",
        args: &["mount", "path"],
    },
    // -- read --------------------------------------------------------------
    Op {
        name: "resolve",
        args: &["mount", "path"],
    },
    Op {
        name: "path_of",
        args: &["mount", "node"],
    },
    Op {
        name: "stat",
        args: &["mount", "path"],
    },
    Op {
        name: "stat_by_id",
        args: &["mount", "node"],
    },
    Op {
        name: "exists",
        args: &["mount", "path"],
    },
    Op {
        name: "list",
        args: &["mount", "path"],
    },
    Op {
        name: "read_all",
        args: &["mount", "path"],
    },
    // -- locking -----------------------------------------------------------
    Op {
        name: "lock",
        args: &["mount", "path", "ttl_ms"],
    },
    Op {
        name: "unlock",
        args: &["mount", "lock"],
    },
    Op {
        name: "renew_lock",
        args: &["mount", "lock", "ttl_ms"],
    },
    // -- streaming ---------------------------------------------------------
    Op {
        name: "open_read",
        args: &["mount", "path"],
    },
    Op {
        name: "open_write",
        args: &["mount", "path", "mode", "lock"],
    },
    Op {
        name: "read",
        args: &["fd", "len"],
    },
    Op {
        name: "write",
        args: &["fd", "data"],
    },
    Op {
        name: "seek",
        args: &["fd", "offset", "whence"],
    },
    Op {
        name: "tell",
        args: &["fd"],
    },
    Op {
        name: "close",
        args: &["fd"],
    },
    Op {
        name: "abort",
        args: &["fd"],
    },
    // -- maintenance -------------------------------------------------------
    Op {
        name: "prune",
        args: &["volume"],
    },
    Op {
        name: "prune_content",
        args: &["volume"],
    },
    Op {
        name: "verify",
        args: &["mount", "deep"],
    },
    Op {
        name: "health_check",
        args: &[],
    },
];

/// Operations beyond the spec — each a facade rule the reference applies
/// outside its operation layer, and nothing the engine could not do through
/// [`OPS`]:
///
/// - `resolve_volume_name(name)` → `VolumeId | null`: the reference's rule
///   for a duplicated volume name, the most recently created wins
///   (`created_at`, then id).
pub const EXTRA_OPS: &[Op] = &[Op {
    name: "resolve_volume_name",
    args: &["name"],
}];

/// `create_volume`'s `chunk_size` when the request leaves it out, as the
/// spec declares it.
pub const DEFAULT_CHUNK_SIZE: usize = 1_048_576;
