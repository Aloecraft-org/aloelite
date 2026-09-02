//! The file model: a path on a real filesystem. Native and WASI.
//!
//! The production shape for servers, the CLI and the FUSE daemon: SQLite
//! owns durability per transaction (WAL where the filesystem allows it,
//! PERSIST otherwise — `Db::open` probes), and a second process can open the
//! same file, which is what the mount-row model expects.

use std::path::Path;

use aloelite_core::Db;
use aloelite_core::platform::{Clock, CryptoRngCore};
use ego_platform::entropy::SystemEntropy;
use rusqlite::{Connection, OpenFlags};

use crate::clock::system_clock;
use crate::error::Result;

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// Open `path`, creating the database file if it does not exist, on the
/// platform clock and entropy.
pub fn open(path: impl AsRef<Path>) -> Result<Db> {
    open_with(path, system_clock(), SystemEntropy)
}

/// Open `path` only if it already exists; a missing file is an error rather
/// than a fresh, empty volume store. What a CLI wants for a user-supplied
/// path.
pub fn open_existing(path: impl AsRef<Path>) -> Result<Db> {
    let flags = OpenFlags::default() & !OpenFlags::SQLITE_OPEN_CREATE;
    let conn = Connection::open_with_flags(path, flags)?;
    Ok(Db::open(conn, system_clock(), SystemEntropy)?)
}

/// [`open`] with an injected clock and entropy source.
pub fn open_with(
    path: impl AsRef<Path>,
    clock: impl Clock + 'static,
    rng: impl CryptoRngCore + Send + 'static,
) -> Result<Db> {
    let conn = Connection::open(path)?;
    Ok(Db::open(conn, clock, rng)?)
}
