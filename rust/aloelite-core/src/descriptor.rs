//! Streaming descriptor.
//!
//! Runtime state — a cursor plus, for writers, a small pending buffer and the
//! application-level lock — with no SQL of its own except at its boundaries.
//! It is a *contract* object: `open_read` / `open_write` return it and every
//! implementation must produce an equivalent. It holds no reference to the
//! engine: each call takes the [`Db`] it was opened on, which is the flat
//! shape the Mount API's `streaming` section describes (`read(fd, len)`) and
//! what lets a binding keep descriptors in a table beside one connection.
//!
//! Bounded-memory streaming:
//!
//!   READS are ranged. The reader holds (committed version, total size,
//!   chunk size, cursor); `read(n)` fetches just the chunks covering
//!   `[pos, pos+n)` and trims the edges. A version's chunks are uniform
//!   `chunk_size` except the final one (true for every write path), so
//!   `byte_offset / chunk_size` is the chunk index. The committed pointer is
//!   re-read on every call, so a reader tracks commits made after it opened
//!   (CV-3: the committed pointer is the sole definition of current bytes).
//!
//!   WRITES stream forward. The writer keeps a pending buffer under one chunk;
//!   each time a write pushes it past a chunk boundary the complete chunk is
//!   staged + committed in its OWN short transaction (CV-5) and dropped from
//!   memory. The new version number is allocated lazily on the first flush
//!   (or at close) so a same-mount `write_all` between open and close cannot
//!   collide on it. `close()` stages the final short chunk and swaps the
//!   committed pointer in one transaction, releasing the lock. A crash
//!   mid-stream leaves staged chunks ABOVE the committed pointer: the prior
//!   version is intact and `prune_content` reclaims the orphans.
//!
//!   SEQUENTIAL CONTRACT. A write inside the current pending window is
//!   absorbed in memory; a write into an ALREADY-FLUSHED region is refused as
//!   `unsupported` rather than silently corrupting. A writer is write-forward
//!   only: `read()` on it is `unsupported`.

use rusqlite::named_params;

use crate::db::Db;
use crate::errors::{FsError, Result};
use crate::templates::mutation::{COPY_CHUNK_REFS_RANGE, RELEASE_LOCK, UPDATE_CONTENT};
use crate::templates::resolution::GET_LOCK_VALID;
use crate::types::{FdId, LockId, NodeId, VolumeId, Whence};

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// An open read or write handle to a leaf's content. Construct via
/// `ops::open_read` / `ops::open_write`, never directly.
#[derive(Debug)]
pub struct Descriptor {
    pub fd: FdId,
    pub node: NodeId,
    pub writable: bool,
    volume: VolumeId,
    chunk_size: u64,
    lock: Option<LockId>,
    /// `false` when the lock was supplied by `ops::lock` rather than minted
    /// by `open_write`: the caller owns its lifetime, so close/abort must
    /// leave it standing.
    owns_lock: bool,
    pos: u64,
    closed: bool,
    // -- read mode: advisory snapshot, refreshed per call ---------------
    version: i64,
    size: u64,
    // -- write mode --------------------------------------------------------
    /// Allocated lazily on first flush / close.
    new_version: Option<i64>,
    /// Prior version to re-reference full leading chunks from (append).
    carry_src: i64,
    /// Count of full leading chunks carried by reference.
    carry_full: u64,
    carry_done: bool,
    /// Next chunk index to assign.
    chunk_index: u64,
    /// Bytes already in committed chunks.
    flushed: u64,
    /// Bytes from `flushed` onward, not yet a full chunk.
    pending: Vec<u8>,
}

/// What `open_write` hands the descriptor: the append carry-forward state.
pub(crate) struct WriterSetup {
    pub lock: LockId,
    pub owns_lock: bool,
    pub carry_src: i64,
    pub carry_full: u64,
    pub pending: Vec<u8>,
    pub position: u64,
}

impl Descriptor {
    pub(crate) fn reader(
        fd: FdId,
        node: NodeId,
        volume: VolumeId,
        chunk_size: usize,
        version: i64,
        size: i64,
    ) -> Self {
        Descriptor {
            fd,
            node,
            writable: false,
            volume,
            chunk_size: chunk_size as u64,
            lock: None,
            owns_lock: false,
            pos: 0,
            closed: false,
            version,
            size: size.max(0) as u64,
            new_version: None,
            carry_src: 0,
            carry_full: 0,
            carry_done: true,
            chunk_index: 0,
            flushed: 0,
            pending: Vec::new(),
        }
    }

    pub(crate) fn writer(
        fd: FdId,
        node: NodeId,
        volume: VolumeId,
        chunk_size: usize,
        setup: WriterSetup,
    ) -> Self {
        let chunk_size = chunk_size as u64;
        Descriptor {
            fd,
            node,
            writable: true,
            volume,
            chunk_size,
            lock: Some(setup.lock),
            owns_lock: setup.owns_lock,
            pos: setup.position,
            closed: false,
            version: 0,
            size: 0,
            new_version: None,
            carry_src: setup.carry_src,
            carry_full: setup.carry_full,
            carry_done: false,
            chunk_index: setup.carry_full,
            flushed: setup.carry_full * chunk_size,
            pending: setup.pending,
        }
    }

    pub fn closed(&self) -> bool {
        self.closed
    }

    /// The volume this descriptor's leaf lives in.
    pub fn volume(&self) -> &VolumeId {
        &self.volume
    }

    /// The lock this descriptor writes under (`None` for a reader).
    pub fn lock(&self) -> Option<&LockId> {
        self.lock.as_ref()
    }

    /// Up to `n` bytes from the cursor (all remaining bytes when `None`),
    /// over the CURRENT committed version.
    pub fn read(&mut self, db: &mut Db, n: Option<usize>) -> Result<Vec<u8>> {
        self.check_open()?;
        if self.writable {
            return Err(FsError::unsupported(
                "streaming write descriptor is write-forward only",
            ));
        }
        self.refresh(db)?;
        let start = self.pos;
        let end = match n {
            None => self.size,
            Some(n) => self.size.min(start.saturating_add(n as u64)),
        };
        if end <= start {
            return Ok(Vec::new());
        }
        let data = self.fetch_range(db, start, end)?;
        self.pos = end;
        Ok(data)
    }

    /// Append `data` at the cursor; returns the byte count written.
    pub fn write(&mut self, db: &mut Db, data: &[u8]) -> Result<usize> {
        self.check_open()?;
        self.check_writable()?;
        if data.is_empty() {
            return Ok(0);
        }
        let end = self.pos + data.len() as u64;
        if self.pos < self.flushed {
            // target overlaps an already-flushed, immutable chunk region
            return Err(FsError::unsupported(
                "write into an already-flushed region is not supported (streaming writer is sequential)",
            ));
        }
        let lo = (self.pos - self.flushed) as usize;
        let hi = (end - self.flushed) as usize;
        if lo > self.pending.len() {
            // sparse gap -> zero-fill
            self.pending.resize(lo, 0);
        }
        if hi > self.pending.len() {
            self.pending.resize(hi, 0);
        }
        self.pending[lo..hi].copy_from_slice(data);
        self.pos = end;
        while self.pending.len() as u64 >= self.chunk_size {
            self.flush_one_chunk(db)?;
        }
        Ok(data.len())
    }

    /// Move the cursor; returns the new position. `End` on a reader is the
    /// CURRENT committed end, not the open-time one.
    pub fn seek(&mut self, db: &mut Db, offset: i64, whence: Whence) -> Result<u64> {
        self.check_open()?;
        if !self.writable && whence == Whence::End {
            self.refresh(db)?;
        }
        let total = if self.writable {
            self.flushed + self.pending.len() as u64
        } else {
            self.size
        };
        let base = match whence {
            Whence::Set => 0i128,
            Whence::Cur => self.pos as i128,
            Whence::End => total as i128,
        };
        let new = base + offset as i128;
        if new < 0 {
            return Err(FsError::usage("negative seek position"));
        }
        self.pos = new as u64;
        Ok(self.pos)
    }

    pub fn tell(&self) -> Result<u64> {
        self.check_open()?;
        Ok(self.pos)
    }

    /// Discard this write and release the lock if this descriptor owns it.
    /// Idempotent; a no-op on a reader.
    ///
    /// The committed version pointer is left exactly where it was, so the
    /// leaf keeps its previous bytes and the partial write is never visible.
    /// Staged chunks are NOT deleted eagerly: above the committed pointer
    /// they are already CV-3's "incomplete write" state, and CV-7's
    /// `prune_content` is what reclaims it.
    pub fn abort(&mut self, db: &mut Db) -> Result<()> {
        if self.closed {
            return Ok(());
        }
        let result = match (&self.lock, self.writable && self.owns_lock) {
            (Some(lock), true) => {
                let lock = lock.clone();
                db.txn(|db| {
                    db.run(RELEASE_LOCK, named_params! { ":lock": lock })?;
                    Ok(())
                })
            }
            _ => Ok(()),
        };
        self.closed = true;
        result
    }

    /// Commit (writers) and release the lock if this descriptor owns it.
    /// Idempotent.
    ///
    /// Stages the final (short) chunk and swaps the committed-version pointer
    /// in one transaction, after re-validating the lock. A lock that went
    /// invalid mid-stream is `lock_invalid` and the pointer is NOT advanced
    /// (the staged chunks become orphans above it, reclaimable once the lock
    /// is gone). A lock supplied by the caller is left standing: its
    /// lifetime belongs to whoever took it.
    pub fn close(&mut self, db: &mut Db) -> Result<()> {
        if self.closed {
            return Ok(());
        }
        let result = if self.writable {
            self.commit(db)
        } else {
            Ok(()) // a reader holds no lock and has nothing to commit
        };
        self.closed = true;
        result
    }
}

// ---------------------------------------------------------------------------
// depth: ranged reads, chunk flushing, the commit
// ---------------------------------------------------------------------------

impl Descriptor {
    fn check_open(&self) -> Result<()> {
        if self.closed {
            return Err(FsError::usage("descriptor is closed"));
        }
        Ok(())
    }

    fn check_writable(&self) -> Result<()> {
        if !self.writable {
            return Err(FsError::usage("descriptor is read-only"));
        }
        Ok(())
    }

    /// Re-read the committed (version, size) pointer. Within one call the
    /// pair is read atomically, so a single read never mixes versions;
    /// across calls a racing writer may interleave — the contract POSIX
    /// `read(2)` gives.
    fn refresh(&mut self, db: &Db) -> Result<()> {
        let (version, size) = db.read_content_meta(&self.node)?.unwrap_or((0, 0));
        self.version = version;
        self.size = size.max(0) as u64;
        Ok(())
    }

    /// Bytes `[start, end)` of the committed version, pulling only the chunks
    /// that cover the range.
    fn fetch_range(&self, db: &Db, start: u64, end: u64) -> Result<Vec<u8>> {
        let cs = self.chunk_size;
        let lo_idx = start / cs;
        let hi_idx = (end - 1) / cs;
        let rows = db.read_chunk_range(&self.node, self.version, lo_idx as i64, hi_idx as i64)?;
        let mut buf = Vec::new();
        for (_, chunk) in rows {
            buf.extend_from_slice(&chunk);
        }
        let base = lo_idx * cs;
        let from = ((start - base) as usize).min(buf.len());
        let to = ((end - base) as usize).min(buf.len());
        Ok(buf[from..to].to_vec())
    }

    /// Peel one full chunk off the pending buffer and stage + commit it in
    /// its own short transaction (bounded memory, bounded WAL).
    fn flush_one_chunk(&mut self, db: &mut Db) -> Result<()> {
        let cs = self.chunk_size as usize;
        let chunk: Vec<u8> = self.pending.drain(..cs).collect();
        db.txn(|db| {
            let version = self.claim_version_locked(db)?;
            db.stage_chunk(&self.node, version, self.chunk_index as i64, &chunk)
        })?;
        self.chunk_index += 1;
        self.flushed += self.chunk_size;
        Ok(())
    }

    /// Allocate the new version on first use and carry the prior version's
    /// full leading chunks into it (append). Idempotent; runs inside a txn.
    fn claim_version_locked(&mut self, db: &mut Db) -> Result<i64> {
        let version = match self.new_version {
            Some(v) => v,
            None => {
                let v = db.alloc_version(&self.node)?;
                self.new_version = Some(v);
                v
            }
        };
        if !self.carry_done {
            if self.carry_full > 0 {
                db.run(
                    COPY_CHUNK_REFS_RANGE,
                    named_params! {
                        ":node": self.node,
                        ":dst_version": version,
                        ":src_version": self.carry_src,
                        ":lo": 0i64,
                        ":hi": self.carry_full as i64 - 1,
                    },
                )?;
            }
            self.carry_done = true;
        }
        Ok(version)
    }

    fn commit(&mut self, db: &mut Db) -> Result<()> {
        let size = self.flushed + self.pending.len() as u64;
        db.txn(|db| {
            if let Some(lock) = &self.lock {
                let valid: Option<i64> =
                    db.scalar(GET_LOCK_VALID, named_params! { ":lock": lock })?;
                if valid.unwrap_or(0) == 0 {
                    return Err(FsError::LockInvalid {
                        msg: format!("lock {lock} on {} is no longer valid", self.node),
                    });
                }
            }
            let version = self.claim_version_locked(db)?;
            if !self.pending.is_empty() {
                db.stage_chunk(&self.node, version, self.chunk_index as i64, &self.pending)?;
            }
            db.run(
                UPDATE_CONTENT,
                named_params! {
                    ":node": self.node,
                    ":version": version,
                    ":size": size as i64,
                    ":hash": Option::<Vec<u8>>::None,
                },
            )?;
            if let (Some(lock), true) = (&self.lock, self.owns_lock) {
                db.run(RELEASE_LOCK, named_params! { ":lock": lock })?;
            }
            Ok(())
        })
    }
}
