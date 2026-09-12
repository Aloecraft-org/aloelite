//! Content addressing and chunking (CV-1, CV-2), pinned by
//! `conformance/vectors/format-v1.json` (`chunk_address`, `chunk_split`).
//!
//! A chunk's address is `SHA256(len_be64 || bytes)` — the byte length folded
//! in so a short final chunk can never collide with a full chunk sharing its
//! leading bytes. The address is taken over the bytes **actually stored**:
//! for an encrypted volume that is the ciphertext, so the pool's invariant
//! (same address ⇔ same stored bytes) holds even when two volumes in one
//! file use different keys (see `crypto`).
//!
//! Splitting is uniform: every chunk is `chunk_size` bytes except a shorter
//! final one; empty content stages no chunks at all.

use sha2::{Digest, Sha256};

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// CV-2: the content address of `data`, as lowercase hex.
pub fn chunk_hash(data: &[u8]) -> String {
    let mut h = Sha256::new();
    h.update((data.len() as u64).to_be_bytes());
    h.update(data);
    hex::encode(h.finalize())
}

/// CV-1: `data` in `chunk_size` pieces, the last one possibly short. Empty
/// data yields no chunks.
///
/// # Panics
///
/// If `chunk_size == 0`. Chunk size is a volume property fixed at creation
/// and validated there; a zero here is a programming error, as it is in the
/// reference (`range(0, n, 0)` raises).
pub fn split_chunks(data: &[u8], chunk_size: usize) -> Vec<&[u8]> {
    assert!(chunk_size > 0, "chunk_size must be positive");
    if data.is_empty() {
        return Vec::new();
    }
    data.chunks(chunk_size).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn address_is_length_sensitive() {
        // The reason the length is folded in: these share leading bytes.
        assert_ne!(chunk_hash(b"ab"), chunk_hash(b"abc"));
        assert_ne!(chunk_hash(b""), chunk_hash(b"\0"));
    }

    #[test]
    fn split_shapes() {
        assert!(split_chunks(b"", 8).is_empty());
        assert_eq!(split_chunks(b"abc", 8), vec![&b"abc"[..]]);
        assert_eq!(
            split_chunks(b"abcdefghij", 4),
            vec![&b"abcd"[..], b"efgh", b"ij"]
        );
        assert_eq!(
            split_chunks(b"abcdefgh", 4).len(),
            2,
            "exact multiple: no empty tail"
        );
    }
}
