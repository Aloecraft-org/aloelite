//! The engine behind one handle, driven by the spec's operation names.
//!
//! `Fs.call(op, args)` is the whole API: `op` is a name from
//! `mount-api.yaml`, `args` an object keyed by that operation's parameter
//! names, and the result is the operation's return as [`crate::value`]
//! renders it. One entry point rather than fifty methods, for two reasons.
//! The protocol in [`crate::serve`] is then this same call with an envelope
//! around it, so nothing is reachable over messages that is not reachable
//! directly, or the other way round. And the dispatch is a TABLE,
//! [`aloelite_api::OPS`], which that crate's `tests/projection.rs` holds
//! against the spec in both directions — an operation cannot be added to
//! one and forgotten in the other, and a parameter name cannot drift in
//! silence.
//!
//! Everything below is the browser's part: the `wasm_bindgen` class, how a
//! handle is opened, and the JS error a failure throws. The operations
//! themselves are `aloelite_api::Handle`, which this crate shares with
//! every other frontend — so a second frontend cannot answer `create_entry`
//! differently, or forget it.
//!
//! Streaming descriptors live in the handle: `open_read` / `open_write`
//! return the spec's `Descriptor` record, and `read` / `write` / `seek` /
//! `tell` / `close` / `abort` take its `fd`. Closing the handle aborts
//! whatever is still open.

use std::future::Future;
use std::pin::Pin;

use aloelite_api::Handle;
use aloelite_core::{Db, FsError};
use ego_platform::entropy::SystemEntropy;
use rusqlite::Connection;
use wasm_bindgen::prelude::*;

use crate::args::JsArg;
use crate::value::{self, JsSurface};

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// The operations `Fs.call` dispatches, and their parameter names: the one
/// table every Aloelite frontend shares.
pub use aloelite_api::table::{DEFAULT_CHUNK_SIZE, EXTRA_OPS, OPS, Op};

/// What an `on_close` hook hands back: something to wait for before the
/// close is complete (the Web Lock's actual release, for the pool).
pub type AfterClose = Pin<Box<dyn Future<Output = ()>>>;

/// The engine behind one handle.
#[wasm_bindgen]
pub struct Fs {
    handle: Handle,
    on_close: Option<Box<dyn FnOnce() -> AfterClose>>,
}

#[wasm_bindgen]
impl Fs {
    /// A volume store in memory: nothing outlives the handle. For demos,
    /// tests, and a page that keeps its own bytes.
    #[wasm_bindgen(js_name = openMemory)]
    pub fn open_memory() -> Result<Fs, JsValue> {
        let conn = Connection::open_in_memory().map_err(|e| value::throw(&FsError::from(e)))?;
        let db = Db::open(conn, aloelite_store::clock::system_clock(), SystemEntropy)
            .map_err(|e| value::throw(&e))?;
        Ok(Fs::from_db(db))
    }

    /// Run one operation: `op` is a name from [`OPS`] (or [`EXTRA_OPS`]),
    /// `args` an object keyed by its parameter names, or nothing. Throws an
    /// `Error` whose `code` is the spec's error name.
    pub fn call(&mut self, op: &str, args: JsValue) -> Result<JsValue, JsValue> {
        self.dispatch(op, &args).map_err(|e| value::throw(&e))
    }

    /// Abort open descriptors, flush the engine, release the admission
    /// lock; resolves once the lock is actually released, so an `open` of
    /// the same file that follows the `await` will not see `busy`.
    /// Idempotent; every later `call` is a `usage` error.
    pub async fn close(&mut self) -> Result<(), JsValue> {
        let (result, after) = self.shut();
        if let Some(after) = after {
            after.await;
        }
        result.map_err(|e| value::throw(&e))
    }

    /// Whether `close` has run.
    #[wasm_bindgen(getter)]
    pub fn closed(&self) -> bool {
        self.handle.is_closed()
    }

    /// Every name `call` accepts, for a host building its own wrapper.
    pub fn operations() -> Vec<String> {
        Handle::operations()
    }
}

impl std::fmt::Debug for Fs {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Fs")
            .field("closed", &self.closed())
            .field("open_descriptors", &self.handle.open_descriptors())
            .finish()
    }
}

impl Fs {
    /// Wrap an opened engine. How `Pool.open` and `openMemory` build one.
    pub fn from_db(db: Db) -> Fs {
        Fs {
            handle: Handle::new(db),
            on_close: None,
        }
    }

    /// Run `f` when the handle closes and wait for what it returns (the
    /// pool hangs the Web Lock's release here).
    pub fn on_close(&mut self, f: impl FnOnce() -> AfterClose + 'static) {
        self.on_close = Some(Box::new(f));
    }

    /// The synchronous part of [`Fs::close`]: the engine is closed when this
    /// returns; the future, if any, is the part still to wait for. What the
    /// server uses so it never holds a borrow across an await.
    pub fn shut(&mut self) -> (Result<(), FsError>, Option<AfterClose>) {
        if self.handle.is_closed() {
            return (Ok(()), None);
        }
        let result = self.handle.shut();
        let after = self.on_close.take().map(|f| f());
        (result, after)
    }

    /// [`Fs::call`] without the JS error conversion: what the server uses.
    pub fn dispatch(&mut self, op: &str, args: &JsValue) -> Result<JsValue, FsError> {
        self.handle.call::<JsSurface>(op, &JsArg::from(args))
    }
}
