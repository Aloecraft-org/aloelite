//! The Web Lock that makes a volume single-writer (D-7): taken before the
//! file is opened, held until the handle closes, released by the browser
//! itself if the Worker dies. Two Workers each holding a `sahpool` handle on
//! one file would corrupt it, so the lock is not advice; it is the admission
//! policy, and it lives in the one place that opens files.
//!
//! Through `Reflect` rather than `web_sys::LockManager`, which is behind
//! `web_sys_unstable_apis` and would put a `--cfg` on every build of the
//! workspace for three calls.

use std::future::Future;

use js_sys::{Function, Object, Promise, Reflect};
use wasm_bindgen::prelude::*;
use wasm_bindgen_futures::JsFuture;

use crate::value;

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// Every lock this crate takes is `aloelite:<name>`, so a page's own locks
/// can never collide with a volume's.
pub const LOCK_PREFIX: &str = "aloelite:";

/// A held Web Lock. Dropping it releases the lock; [`Held::release`] does
/// the same and lets the caller wait until the release has actually
/// happened, which a drop cannot.
pub struct Held {
    release: Option<Function>,
    /// `request()`'s own promise: it settles only after the lock is
    /// released, so awaiting it is how a caller knows the next `open` of the
    /// same name will not see `busy`. The release itself is asynchronous in
    /// every browser — a request made in the same turn as the release still
    /// finds the lock held.
    pending: Promise,
}

/// Take the exclusive lock `aloelite:<name>` if it is free, without
/// waiting: `Ok(None)` means another context holds it. `Err` only when Web
/// Locks are unavailable, which the browser target treats as fatal rather
/// than as permission to open unprotected.
pub async fn try_acquire(name: &str) -> Result<Option<Held>, JsValue> {
    let manager = lock_manager()?;
    let request = Reflect::get(&manager, &"request".into())?.dyn_into::<Function>()?;

    // `request` holds the lock for exactly as long as the promise its
    // callback returns stays pending; the resolver of that promise is what
    // `Held` keeps. A second promise carries the grant decision out of the
    // callback, because with `ifAvailable` a refused request resolves the
    // outer promise immediately and a granted one not until release.
    let mut release = None;
    let hold = Promise::new(&mut |res, _| release = Some(res));
    let release = release.expect("a Promise executor runs synchronously");
    let mut decide = None;
    let decision = Promise::new(&mut |res, _| decide = Some(res));
    let decide = decide.expect("a Promise executor runs synchronously");

    let callback = Closure::once_into_js(move |lock: JsValue| -> JsValue {
        let granted = !(lock.is_null() || lock.is_undefined());
        let _ = decide.call1(&JsValue::UNDEFINED, &JsValue::from_bool(granted));
        if granted {
            hold.into()
        } else {
            JsValue::UNDEFINED
        }
    });
    let options = Object::new();
    Reflect::set(&options, &"mode".into(), &"exclusive".into())?;
    Reflect::set(&options, &"ifAvailable".into(), &JsValue::TRUE)?;
    let full = format!("{LOCK_PREFIX}{name}");
    let pending = request
        .call3(&manager, &full.into(), &options, &callback)?
        .dyn_into::<Promise>()?;

    let granted = JsFuture::from(decision).await?.as_bool().unwrap_or(false);
    Ok(granted.then_some(Held {
        release: Some(release),
        pending,
    }))
}

impl Held {
    /// Let the next requester in; the future completes once the lock is
    /// actually released.
    pub fn release(mut self) -> impl Future<Output = ()> {
        self.let_go();
        let pending = self.pending.clone();
        async move {
            let _ = JsFuture::from(pending).await;
        }
    }
}

// ---------------------------------------------------------------------------
// depth
// ---------------------------------------------------------------------------

impl Held {
    fn let_go(&mut self) {
        if let Some(release) = self.release.take() {
            let _ = release.call0(&JsValue::UNDEFINED);
        }
    }
}

impl Drop for Held {
    fn drop(&mut self) {
        self.let_go();
    }
}

fn lock_manager() -> Result<JsValue, JsValue> {
    let navigator = Reflect::get(&js_sys::global(), &"navigator".into())?;
    let locks = Reflect::get(&navigator, &"locks".into())?;
    if locks.is_undefined() || locks.is_null() {
        return Err(value::throw_with(
            "unsupported",
            "Web Locks are not available here, and a volume needs one to be single-writer",
        ));
    }
    Ok(locks)
}
