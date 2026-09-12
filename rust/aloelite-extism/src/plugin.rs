//! The plug-in: the five things a host can call, and the one engine handle
//! behind them.
//!
//! An Extism plug-in instance is a linear memory that persists between
//! calls, so a handle can outlive the call that opened it and a mount can
//! outlive the call that made it. One instance therefore holds one volume,
//! the way one `Fs` does in the browser and one process does at the CLI: to
//! work on two volumes, a host instantiates the plug-in twice.
//!
//! | export | input | what it does |
//! |---|---|---|
//! | `fs_open` | `{path}` | open the volume file at `path`, which the manifest must have granted |
//! | `fs_open_memory` | — | a volume store in memory; nothing outlives the instance |
//! | `fs_call` | `{op, args}` | run one Mount API operation ([`aloelite_api::OPS`] is the table) |
//! | `fs_close` | — | abort open descriptors and close the engine; idempotent |
//! | `fs_operations` | — | every name `fs_call` accepts |
//!
//! Every export answers the envelope in [`crate::wire`] and none of them
//! fails at the Extism level, including the ones that failed.
//!
//! The exports themselves are [`crate::exports`], which exists only where
//! the Extism ABI does: a `#[plugin_fn]` is a `#[no_mangle]` symbol calling
//! the host's allocator, and an rlib carrying one cannot be linked into a
//! native test binary. What is left here is the same work without the
//! envelope, which is what lets `cargo test` drive the whole path — codec,
//! dispatch and engine — natively, with no wasm runtime in the loop.

use std::cell::RefCell;

use aloelite_api::Handle;
use aloelite_core::{Db, FsError};
use rusqlite::Connection;

use crate::args::Msg;
use crate::platform::{HostClock, HostEntropy};
use crate::value::MsgpackSurface;

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// Open the volume file at `path`. Under Extism that path is one the
/// manifest's `allowed_paths` granted; the plug-in can reach nothing else,
/// and finding that out is the host's business, not this crate's.
pub fn open_file(path: &str) -> Result<(), FsError> {
    install(Db::open(Connection::open(path)?, HostClock, HostEntropy)?)
}

/// A volume store in memory: nothing outlives the instance. For demos,
/// tests, and a host that keeps its own bytes.
pub fn open_in_memory() -> Result<(), FsError> {
    install(Db::open(
        Connection::open_in_memory()?,
        HostClock,
        HostEntropy,
    )?)
}

/// Run one operation on the open handle. The result is one MessagePack
/// value's encoding, ready for [`crate::wire::reply`].
pub fn dispatch(op: &str, args: &Msg) -> Result<Vec<u8>, FsError> {
    HANDLE.with(|slot| {
        let mut slot = slot.borrow_mut();
        let handle = slot
            .as_mut()
            .ok_or_else(|| FsError::usage(format!("{op}: no volume is open")))?;
        handle.call::<MsgpackSurface>(op, args)
    })
}

/// Abort open descriptors and close the engine. Idempotent, and an instance
/// that has closed one volume may open another.
pub fn shut() -> Result<(), FsError> {
    HANDLE.with(|slot| match slot.borrow_mut().as_mut() {
        Some(handle) => handle.shut(),
        None => Ok(()),
    })
}

// ---------------------------------------------------------------------------
// depth: the instance's one handle
// ---------------------------------------------------------------------------

thread_local! {
    static HANDLE: RefCell<Option<Handle>> = const { RefCell::new(None) };
}

/// Take an opened engine as this instance's handle, refusing to displace one
/// that is still open — dropping it would lose its descriptors and the mount
/// rows they were opened against, in silence.
fn install(db: Db) -> Result<(), FsError> {
    HANDLE.with(|slot| {
        let mut slot = slot.borrow_mut();
        if slot.as_ref().is_some_and(|h| !h.is_closed()) {
            return Err(FsError::usage(
                "a volume is already open on this plug-in instance; close it first",
            ));
        }
        *slot = Some(Handle::new(db));
        Ok(())
    })
}
