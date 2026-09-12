//! What crosses the boundary, as two traits: one for values coming in, one
//! for the values a result is built from.
//!
//! A frontend speaks some value type — a `JsValue` in the browser, a
//! `serde_json::Value` over a plug-in's wire, whatever the next one brings.
//! The dispatch in [`crate::handle`] does not care which, and must not: it
//! is the spec's operations, and the spec has nothing to say about
//! JavaScript. So everything that is genuinely about the value type lives
//! here, in two small traits, and everything else — which argument an
//! operation takes, what it is called, what it coerces to, what the error
//! says when it is wrong — is written once against them.
//!
//! [`Value`] is the inbound half, and it is deliberately thin: six
//! questions, each answered `Option` rather than `Result`, because "this is
//! not a string" is the only thing the surface knows and the message that
//! follows from it belongs to [`crate::args`]. The associated `WANTED_*`
//! strings are how a surface keeps its own vocabulary in those messages —
//! the browser can say "a safe Number or a BigInt" where a JSON surface
//! says "a number".
//!
//! [`Surface`] is the outbound half and pairs the two types, so a dispatch
//! is `dispatch::<JsSurface>(..)` and the argument and result types follow
//! from the one name.

use aloelite_core::FsError;
use serde::Serialize;

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// Error codes the dispatch itself can raise, which are NOT in the spec's
/// closed set. A frontend may add its own (the browser adds `busy` and
/// `opfs`); these three every frontend has.
///
/// | code | meaning |
/// |---|---|
/// | `usage` | the request itself was wrong: unknown operation or argument, wrong type, a closed handle, an unknown descriptor |
/// | `internal` | an engine invariant failed (a bug, not a caller error) |
/// | `sqlite` | SQLite refused something the engine did not anticipate |
pub const EXTRA_CODES: &[&str] = &["usage", "internal", "sqlite"];

/// The wire code of an engine error: the spec's name, or the engine-side
/// name from [`EXTRA_CODES`].
pub fn code(e: &FsError) -> &'static str {
    e.code().unwrap_or(match e {
        FsError::Usage(_) => "usage",
        FsError::Sqlite(_) => "sqlite",
        _ => "internal",
    })
}

/// One argument value, as the surface it arrived on can describe it.
///
/// Every accessor answers `None` for "not that", and never for "absent" —
/// absence is [`Value::is_absent`], asked first. Implementations should be
/// lenient in the ways their own language is naturally loose (a string is
/// bytes; a number that happens to be integral is an integer) and strict
/// everywhere a mistake would otherwise be silent.
pub trait Value: Sized {
    /// What an error message calls an integer on this surface.
    const WANTED_INT: &'static str = "an integer";
    /// …and bytes.
    const WANTED_BYTES: &'static str = "bytes";
    /// …and an object of strings.
    const WANTED_MAP: &'static str = "an object of strings";
    /// …and an object, for the arguments themselves.
    const WANTED_OBJECT: &'static str = "an object";

    /// Not present at all — JavaScript's `undefined`. A surface that draws
    /// no such distinction never says yes, which is the default.
    ///
    /// The difference is not pedantry: an argument that is present and
    /// explicitly null is still an argument, so a misspelled optional set
    /// to null is reported as unknown rather than ignored.
    fn is_omitted(&self) -> bool {
        false
    }

    /// Omitted, or present and empty: what "the caller did not give this"
    /// means when an accessor asks.
    fn is_absent(&self) -> bool;

    fn as_str(&self) -> Option<String>;
    fn as_bytes(&self) -> Option<Vec<u8>>;
    fn as_int(&self) -> Option<i64>;
    fn as_bool(&self) -> Option<bool>;

    /// True when the value is an integer of this surface's kind but does
    /// not fit an `i64` — the one near-miss worth its own message, since
    /// "must be an integer, got BigInt" would read as a lie.
    fn is_oversized_int(&self) -> bool {
        false
    }

    /// The members of an object, in any order; `None` if this is not one.
    /// Used both for the arguments themselves and for a `{string: string}`
    /// argument, so the values come back as values and the string coercion
    /// (and its error message) stays in one place.
    fn entries(&self) -> Option<Vec<(String, Self)>>;

    /// What an error message says the caller passed: a type name, as the
    /// surface's own users would recognise it.
    fn describe(&self) -> String;
}

/// A frontend's two value types, paired: what an argument is, what a result
/// is built from.
pub trait Surface {
    /// The inbound value type.
    type In: Value;
    /// The outbound value type.
    type Out;

    /// A record or scalar, from anything the engine can serialize.
    fn record<T: Serialize + ?Sized>(value: &T) -> Self::Out;
    /// Bytes.
    fn bytes(b: &[u8]) -> Self::Out;
    /// A `void` result.
    fn unit() -> Self::Out;
    /// An absent optional — what `get_xattr` answers for a name that is not
    /// set. Distinct from [`Surface::unit`]: this one is a value.
    fn null() -> Self::Out;
}
