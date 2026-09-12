//! What crosses the boundary outward: records as plain objects, integers
//! as `BigInt`, bytes as `Uint8Array`, errors as an `Error` with a `code`.
//!
//! The one decision here that a consumer feels is `BigInt` for every
//! integer. Timestamps are nanoseconds (NODE-4) and sit around 2^60, where
//! a double's spacing is 256 ns: a `set_mtime` round-tripped through a JS
//! number would come back changed. Consistency beats convenience — a mixed
//! surface where `size` is a number but `modified_at` a BigInt is the kind
//! of thing that is right until it is not — so sizes, counts and modes are
//! BigInt too, and `Number(x)` is one call away when a number is wanted.
//!
//! [`JsSurface`] is this crate's half of `aloelite_api::Surface`; the other
//! half, arguments coming in, is [`crate::args`].

use aloelite_api::Surface;
use aloelite_core::FsError;
use js_sys::{Object, Reflect, Uint8Array};
use serde::Serialize;
use wasm_bindgen::JsValue;

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// Error codes this surface can raise that are NOT in the spec's closed set.
/// A consumer may see exactly these in addition to `FsError::CODES`. The
/// first three are the dispatch's own (`aloelite_api::EXTRA_CODES`); the
/// rest are the browser's.
///
/// | code | meaning |
/// |---|---|
/// | `usage` | the request itself was wrong: unknown operation or argument, wrong type, a closed handle, an unknown descriptor |
/// | `internal` | an engine invariant failed (a bug, not a caller error) |
/// | `sqlite` | SQLite refused something the engine did not anticipate |
/// | `busy` | the volume file is open in another Worker (`Pool.open`) |
/// | `opfs` | the OPFS pool refused: no capacity, no such file, storage denied |
/// | `io` | a blob store failed (not raised by this crate's own openers) |
pub const EXTRA_CODES: &[&str] = &["usage", "internal", "sqlite", "busy", "opfs", "io"];

/// The browser's pair of value types: a `JsValue` in, a `JsValue` out.
pub struct JsSurface;

impl Surface for JsSurface {
    type In = crate::args::JsArg;
    type Out = JsValue;

    /// A record or scalar, as a JS value: objects for records and string
    /// maps, arrays for lists, `BigInt` for integers, `null` for an absent
    /// optional.
    fn record<T: Serialize + ?Sized>(value: &T) -> JsValue {
        let ser = serde_wasm_bindgen::Serializer::new()
            .serialize_maps_as_objects(true)
            .serialize_missing_as_null(true)
            .serialize_large_number_types_as_bigints(true);
        value.serialize(&ser).expect("records serialize")
    }

    /// Bytes, as a fresh `Uint8Array` the JS side owns.
    fn bytes(b: &[u8]) -> JsValue {
        Uint8Array::from(b).into()
    }

    fn unit() -> JsValue {
        JsValue::UNDEFINED
    }

    fn null() -> JsValue {
        JsValue::NULL
    }
}

/// The wire code of an engine error: the spec's name, or the engine-side
/// name from [`EXTRA_CODES`].
pub use aloelite_api::code;

/// What `Fs.call` throws: an `Error` whose `code` property is the wire code
/// and whose `name` is `AloeliteError`.
pub fn throw(e: &FsError) -> JsValue {
    throw_with(code(e), &e.to_string())
}

/// [`throw`] for a code that has no `FsError` behind it.
pub fn throw_with(code: &str, message: &str) -> JsValue {
    let err = js_sys::Error::new(message);
    err.set_name("AloeliteError");
    let _ = Reflect::set(&err, &"code".into(), &code.into());
    err.into()
}

/// What a reply's `error` field carries: `{code, message}` as a plain
/// object, because structured clone drops an `Error`'s own properties and
/// the code would not survive the trip.
pub fn error_object(e: &FsError) -> JsValue {
    error_object_with(code(e), &e.to_string())
}

pub fn error_object_with(code: &str, message: &str) -> JsValue {
    let obj = Object::new();
    let _ = Reflect::set(&obj, &"code".into(), &code.into());
    let _ = Reflect::set(&obj, &"message".into(), &message.into());
    obj.into()
}
