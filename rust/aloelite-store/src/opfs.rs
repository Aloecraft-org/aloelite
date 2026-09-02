//! The browser model: `sqlite-wasm-vfs`'s OPFS pool (`sahpool`), from a
//! Dedicated Worker.
//!
//! OPFS synchronous access handles exist only in Workers, and they are what
//! lets the browser run the *same* synchronous engine as native with real
//! per-transaction durability, instead of an async engine that would need
//! every conformance scenario written twice (D-7). One Worker owns one
//! volume: two Workers each holding a handle on one file would conflict, so
//! the host takes a Web Lock before it opens the file — D-4's admission
//! policy, enforced by the platform rather than by a mount row.
//!
//! [`Pool::install`] registers the VFS once per Worker (a second call for
//! the same name returns the existing pool); [`Pool::open`] then opens a
//! database in it by name. The pool is a fixed number of pre-opened OPFS
//! files (`initial_capacity`): a database and its rollback journal take two,
//! and [`Pool::add_capacity`] grows it.

use aloelite_core::Db;
use aloelite_core::platform::{Clock, CryptoRngCore};
use ego_platform::entropy::SystemEntropy;
use rusqlite::{Connection, OpenFlags};
use sqlite_wasm_vfs::sahpool::{OpfsSAHPoolCfgBuilder, OpfsSAHPoolUtil};

use crate::clock::system_clock;
use crate::error::{Result, StoreError};

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// Where the pool lives in OPFS and how it registers with SQLite.
#[derive(Debug, Clone)]
pub struct OpfsConfig {
    /// OPFS directory holding the pool's files.
    pub directory: String,
    /// The SQLite VFS name the pool registers under.
    pub vfs_name: String,
    /// Pre-opened files. A database and its journal take two.
    pub initial_capacity: u32,
}

impl Default for OpfsConfig {
    fn default() -> Self {
        OpfsConfig {
            directory: ".aloelite".to_owned(),
            vfs_name: "aloelite-opfs".to_owned(),
            initial_capacity: 6,
        }
    }
}

/// An installed OPFS pool: opens databases and administers the files.
pub struct Pool {
    util: OpfsSAHPoolUtil,
    vfs_name: String,
}

impl Pool {
    /// Register the VFS (once per Worker) and return the pool.
    pub async fn install(cfg: &OpfsConfig) -> Result<Pool> {
        let options = OpfsSAHPoolCfgBuilder::new()
            .vfs_name(&cfg.vfs_name)
            .directory(&cfg.directory)
            .initial_capacity(cfg.initial_capacity)
            .build();
        let util =
            sqlite_wasm_vfs::sahpool::install::<sqlite_wasm_rs::WasmOsCallback>(&options, false)
                .await
                .map_err(opfs_err)?;
        Ok(Pool {
            util,
            vfs_name: cfg.vfs_name.clone(),
        })
    }

    /// Open (creating if absent) the database called `name` in this pool, on
    /// the platform clock and entropy.
    pub fn open(&self, name: &str) -> Result<Db> {
        self.open_with(name, system_clock(), SystemEntropy)
    }

    /// [`Pool::open`] with an injected clock and entropy source.
    pub fn open_with(
        &self,
        name: &str,
        clock: impl Clock + 'static,
        rng: impl CryptoRngCore + Send + 'static,
    ) -> Result<Db> {
        let conn = Connection::open_with_flags_and_vfs(
            name,
            OpenFlags::default(),
            self.vfs_name.as_str(),
        )?;
        Ok(Db::open(conn, clock, rng)?)
    }

    /// The database file's bytes, for backup or transfer.
    pub fn export(&self, name: &str) -> Result<Vec<u8>> {
        self.util.export_db(name).map_err(opfs_err)
    }

    /// Install `bytes` as the database called `name` (a whole SQLite file).
    pub fn import(&self, name: &str, bytes: &[u8]) -> Result<()> {
        self.util.import_db(name, bytes).map_err(opfs_err)
    }

    /// Remove the database called `name`; `false` if there was none.
    pub fn delete(&self, name: &str) -> Result<bool> {
        self.util.delete_db(name).map_err(opfs_err)
    }

    pub fn exists(&self, name: &str) -> Result<bool> {
        self.util.exists(name).map_err(opfs_err)
    }

    /// Every database in the pool.
    pub fn list(&self) -> Vec<String> {
        self.util.list()
    }

    pub fn capacity(&self) -> u32 {
        self.util.get_capacity()
    }

    /// Pre-open `n` more files; returns the new capacity.
    pub async fn add_capacity(&self, n: u32) -> Result<u32> {
        self.util.add_capacity(n).await.map_err(opfs_err)
    }
}

// ---------------------------------------------------------------------------
// depth
// ---------------------------------------------------------------------------

fn opfs_err(e: impl std::fmt::Debug) -> StoreError {
    StoreError::Opfs(format!("{e:?}"))
}
