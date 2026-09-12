//! How a `JsValue` answers the questions `aloelite_api::Value` asks — the
//! inbound half of the boundary that [`crate::value`] is the outbound half
//! of.
//!
//! Lenient where JavaScript is naturally loose, strict where a mistake
//! would be silent: an integer may arrive as a `Number` (if it is a safe
//! integer) or a `BigInt`; bytes as a `Uint8Array`, an `ArrayBuffer`, or a
//! string (UTF-8); `undefined` means "not given", and `null` means "given,
//! and empty" — which is why a misspelled optional set to null is still
//! reported as an unknown argument rather than ignored.
//!
//! Which argument each operation takes, and what happens when one is
//! missing or wrong, is not here: that is one implementation in
//! `aloelite_api::args`, shared with every other frontend.

use aloelite_api::Value;
use js_sys::{Array, ArrayBuffer, BigInt, Object, Reflect, Uint8Array};
use wasm_bindgen::{JsCast, JsValue};

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// Largest magnitude a JS `Number` can carry exactly; beyond it an integer
/// must be a `BigInt`.
pub const MAX_SAFE_INTEGER: f64 = 9_007_199_254_740_991.0;

/// A `JsValue` in argument position.
///
/// The wrapper is the orphan rule's doing — `aloelite_api::Value` and
/// `JsValue` are both someone else's type — and it costs one refcount bump
/// per call, since a `JsValue` is a handle into the JS heap.
#[derive(Clone, Debug)]
pub struct JsArg(pub JsValue);

impl From<&JsValue> for JsArg {
    fn from(v: &JsValue) -> JsArg {
        JsArg(v.clone())
    }
}

impl Value for JsArg {
    const WANTED_INT: &'static str = "an integer (a safe Number or a BigInt)";
    const WANTED_BYTES: &'static str = "bytes (Uint8Array, ArrayBuffer or string)";
    const WANTED_MAP: &'static str = "an object of strings";
    const WANTED_OBJECT: &'static str = "an object";

    fn is_omitted(&self) -> bool {
        self.0.is_undefined()
    }

    fn is_absent(&self) -> bool {
        self.0.is_undefined() || self.0.is_null()
    }

    fn as_str(&self) -> Option<String> {
        self.0.as_string()
    }

    fn as_bytes(&self) -> Option<Vec<u8>> {
        if let Some(u8s) = self.0.dyn_ref::<Uint8Array>() {
            return Some(u8s.to_vec());
        }
        if let Some(buf) = self.0.dyn_ref::<ArrayBuffer>() {
            return Some(Uint8Array::new(buf).to_vec());
        }
        self.0.as_string().map(String::into_bytes)
    }

    fn as_int(&self) -> Option<i64> {
        if let Some(f) = JsValue::as_f64(&self.0) {
            return (f.fract() == 0.0 && f.abs() <= MAX_SAFE_INTEGER).then_some(f as i64);
        }
        if self.0.is_bigint() {
            let big: &BigInt = self.0.unchecked_ref();
            return big
                .to_string(10)
                .ok()
                .and_then(|s| String::from(s).parse::<i64>().ok());
        }
        None
    }

    // Named rather than inferred: `JsValue` has an inherent `as_bool`, and
    // an inherent method wins over a trait one, so `self.as_bool()` here
    // would be this method calling itself.
    fn as_bool(&self) -> Option<bool> {
        JsValue::as_bool(&self.0)
    }

    fn is_oversized_int(&self) -> bool {
        self.0.is_bigint() && self.as_int().is_none()
    }

    fn entries(&self) -> Option<Vec<(String, JsArg)>> {
        if !self.0.is_object() || Array::is_array(&self.0) {
            return None;
        }
        let obj: &Object = self.0.unchecked_ref();
        Some(
            Object::keys(obj)
                .iter()
                .map(|key| {
                    let name = key.as_string().unwrap_or_default();
                    let v = Reflect::get(&self.0, &key).unwrap_or(JsValue::UNDEFINED);
                    (name, JsArg(v))
                })
                .collect(),
        )
    }

    /// `typeof`, plus the constructor name for objects: what an error
    /// message says the caller passed.
    fn describe(&self) -> String {
        if self.0.is_null() {
            return "null".to_owned();
        }
        let ty = self.0.js_typeof().as_string().unwrap_or_default();
        if ty == "object"
            && let Some(name) = Reflect::get(&self.0, &"constructor".into())
                .ok()
                .and_then(|c| Reflect::get(&c, &"name".into()).ok())
                .and_then(|n| n.as_string())
        {
            return name;
        }
        ty
    }
}
