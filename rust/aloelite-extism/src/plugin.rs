//! The plug-in: the five things a host can call, and the one engine handle
//! behind them.
//!
//! An Extism plug-in instance is a linear memory that persists between
//! calls, so a handle can outlive the call that opened it and a mount can
//! outlive the call that made it. One instance therefore holds one volume,
//! the way one `Fs` does in the browser and one process does at the CLI: to
//! work on two volumes, a host instantiates the plug-in twice.
//!
//! Two storage shapes, and the second is the reason to reach for a plug-in
//! at all (`doc/DECISIONS.md` D-7):
//!
//! - **A file**, through WASI, on a path the manifest granted. Durability
//!   per transaction, and the volume can be larger than memory.
//! - **A memory image**: `fs_open_image` takes the volume's bytes and
//!   `fs_snapshot` hands them back, so the host decides where they live —
//!   S3, a key/value store, a column in Postgres, an encrypted field it
//!   already has. Such an instance needs **no filesystem grant at all**:
//!   with no `allowed_paths`, the sandbox is complete and the plug-in reads
//!   nothing it was not handed. The cost is that the volume must fit in
//!   plug-in memory and a snapshot is the whole database, so durability is
//!   per snapshot and the host decides when — which is the same trade
//!   `aloelite_store::image::Image` makes, in the crate that owns storage
//!   models. (This crate cannot use that one: it reaches `ego_platform`,
//!   which does not compile for preview 1.)
//!
//! | export | input | what it does |
//! |---|---|---|
//! | `fs_open` | `{path}` | open the volume file at `path`, which the manifest must have granted |
//! | `fs_open_memory` | — | a volume store in memory; nothing outlives the instance |
//! | `fs_open_image` | `{image}` | a volume store in memory, loaded from bytes the host kept |
//! | `fs_snapshot` | — | the whole database as bytes, for the host to keep |
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
use rusqlite::{Connection, MAIN_DB};

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
/// tests, and a host that only wants a scratch volume.
pub fn open_in_memory() -> Result<(), FsError> {
    install(Db::open(
        Connection::open_in_memory()?,
        HostClock,
        HostEntropy,
    )?)
}

/// A volume store in memory, loaded from `image` — the bytes a previous
/// [`snapshot`] produced. An empty image is a fresh, empty volume store,
/// which is what a host with nothing stored yet should send.
pub fn open_image(image: &[u8]) -> Result<(), FsError> {
    let mut conn = Connection::open_in_memory()?;
    if !image.is_empty() {
        // SQLite copies the image into memory it owns and grows it in place
        // from there; nothing keeps `image` alive after this.
        conn.deserialize_read_exact(MAIN_DB, image, image.len(), false)?;
    }
    install(Db::open(conn, HostClock, HostEntropy)?)
}

/// The whole database as it stands, for the host to keep.
///
/// Refused inside a transaction, where the image would be torn. Nothing
/// here decides WHEN a host should call it: that policy belongs to the host
/// (D-7 leaves it open on purpose), and an unsnapshotted write is a lost
/// write.
pub fn snapshot() -> Result<Vec<u8>, FsError> {
    HANDLE.with(|slot| {
        let mut slot = slot.borrow_mut();
        let handle = slot
            .as_mut()
            .ok_or_else(|| FsError::usage("snapshot: no volume is open"))?;
        let conn = handle.db()?.connection();
        if !conn.is_autocommit() {
            return Err(FsError::usage("snapshot inside a transaction"));
        }
        Ok(conn.serialize(MAIN_DB)?.to_vec())
    })
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
