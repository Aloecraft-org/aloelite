//! The OPFS pool, exported: `install` once per Worker, `open` a volume file
//! by name — under the Web Lock that makes it single-writer — and
//! administer the files. A thin face on `aloelite_store::opfs::Pool`; the
//! one thing added here is the admission policy.

use std::rc::Rc;

use aloelite_store::StoreError;
use aloelite_store::opfs::{OpfsConfig, Pool as StorePool};
use js_sys::Uint8Array;
use wasm_bindgen::prelude::*;

use crate::args::Args;
use crate::fs::Fs;
use crate::value;
use crate::weblock;

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// `Pool.install`'s options, every one optional; the defaults are
/// `OpfsConfig`'s (directory `.aloelite`, VFS `aloelite-opfs`, six files).
/// Read like an operation's arguments: an unknown key is refused.
pub const INSTALL_OPTIONS: &[&str] = &["directory", "vfsName", "initialCapacity"];

/// An installed pool.
#[wasm_bindgen]
pub struct Pool {
    inner: Rc<StorePool>,
    directory: String,
}

impl std::fmt::Debug for Pool {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Pool")
            .field("directory", &self.directory)
            .field("capacity", &self.inner.capacity())
            .finish()
    }
}

#[wasm_bindgen]
impl Pool {
    /// Register the VFS with OPFS. Once per Worker; a second call for the
    /// same VFS name returns the existing pool.
    pub async fn install(options: JsValue) -> Result<Pool, JsValue> {
        let cfg = install_config(&options).map_err(|e| value::throw(&e))?;
        let pool = StorePool::install(&cfg).await.map_err(store_err)?;
        Ok(Pool {
            inner: Rc::new(pool),
            directory: cfg.directory,
        })
    }

    /// Open (creating if absent) the volume file `name`, single-writer: the
    /// Web Lock `aloelite:<directory>/<name>` is taken first and held until
    /// the handle closes. When another Worker holds it the open fails with
    /// `busy` rather than waiting — waiting silently is how a page hangs.
    pub async fn open(&self, name: String) -> Result<Fs, JsValue> {
        let held = weblock::try_acquire(&format!("{}/{name}", self.directory))
            .await?
            .ok_or_else(|| {
                value::throw_with("busy", &format!("{name} is open in another Worker"))
            })?;
        // If the open fails, `held` drops here and the lock is released.
        let db = self.inner.open(&name).map_err(store_err)?;
        let mut fs = Fs::from_db(db);
        fs.on_close(move || Box::pin(held.release()));
        Ok(fs)
    }

    /// The whole database file, for backup or transfer.
    pub fn export(&self, name: &str) -> Result<Uint8Array, JsValue> {
        let bytes = self.inner.export(name).map_err(store_err)?;
        Ok(Uint8Array::from(bytes.as_slice()))
    }

    /// Install `bytes` (a whole SQLite file) as the database called `name`.
    pub fn import(&self, name: &str, bytes: &[u8]) -> Result<(), JsValue> {
        self.inner.import(name, bytes).map_err(store_err)
    }

    /// Remove the database called `name`; `false` if there was none.
    pub fn delete(&self, name: &str) -> Result<bool, JsValue> {
        self.inner.delete(name).map_err(store_err)
    }

    pub fn exists(&self, name: &str) -> Result<bool, JsValue> {
        self.inner.exists(name).map_err(store_err)
    }

    /// Every database in the pool.
    pub fn list(&self) -> Vec<String> {
        self.inner.list()
    }

    /// Pre-opened files. A database and its journal take two.
    #[wasm_bindgen(getter)]
    pub fn capacity(&self) -> u32 {
        self.inner.capacity()
    }

    /// Pre-open `n` more files; resolves to the new capacity.
    #[wasm_bindgen(js_name = addCapacity)]
    pub async fn add_capacity(&self, n: u32) -> Result<u32, JsValue> {
        self.inner.add_capacity(n).await.map_err(store_err)
    }
}

// ---------------------------------------------------------------------------
// depth
// ---------------------------------------------------------------------------

fn install_config(options: &JsValue) -> Result<OpfsConfig, aloelite_core::FsError> {
    let a = Args::read("Pool.install", options)?;
    a.allow(INSTALL_OPTIONS)?;
    let mut cfg = OpfsConfig::default();
    if let Some(d) = a.opt_str("directory")? {
        cfg.directory = d;
    }
    if let Some(v) = a.opt_str("vfsName")? {
        cfg.vfs_name = v;
    }
    if let Some(n) = a.opt_int("initialCapacity")? {
        cfg.initial_capacity = u32::try_from(n).map_err(|_| {
            aloelite_core::FsError::usage("Pool.install: initialCapacity must fit u32")
        })?;
    }
    Ok(cfg)
}

fn store_err(e: StoreError) -> JsValue {
    match e {
        StoreError::Engine(e) => value::throw(&e),
        StoreError::Sqlite(e) => value::throw_with("sqlite", &e.to_string()),
        StoreError::Opfs(msg) => value::throw_with("opfs", &msg),
        StoreError::Blob(e) => value::throw_with("io", &e.to_string()),
    }
}
