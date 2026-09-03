//! Random-access write state over the engine's `write_range`, the port of
//! `fuse.py`'s `_OpenFile`. It buffers only DIRTY byte extents (sorted,
//! non-overlapping), overlays them on ranged reads of committed content, and
//! flushes each coalesced extent as one atomic `write_range`. Memory is
//! bounded by dirty bytes, never by file size.
//!
//! ONE overlay per inode, shared by every rw handle on it (the registry in
//! [`crate::fs`] owns that sharing and the ref count). Per-handle overlays
//! lost writes between concurrent handles and hid unflushed data from a
//! reader on a second fd — the short-read-with-correct-size failure that
//! broke `git push`. One shared overlay makes every handle see one truth.
//!
//! Portable and engine-only (no `fuser`), so it is unit-tested here against
//! an in-memory volume; the live-mount tests exercise it through the kernel.

use aloelite_core::ops;
use aloelite_core::types::{MountId, Whence};
use aloelite_core::{Db, Descriptor, Result};

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------
//
// Entry points: Overlay::new, write, read, truncate, flush, size, is_dirty.
// Configurable: DIRTY_FLUSH. No dispatch.

/// Flush a handle's dirty extents once they reach this many bytes, so a large
/// random-write pass does not grow the buffer without bound. Matches
/// `fuse.py`'s `_DIRTY_FLUSH`.
pub const DIRTY_FLUSH: usize = 32 << 20;

/// The shared dirty-extent state for one inode.
#[derive(Debug)]
pub struct Overlay {
    path: String,
    /// Open rw handle count; the registry drops the overlay at zero.
    pub refs: u32,
    /// Committed size overlaid with pending extents (max seen).
    size: u64,
    /// Dirty extents, sorted by offset, non-overlapping, non-adjacent.
    extents: Vec<(u64, Vec<u8>)>,
    dirty: usize,
}

impl Overlay {
    /// Open the overlay for `path`; its starting size is the committed size.
    pub fn new(db: &mut Db, mount: &MountId, path: &str) -> Result<Overlay> {
        let size = ops::stat(db, mount, path)?.size.unwrap_or(0) as u64;
        Ok(Overlay {
            path: path.to_owned(),
            refs: 0,
            size,
            extents: Vec::new(),
            dirty: 0,
        })
    }

    /// The overlaid size: committed size plus any pending extension.
    pub fn size(&self) -> u64 {
        self.size
    }

    /// Whether anything is buffered and not yet flushed.
    pub fn is_dirty(&self) -> bool {
        self.dirty > 0
    }

    /// Buffer `data` at `off`, coalescing with any touching extent; flushes
    /// if the dirty total crosses [`DIRTY_FLUSH`]. Returns bytes accepted.
    pub fn write(&mut self, db: &mut Db, mount: &MountId, off: u64, data: &[u8]) -> Result<usize> {
        self.insert(off, data);
        if self.dirty >= DIRTY_FLUSH {
            self.flush(db, mount)?;
        }
        Ok(data.len())
    }

    /// Read `[off, off+n)`: committed content from the engine with the dirty
    /// extents overlaid, clamped to the overlaid size. Short at EOF.
    pub fn read(&self, db: &mut Db, mount: &MountId, off: u64, n: usize) -> Result<Vec<u8>> {
        let end = (off + n as u64).min(self.size);
        if end <= off {
            return Ok(Vec::new());
        }
        let mut base = vec![0u8; (end - off) as usize];
        let committed = ops::stat(db, mount, &self.path)?.size.unwrap_or(0) as u64;
        // depth: committed bytes underneath, only where the read predates EOF
        if off < committed {
            let want = (end.min(committed) - off) as usize;
            let mut r: Descriptor = ops::open_read(db, mount, &self.path)?;
            r.seek(db, off as i64, Whence::Set)?;
            let got = r.read(db, Some(want))?;
            r.close(db)?;
            base[..got.len()].copy_from_slice(&got);
        }
        // depth: dirty extents on top
        for (elo, b) in &self.extents {
            let ehi = elo + b.len() as u64;
            if ehi <= off || *elo >= end {
                continue;
            }
            let s = (*elo).max(off);
            let e = ehi.min(end);
            base[(s - off) as usize..(e - off) as usize]
                .copy_from_slice(&b[(s - elo) as usize..(e - elo) as usize]);
        }
        Ok(base)
    }

    /// Flush pending extents, then truncate committed content to `new_size`.
    pub fn truncate(&mut self, db: &mut Db, mount: &MountId, new_size: u64) -> Result<()> {
        self.flush(db, mount)?;
        ops::truncate(db, mount, &self.path, new_size)?;
        self.size = new_size;
        Ok(())
    }

    /// Commit every dirty extent as one `write_range` and clear the buffer.
    /// Idempotent: a second flush (another handle's release) is a no-op.
    pub fn flush(&mut self, db: &mut Db, mount: &MountId) -> Result<()> {
        for (lo, b) in std::mem::take(&mut self.extents) {
            ops::write_range(db, mount, &self.path, lo, &b)?;
        }
        self.dirty = 0;
        Ok(())
    }

    // -----------------------------------------------------------------------
    // depth: extent bookkeeping — fold every touching (overlapping OR
    // adjacent) extent into the new bytes, keep the list sorted. A port of
    // `_OpenFile.write`'s merge, byte for byte.
    // -----------------------------------------------------------------------
    fn insert(&mut self, off: u64, data: &[u8]) {
        let mut new_lo = off;
        let mut new_hi = off + data.len() as u64;
        let mut buf = data.to_vec();
        let mut merged: Vec<(u64, Vec<u8>)> = Vec::with_capacity(self.extents.len() + 1);
        for (lo, b) in std::mem::take(&mut self.extents) {
            let hi = lo + b.len() as u64;
            if hi < new_lo || lo > new_hi {
                merged.push((lo, b)); // disjoint (a gap of >=1 byte remains)
                continue;
            }
            if lo < new_lo {
                let mut head = b[..(new_lo - lo) as usize].to_vec();
                head.extend_from_slice(&buf);
                buf = head;
                new_lo = lo;
            }
            if hi > new_hi {
                buf.extend_from_slice(&b[(new_hi - lo) as usize..]);
                new_hi = new_lo + buf.len() as u64;
            }
        }
        merged.push((new_lo, buf));
        merged.sort_by_key(|(lo, _)| *lo);
        self.extents = merged;
        self.dirty = self.extents.iter().map(|(_, b)| b.len()).sum();
        self.size = self.size.max(new_hi);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use aloelite_core::crypto::EncMode;
    use aloelite_core::ops::MountOptions;
    use ego_platform::entropy::SystemEntropy;
    use rusqlite::Connection;

    fn engine() -> (Db, MountId) {
        let conn = Connection::open_in_memory().unwrap();
        let mut db = Db::open(conn, aloelite_store::clock::system_clock(), SystemEntropy).unwrap();
        let vol = ops::create_volume(&mut db, Some("v"), 64, None, EncMode::Convergent).unwrap();
        let mount = ops::mount(&mut db, &vol.id, &MountOptions::default()).unwrap();
        (db, mount)
    }

    #[test]
    fn disjoint_writes_zero_fill_the_gap() {
        let (mut db, m) = engine();
        ops::create_entry(&mut db, &m, "/f", Some(b"")).unwrap();
        let mut ov = Overlay::new(&mut db, &m, "/f").unwrap();
        ov.write(&mut db, &m, 0, &[b'X'; 100]).unwrap();
        ov.write(&mut db, &m, 200, &[b'Y'; 100]).unwrap();
        assert_eq!(ov.size(), 300);
        let got = ov.read(&mut db, &m, 0, 400).unwrap();
        let mut want = vec![b'X'; 100];
        want.extend(std::iter::repeat_n(0u8, 100));
        want.extend(std::iter::repeat_n(b'Y', 100));
        assert_eq!(got, want);
    }

    #[test]
    fn overlapping_writes_coalesce_into_one_extent() {
        let (mut db, m) = engine();
        ops::create_entry(&mut db, &m, "/f", Some(b"")).unwrap();
        let mut ov = Overlay::new(&mut db, &m, "/f").unwrap();
        ov.write(&mut db, &m, 0, b"AAAAA").unwrap();
        ov.write(&mut db, &m, 3, b"BBBBB").unwrap(); // overlaps at 3-4, extends to 8
        assert_eq!(ov.read(&mut db, &m, 0, 8).unwrap(), b"AAABBBBB");
        // adjacency (an extent ending exactly where the next begins) merges too
        ov.write(&mut db, &m, 8, b"CC").unwrap();
        assert_eq!(ov.read(&mut db, &m, 0, 10).unwrap(), b"AAABBBBBCC");
    }

    #[test]
    fn a_read_overlays_dirty_bytes_on_committed_content() {
        let (mut db, m) = engine();
        ops::create_entry(&mut db, &m, "/f", Some(b"0123456789")).unwrap();
        let mut ov = Overlay::new(&mut db, &m, "/f").unwrap();
        ov.write(&mut db, &m, 4, b"AB").unwrap();
        assert_eq!(ov.read(&mut db, &m, 0, 10).unwrap(), b"0123AB6789");
        // and reading past committed EOF into a dirty extension zero-gaps
        ov.write(&mut db, &m, 12, b"Z").unwrap();
        assert_eq!(ov.read(&mut db, &m, 10, 3).unwrap(), b"\0\0Z");
    }

    #[test]
    fn flush_commits_extents_and_is_idempotent() {
        let (mut db, m) = engine();
        ops::create_entry(&mut db, &m, "/f", Some(b"")).unwrap();
        let mut ov = Overlay::new(&mut db, &m, "/f").unwrap();
        ov.write(&mut db, &m, 0, b"committed").unwrap();
        assert!(ov.is_dirty());
        ov.flush(&mut db, &m).unwrap();
        assert!(!ov.is_dirty());
        ov.flush(&mut db, &m).unwrap(); // no-op
        assert_eq!(ops::read_all(&mut db, &m, "/f").unwrap(), b"committed");
    }

    #[test]
    fn truncate_flushes_then_shrinks() {
        let (mut db, m) = engine();
        ops::create_entry(&mut db, &m, "/f", Some(b"0123456789")).unwrap();
        let mut ov = Overlay::new(&mut db, &m, "/f").unwrap();
        ov.write(&mut db, &m, 2, b"XY").unwrap();
        ov.truncate(&mut db, &m, 4).unwrap();
        assert_eq!(ov.size(), 4);
        assert_eq!(ops::read_all(&mut db, &m, "/f").unwrap(), b"01XY");
    }
}
