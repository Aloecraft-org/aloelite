//! One error for the crate: an engine fault, a driver fault, blob-store I/O,
//! or the browser VFS.

use aloelite_core::FsError;

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

pub type Result<T> = std::result::Result<T, StoreError>;

#[derive(Debug, thiserror::Error)]
pub enum StoreError {
    /// The engine refused the opened connection (a newer schema era, a too-old
    /// SQLite) or a checkpoint was asked for at the wrong moment.
    #[error(transparent)]
    Engine(#[from] FsError),
    /// The driver could not open or read the database.
    #[error("sqlite: {0}")]
    Sqlite(#[from] rusqlite::Error),
    /// The blob store could not read or write the image.
    #[error("blob store: {0}")]
    Blob(#[from] std::io::Error),
    /// The OPFS pool could not be installed or administered.
    #[error("opfs: {0}")]
    Opfs(String),
}
