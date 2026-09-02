//! A request's arguments, read out of a JS object by the spec's parameter
//! names and coerced to the engine's types — the inbound half of the
//! boundary that [`crate::value`] is the outbound half of.
//!
//! Lenient where JavaScript is naturally loose, strict where a mistake
//! would be silent: an integer may arrive as a `Number` (if it is a safe
//! integer) or a `BigInt`; bytes as a `Uint8Array`, an `ArrayBuffer`, or a
//! string (UTF-8); `null` and `undefined` both mean "not given". An unknown
//! argument name is refused rather than ignored, because a misspelled
//! optional (`ttl_ms` as `ttlMs`) would otherwise change behaviour in
//! silence.

use std::collections::BTreeMap;

use aloelite_core::FsError;
use aloelite_core::crypto::EncMode;
use aloelite_core::types::{
    Access, LockId, MountId, NodeId, NodeType, VolumeId, Whence, WriteMode,
};
use js_sys::{Array, ArrayBuffer, BigInt, Object, Reflect, Uint8Array};
use wasm_bindgen::{JsCast, JsValue};

pub type Result<T> = std::result::Result<T, FsError>;

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// Largest magnitude a JS `Number` can carry exactly; beyond it an integer
/// must be a `BigInt`.
pub const MAX_SAFE_INTEGER: f64 = 9_007_199_254_740_991.0;

/// One request's arguments, with typed accessors. Every accessor's failure
/// is a `usage` error naming the operation and the argument.
pub struct Args {
    op: String,
    map: BTreeMap<String, JsValue>,
}

impl Args {
    /// `args` may be `undefined` / `null` (no arguments) or a plain object.
    pub fn read(op: &str, args: &JsValue) -> Result<Args> {
        let mut map = BTreeMap::new();
        if !absent(args) {
            if !args.is_object() || Array::is_array(args) {
                return Err(usage(format!("{op}: args must be an object")));
            }
            let obj: &Object = args.unchecked_ref();
            for key in Object::keys(obj).iter() {
                let name = key.as_string().unwrap_or_default();
                let v = Reflect::get(args, &key).unwrap_or(JsValue::UNDEFINED);
                if !v.is_undefined() {
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
                .as_string()
                .map(Some)
                .ok_or_else(|| self.wrong(name, "a string", v)),
        }
    }

    pub fn bytes(&self, name: &str) -> Result<Vec<u8>> {
        self.opt_bytes(name)?.ok_or_else(|| self.missing(name))
    }

    pub fn opt_bytes(&self, name: &str) -> Result<Option<Vec<u8>>> {
        let Some(v) = self.get(name) else {
            return Ok(None);
        };
        if let Some(u8s) = v.dyn_ref::<Uint8Array>() {
            return Ok(Some(u8s.to_vec()));
        }
        if let Some(buf) = v.dyn_ref::<ArrayBuffer>() {
            return Ok(Some(Uint8Array::new(buf).to_vec()));
        }
        if let Some(s) = v.as_string() {
            return Ok(Some(s.into_bytes()));
        }
        Err(self.wrong(name, "bytes (Uint8Array, ArrayBuffer or string)", v))
    }

    pub fn int(&self, name: &str) -> Result<i64> {
        self.opt_int(name)?.ok_or_else(|| self.missing(name))
    }

    pub fn opt_int(&self, name: &str) -> Result<Option<i64>> {
        let Some(v) = self.get(name) else {
            return Ok(None);
        };
        if let Some(f) = v.as_f64() {
            if f.fract() == 0.0 && f.abs() <= MAX_SAFE_INTEGER {
                return Ok(Some(f as i64));
            }
            return Err(self.wrong(name, "an integer (a safe Number or a BigInt)", v));
        }
        if v.is_bigint() {
            let big: &BigInt = v.unchecked_ref();
            let text = big
                .to_string(10)
                .map(String::from)
                .map_err(|_| self.wrong(name, "an integer", v))?;
            return text
                .parse::<i64>()
                .map(Some)
                .map_err(|_| self.wrong(name, "an integer that fits 64 bits", v));
        }
        Err(self.wrong(name, "an integer (a safe Number or a BigInt)", v))
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
        if !v.is_object() || Array::is_array(v) {
            return Err(self.wrong(name, "an object of strings", v));
        }
        let obj: &Object = v.unchecked_ref();
        let mut out = BTreeMap::new();
        for key in Object::keys(obj).iter() {
            let k = key.as_string().unwrap_or_default();
            let val = Reflect::get(v, &key).unwrap_or(JsValue::UNDEFINED);
            let Some(s) = val.as_string() else {
                return Err(usage(format!(
                    "{}: {name}.{k} must be a string, got {}",
                    self.op,
                    describe(&val)
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

impl Args {
    fn get(&self, name: &str) -> Option<&JsValue> {
        self.map.get(name).filter(|v| !absent(v))
    }

    fn missing(&self, name: &str) -> FsError {
        usage(format!("{}: missing argument {name}", self.op))
    }

    fn wrong(&self, name: &str, wanted: &str, got: &JsValue) -> FsError {
        usage(format!(
            "{}: {name} must be {wanted}, got {}",
            self.op,
            describe(got)
        ))
    }

    fn unknown(&self, name: &str, what: &str, got: &str) -> FsError {
        usage(format!("{}: {got:?} is not a {what} ({name})", self.op))
    }
}

fn absent(v: &JsValue) -> bool {
    v.is_undefined() || v.is_null()
}

fn usage(msg: String) -> FsError {
    FsError::usage(msg)
}

/// `typeof`, plus the constructor name for objects: what an error message
/// says the caller passed.
fn describe(v: &JsValue) -> String {
    if v.is_null() {
        return "null".to_owned();
    }
    let ty = v.js_typeof().as_string().unwrap_or_default();
    if ty == "object"
        && let Some(name) = Reflect::get(v, &"constructor".into())
            .ok()
            .and_then(|c| Reflect::get(&c, &"name".into()).ok())
            .and_then(|n| n.as_string())
    {
        return name;
    }
    ty
}
