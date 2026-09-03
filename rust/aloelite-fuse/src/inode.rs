//! Inode numbers and permission bits: the two pure mappings the handlers
//! share with the reference daemon, so a volume presents the same `st_ino`
//! and `st_mode` whichever daemon serves it.

use aloelite_core::records::NodeInfo;

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// The kernel's root inode; the volume root is pinned to it.
pub const ROOT: u64 = 1;

/// uuid7 → 64-bit inode: FNV-1a over the id's text, avoiding 0 (invalid)
/// and 1 (the root). The same function as `fuse.py`'s `_ino`.
pub fn ino(node_id: &str) -> u64 {
    let mut h: u64 = 14_695_981_039_346_656_037;
    for b in node_id.bytes() {
        h ^= u64::from(b);
        h = h.wrapping_mul(1_099_511_628_211);
    }
    if h > 1 { h } else { h + 2 }
}

/// Permission bits for a node: the era-2 `mode` column when set, else the
/// era-1 octal-string metadata convention, else `default`. Masked to
/// `0o7777` so a malformed value can never smuggle in file-type bits.
pub fn mode_bits(info: &NodeInfo, default: u32) -> u32 {
    if let Some(mode) = info.mode {
        return (mode as u32) & 0o7777;
    }
    if let Some(raw) = info.metadata.get("mode")
        && let Ok(parsed) = u32::from_str_radix(raw, 8)
    {
        return parsed & 0o7777;
    }
    default
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ino_matches_the_reference_hash() {
        // FNV-1a of "a" is 0xaf63dc4c8601ec8c; the reference computes the
        // same, so the two daemons agree on st_ino for the same node.
        assert_eq!(ino("a"), 0xaf63_dc4c_8601_ec8c);
        assert_ne!(ino(""), 0);
        assert_ne!(ino(""), 1);
    }
}
