//! How a MessagePack value answers the questions `aloelite_api::Value` asks
//! — the inbound half of the boundary that [`crate::value`] is the outbound
//! half of.
//!
//! Stricter than the browser's, and deliberately so. There, leniency buys
//! something: JavaScript has one number type, so an integer may arrive as a
//! `Number` or a `BigInt` and refusing either would be pedantry. MessagePack
//! has a real integer type, so a float where an integer belongs means the
//! host encoded it wrong, and saying so is more useful than rounding it. The
//! one leniency kept is that a `str` is accepted where `bin` is wanted, for
//! the same reason the browser accepts a string as bytes: a caller writing
//! text to a file should not have to encode it first.
//!
//! MessagePack has no `undefined`, only `nil`, so a key present with a nil
//! value is still a key the caller wrote: a misspelled optional set to nil
//! is reported as an unknown argument rather than ignored.

use aloelite_api::Value;
use rmpv::Value as Mp;

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// A MessagePack value in argument position.
///
/// The wrapper is the orphan rule's doing — `aloelite_api::Value` and
/// `rmpv::Value` are both someone else's type.
#[derive(Clone, Debug, PartialEq)]
pub struct Msg(pub Mp);

impl From<Mp> for Msg {
    fn from(v: Mp) -> Msg {
        Msg(v)
    }
}

impl Value for Msg {
    const WANTED_INT: &'static str = "an integer (a msgpack int, not a float)";
    const WANTED_BYTES: &'static str = "bytes (a msgpack bin, or a str as UTF-8)";
    const WANTED_MAP: &'static str = "a map of strings";
    const WANTED_OBJECT: &'static str = "a map with string keys";

    fn is_absent(&self) -> bool {
        matches!(self.0, Mp::Nil)
    }

    fn as_str(&self) -> Option<String> {
        match &self.0 {
            Mp::String(s) => s.as_str().map(str::to_owned),
            _ => None,
        }
    }

    fn as_bytes(&self) -> Option<Vec<u8>> {
        match &self.0 {
            Mp::Binary(b) => Some(b.clone()),
            Mp::String(s) => s.as_str().map(|s| s.as_bytes().to_vec()),
            _ => None,
        }
    }

    fn as_int(&self) -> Option<i64> {
        match &self.0 {
            Mp::Integer(i) => i.as_i64(),
            _ => None,
        }
    }

    fn as_bool(&self) -> Option<bool> {
        match &self.0 {
            Mp::Boolean(b) => Some(*b),
            _ => None,
        }
    }

    /// A `uint64` past `i64::MAX` is an integer that will not fit, which is
    /// worth its own message.
    fn is_oversized_int(&self) -> bool {
        matches!(self.0, Mp::Integer(_)) && self.as_int().is_none()
    }

    /// A map's members. A non-string key makes the whole thing not an
    /// arguments map, rather than one bad member: a caller that keyed a map
    /// by integer meant something else entirely.
    fn entries(&self) -> Option<Vec<(String, Msg)>> {
        let Mp::Map(pairs) = &self.0 else {
            return None;
        };
        pairs
            .iter()
            .map(|(k, v)| match k {
                Mp::String(s) => s.as_str().map(|k| (k.to_owned(), Msg(v.clone()))),
                _ => None,
            })
            .collect()
    }

    /// The MessagePack type name, which is what a host's encoder chose and
    /// therefore what the author of the request can act on.
    fn describe(&self) -> String {
        match &self.0 {
            Mp::Nil => "nil",
            Mp::Boolean(_) => "bool",
            Mp::Integer(_) => "int",
            Mp::F32(_) | Mp::F64(_) => "float",
            Mp::String(s) if s.as_str().is_none() => "str (not UTF-8)",
            Mp::String(_) => "str",
            Mp::Binary(_) => "bin",
            Mp::Array(_) => "array",
            Mp::Map(_) => "map",
            Mp::Ext(..) => "ext",
        }
        .to_owned()
    }
}
