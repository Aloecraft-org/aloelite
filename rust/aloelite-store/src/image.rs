//! The memory-image model: the whole volume held in a `:memory:` database,
//! loaded from and checkpointed back to ONE blob in a `BlobStore`.
//!
//! Aloelite's premise is *the filesystem is one file*, and `BlobStore`'s one
//! hard promise — a put is all-or-nothing, a reader sees the old blob or the
//! new blob and never a torn one — is exactly the durability primitive a
//! whole-file checkpoint needs. That makes this the portable shape: the same
//! code runs on every target over `MemStore`, `DirStore` or `IdbStore`.
//!
//! Durability is **per checkpoint**, not per transaction. Everything since
//! the last [`Image::checkpoint`] lives only in memory; [`Image::close`]
//! checkpoints first. The policy of WHEN to checkpoint (on unmount, every N
//! writes, on idle) belongs to the host, which is why it is a method and not
//! a timer: nothing here decides it for you (D-7 leaves it open on purpose).

use std::sync::Arc;

use aloelite_core::Db;
use aloelite_core::platform::{Clock, CryptoRngCore};
use ego_platform::blobs::BlobStore;
use ego_platform::entropy::SystemEntropy;

use crate::clock::system_clock;
use crate::error::Result;

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// A volume store held in memory and persisted as one blob.
pub struct Image {
    db: Db,
    store: Arc<dyn BlobStore>,
    key: String,
}

impl Image {
    /// Load the image under `key` (an absent or empty blob is a fresh, empty
    /// volume store) on the platform clock and entropy.
    pub async fn open(store: Arc<dyn BlobStore>, key: &str) -> Result<Image> {
        Self::open_with(store, key, system_clock(), SystemEntropy).await
    }

    /// [`Image::open`] with an injected clock and entropy source.
    pub async fn open_with(
        store: Arc<dyn BlobStore>,
        key: &str,
        clock: impl Clock + 'static,
        rng: impl CryptoRngCore + Send + 'static,
    ) -> Result<Image> {
        let bytes = store.get(key).await?.unwrap_or_default();
        let db = Db::open_image(&bytes, clock, rng)?;
        Ok(Image {
            db,
            store,
            key: key.to_owned(),
        })
    }

    /// The blob key this image checkpoints to.
    pub fn key(&self) -> &str {
        &self.key
    }

    /// The engine handle: pass it to the operations in `aloelite_core::ops`.
    pub fn db(&mut self) -> &mut Db {
        &mut self.db
    }

    /// The database as it stands, serialized: what a checkpoint writes.
    /// Refused inside a transaction, where the image would be torn.
    pub fn snapshot(&self) -> Result<Vec<u8>> {
        Ok(self.db.snapshot()?)
    }

    /// Persist the image: one atomic put of the whole database.
    pub async fn checkpoint(&mut self) -> Result<()> {
        let bytes = self.snapshot()?;
        self.store.put(&self.key, bytes).await?;
        Ok(())
    }

    /// Checkpoint, then close the engine handle.
    pub async fn close(mut self) -> Result<()> {
        self.checkpoint().await?;
        self.db.close()?;
        Ok(())
    }
}
