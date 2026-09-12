//! Vocabulary: opaque id scalars and closed enums.
//!
//! The Rust projection of the `scalars` and `enums` sections of
//! `aloelite/config/mount-api.yaml`. Ids are uuid7 strings underneath, modeled
//! as distinct newtypes so a `MountId` cannot stand in for a `NodeId` — the
//! operations are fifty functions over positional string arguments, which is
//! exactly where that mistake would otherwise hide. Each binds directly as a
//! SQL parameter and reads directly from a row.
//!
//! Enum values are the exact lowercase tokens stored in the database
//! (`node.type`, `mount.state`, `mount.access`); the guard triggers in
//! `schema.sql` refuse anything else at insert. Renaming one is a schema
//! change, not a refactor.

use std::fmt;

use rusqlite::types::{FromSql, FromSqlError, FromSqlResult, ToSql, ToSqlOutput, ValueRef};
use serde::{Deserialize, Serialize};

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// Unix epoch nanoseconds (era 2; era 1 stored milliseconds).
pub type Timestamp = i64;

macro_rules! id_type {
    ($(#[$meta:meta])* $name:ident) => {
        $(#[$meta])*
        #[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
        #[serde(transparent)]
        pub struct $name(pub String);

        impl $name {
            pub fn as_str(&self) -> &str {
                &self.0
            }
        }
        impl fmt::Display for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str(&self.0)
            }
        }
        impl AsRef<str> for $name {
            fn as_ref(&self) -> &str {
                &self.0
            }
        }
        impl From<String> for $name {
            fn from(s: String) -> Self {
                $name(s)
            }
        }
        impl From<&str> for $name {
            fn from(s: &str) -> Self {
                $name(s.to_owned())
            }
        }
        impl ToSql for $name {
            fn to_sql(&self) -> rusqlite::Result<ToSqlOutput<'_>> {
                Ok(ToSqlOutput::Borrowed(ValueRef::Text(self.0.as_bytes())))
            }
        }
        impl FromSql for $name {
            fn column_result(v: ValueRef<'_>) -> FromSqlResult<Self> {
                v.as_str().map(|s| $name(s.to_owned()))
            }
        }
    };
}

id_type!(
    /// uuid7 of a node.
    NodeId
);
id_type!(
    /// uuid7 of an edge (a placement).
    EdgeId
);
id_type!(
    /// uuid7 of a volume.
    VolumeId
);
id_type!(
    /// uuid7 of a mount — the durable identity that brokers access (ACC-1).
    MountId
);
id_type!(
    /// uuid7 of a lock.
    LockId
);
id_type!(
    /// Opaque streaming-descriptor handle.
    FdId
);

macro_rules! token_enum {
    ($(#[$meta:meta])* $name:ident { $($variant:ident = $token:literal),+ $(,)? }) => {
        $(#[$meta])*
        #[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
        #[serde(rename_all = "lowercase")]
        pub enum $name { $($variant),+ }

        impl $name {
            /// The database token.
            pub fn as_str(self) -> &'static str {
                match self { $($name::$variant => $token),+ }
            }
            /// The inverse of [`Self::as_str`]; `None` for anything outside
            /// the closed set.
            pub fn parse(s: &str) -> Option<Self> {
                match s { $($token => Some($name::$variant),)+ _ => None }
            }
        }
        impl fmt::Display for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str(self.as_str())
            }
        }
        impl ToSql for $name {
            fn to_sql(&self) -> rusqlite::Result<ToSqlOutput<'_>> {
                Ok(ToSqlOutput::Borrowed(ValueRef::Text(self.as_str().as_bytes())))
            }
        }
        impl FromSql for $name {
            fn column_result(v: ValueRef<'_>) -> FromSqlResult<Self> {
                let s = v.as_str()?;
                $name::parse(s).ok_or_else(|| {
                    FromSqlError::Other(
                        format!("{} is not a {}", s, stringify!($name)).into(),
                    )
                })
            }
        }
    };
}

token_enum!(
    /// NODE-2 vocabulary. Era 2 (D-3) adds the three special leaf types; they
    /// place like entries and carry a content row (a symlink's content is its
    /// target, fifo/socket content is empty). Devices are refused by decision.
    NodeType {
        Container = "container",
        Entry = "entry",
        Symlink = "symlink",
        Fifo = "fifo",
        Socket = "socket",
    }
);

impl NodeType {
    /// Everything that is not a container: has a content row, places as a
    /// leaf, may be hardlinked.
    pub fn is_leaf(self) -> bool {
        self != NodeType::Container
    }
}

token_enum!(
    /// `new` and `active` both count as valid; only `unmounted` is terminal
    /// (ACC-4).
    MountState {
        New = "new",
        Active = "active",
        Unmounted = "unmounted",
    }
);

token_enum!(
    /// D-4 mount access mode.
    Access {
        Rw = "rw",
        Ro = "ro",
    }
);

token_enum!(
    /// Seek origin for a streaming descriptor.
    Whence {
        Set = "set",
        Cur = "cur",
        End = "end",
    }
);

token_enum!(
    /// Start position for `open_write`; an exclusive lock is taken either way.
    WriteMode {
        Truncate = "truncate",
        Append = "append",
    }
);

token_enum!(
    /// Only exclusive in this iteration (ACC-7); reserved for future modes.
    LockMode {
        Exclusive = "exclusive",
    }
);

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tokens_round_trip() {
        for t in [
            NodeType::Container,
            NodeType::Entry,
            NodeType::Symlink,
            NodeType::Fifo,
            NodeType::Socket,
        ] {
            assert_eq!(NodeType::parse(t.as_str()), Some(t));
        }
        assert_eq!(NodeType::parse("device"), None);
        assert_eq!(
            serde_json::to_string(&NodeType::Entry).unwrap(),
            "\"entry\""
        );
        assert_eq!(
            serde_json::to_string(&NodeId("abc".into())).unwrap(),
            "\"abc\""
        );
    }
}
