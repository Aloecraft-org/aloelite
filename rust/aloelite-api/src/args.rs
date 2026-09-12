//! A request's arguments, read by the spec's parameter names and coerced to
//! the engine's types — the inbound half of the boundary
//! [`crate::value::Surface`] is the outbound half of.
//!
//! Nothing here knows what surface it is reading. What is lenient and what
//! is strict is decided by the [`Value`] implementation (a string may be
//! bytes; an integer may arrive however that surface spells integers); what
//! is decided here is the part the spec fixes for every frontend alike: an
//! unknown argument name is refused rather than ignored, because a
//! misspelled optional (`ttl_ms` as `ttlMs`) would otherwise change
//! behaviour in silence, and every failure is a `usage` error naming the
//! operation and the argument.

use std::collections::BTreeMap;

use aloelite_core::FsError;
use aloelite_core::crypto::EncMode;
use aloelite_core::types::{
    Access, LockId, MountId, NodeId, NodeType, VolumeId, Whence, WriteMode,
};

use crate::value::Value;

pub type Result<T> = std::result::Result<T, FsError>;

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// One request's arguments, with typed accessors.
pub struct Args<V: Value> {
    op: String,
    map: BTreeMap<String, V>,
}

impl<V: Value> Args<V> {
    /// `args` may be absent (no arguments) or an object.
    pub fn read(op: &str, args: &V) -> Result<Args<V>> {
        let mut map = BTreeMap::new();
        if !args.is_absent() {
            let entries = args
                .entries()
                .ok_or_else(|| usage(format!("{op}: args must be {}", V::WANTED_OBJECT)))?;
            for (name, v) in entries {
                if !v.is_omitted() {
                    map.insert(name, v);
                }
            }
        }
        Ok(Args {
            op: op.to_owned(),
            map,
        })
    }

    /// Refuse arguments the operation does not take.
    pub fn allow(&self, names: &[&str]) -> Result<()> {
        let unknown: Vec<&str> = self
            .map
            .keys()
            .map(String::as_str)
            .filter(|k| !names.contains(k))
            .collect();
        if unknown.is_empty() {
            Ok(())
        } else {
            Err(usage(format!(
                "{}: unknown argument(s) {unknown:?}; it takes {names:?}",
                self.op
            )))
        }
    }

    pub fn str(&self, name: &str) -> Result<String> {
        self.opt_str(name)?.ok_or_else(|| self.missing(name))
    }

    pub fn opt_str(&self, name: &str) -> Result<Option<String>> {
        match self.get(name) {
            None => Ok(None),
            Some(v) => v
                .as_str()
                .map(Some)
                .ok_or_else(|| self.wrong(name, "a string", v)),
        }
    }

    pub fn bytes(&self, name: &str) -> Result<Vec<u8>> {
        self.opt_bytes(name)?.ok_or_else(|| self.missing(name))
    }

    pub fn opt_bytes(&self, name: &str) -> Result<Option<Vec<u8>>> {
        match self.get(name) {
            None => Ok(None),
            Some(v) => v
                .as_bytes()
                .map(Some)
                .ok_or_else(|| self.wrong(name, V::WANTED_BYTES, v)),
        }
    }

    pub fn int(&self, name: &str) -> Result<i64> {
        self.opt_int(name)?.ok_or_else(|| self.missing(name))
    }

    pub fn opt_int(&self, name: &str) -> Result<Option<i64>> {
        let Some(v) = self.get(name) else {
            return Ok(None);
        };
        if let Some(n) = v.as_int() {
            return Ok(Some(n));
        }
        if v.is_oversized_int() {
            return Err(self.wrong(name, "an integer that fits 64 bits", v));
        }
        Err(self.wrong(name, V::WANTED_INT, v))
    }

    /// A non-negative integer: sizes and offsets.
    pub fn uint(&self, name: &str) -> Result<u64> {
        let n = self.int(name)?;
        u64::try_from(n).map_err(|_| usage(format!("{}: {name} must not be negative", self.op)))
    }

    pub fn opt_bool(&self, name: &str) -> Result<Option<bool>> {
        match self.get(name) {
            None => Ok(None),
            Some(v) => v
                .as_bool()
                .map(Some)
                .ok_or_else(|| self.wrong(name, "a boolean", v)),
        }
    }

    /// A `{string: string}` object; absent means empty.
    pub fn map(&self, name: &str) -> Result<BTreeMap<String, String>> {
        let Some(v) = self.get(name) else {
            return Ok(BTreeMap::new());
        };
        let entries = v
            .entries()
            .ok_or_else(|| self.wrong(name, V::WANTED_MAP, v))?;
        let mut out = BTreeMap::new();
        for (k, val) in entries {
            let Some(s) = val.as_str() else {
                return Err(usage(format!(
                    "{}: {name}.{k} must be a string, got {}",
                    self.op,
                    val.describe()
                )));
            };
            out.insert(k, s);
        }
        Ok(out)
    }

    // -- the spec's scalars and enums ------------------------------------

    pub fn mount(&self) -> Result<MountId> {
        Ok(MountId(self.str("mount")?))
    }

    pub fn volume(&self, name: &str) -> Result<VolumeId> {
        Ok(VolumeId(self.str(name)?))
    }

    pub fn opt_volume(&self, name: &str) -> Result<Option<VolumeId>> {
        Ok(self.opt_str(name)?.map(VolumeId))
    }

    pub fn node(&self, name: &str) -> Result<NodeId> {
        Ok(NodeId(self.str(name)?))
    }

    pub fn lock(&self, name: &str) -> Result<LockId> {
        Ok(LockId(self.str(name)?))
    }

    pub fn opt_lock(&self, name: &str) -> Result<Option<LockId>> {
        Ok(self.opt_str(name)?.map(LockId))
    }

    pub fn node_type(&self, name: &str) -> Result<NodeType> {
        let s = self.str(name)?;
        NodeType::parse(&s).ok_or_else(|| self.unknown(name, "node type", &s))
    }

    /// `open_write`'s mode; the spec's default is `truncate`.
    pub fn write_mode(&self, name: &str) -> Result<WriteMode> {
        match self.opt_str(name)? {
            None => Ok(WriteMode::Truncate),
            Some(s) => WriteMode::parse(&s).ok_or_else(|| self.unknown(name, "write mode", &s)),
        }
    }

    /// `seek`'s origin; the spec's default is `set`.
    pub fn whence(&self, name: &str) -> Result<Whence> {
        match self.opt_str(name)? {
            None => Ok(Whence::Set),
            Some(s) => Whence::parse(&s).ok_or_else(|| self.unknown(name, "whence", &s)),
        }
    }

    /// `create_volume`'s encryption mode; the spec's default is `convergent`.
    pub fn enc_mode(&self, name: &str) -> Result<EncMode> {
        match self.opt_str(name)? {
            None => Ok(EncMode::Convergent),
            Some(s) => EncMode::parse(&s).ok_or_else(|| self.unknown(name, "enc_mode", &s)),
        }
    }

    /// `mount`'s access mode; the spec's default is `rw`.
    pub fn access(&self, name: &str) -> Result<Access> {
        match self.opt_str(name)? {
            None => Ok(Access::Rw),
            Some(s) => Access::parse(&s).ok_or_else(|| self.unknown(name, "access", &s)),
        }
    }
}

// ---------------------------------------------------------------------------
// depth
// ---------------------------------------------------------------------------

impl<V: Value> Args<V> {
    fn get(&self, name: &str) -> Option<&V> {
        self.map.get(name).filter(|v| !v.is_absent())
    }

    fn missing(&self, name: &str) -> FsError {
        usage(format!("{}: missing argument {name}", self.op))
    }

    fn wrong(&self, name: &str, wanted: &str, got: &V) -> FsError {
        usage(format!(
            "{}: {name} must be {wanted}, got {}",
            self.op,
            got.describe()
        ))
    }

    fn unknown(&self, name: &str, what: &str, got: &str) -> FsError {
        usage(format!("{}: {got:?} is not a {what} ({name})", self.op))
    }
}

fn usage(msg: String) -> FsError {
    FsError::usage(msg)
}
