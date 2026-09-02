//! Host-side id minting (doc/DECISIONS.md D-1/D-2).
//!
//! Ids are uuid7 strings in the exact layout the retired SQL triggers
//! produced, pinned byte-for-byte by `conformance/vectors/ids-v1.json`:
//!
//! ```text
//! TTTTTTTT-TTTT-7SSS-Vrrr-rrrrrrrrrrrr
//! ```
//!
//! `T` is 48-bit unix epoch milliseconds as 12 lowercase hex digits, `S` a
//! 12-bit sequence in `rand_a`, `V` a variant nibble drawn from `89ab`, and
//! `r` 15 random hex digits. The 19-character prefix (`T`, the `7`, `S`) is
//! deterministic and is what the vectors assert; the tail is random and
//! carries no promise.
//!
//! Two mints, one per kind of id:
//!
//! - [`stateless_uuid7`] — volume, mount and lock ids. No ordering promise;
//!   the sequence nibbles are random, exactly as the old SQL path did it.
//! - [`MonotonicMint`] — node and edge ids. Strictly increasing `(ts, seq)`
//!   per instance. On 12-bit overflow within one millisecond the timestamp
//!   borrows 1 ms forward, matching the trigger it replaced; spinning would be
//!   pointless when the borrow is invisible sub-millisecond skew.
//!
//! The ordering contract (D-2): strict within a mint; strict across mints
//! separated by more than the coordination window; arbitrary within it. The
//! fence against clock regression is the volume high-water mark —
//! [`MonotonicMint::fence`] is called at attach with the volume's stored
//! `(wm_ts, wm_seq)`, so a mint can never produce an id at or below anything
//! the volume has already recorded.
//!
//! This module takes time and randomness as arguments and touches no
//! platform: `now_ms` comes from the caller's `Clock`, the tail from the
//! caller's `Rng`. That is what lets the vectors drive it deterministically
//! and what lets it compile for every target with zero `cfg`.

use rand_core::Rng;

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// The 12-bit sequence space in `rand_a`: 4096 ids per millisecond per mint.
pub const SEQ_LIMIT: u16 = 4096;

/// The variant nibble is drawn uniformly from these four.
const VARIANT: &[u8; 4] = b"89ab";

/// The shared layout. Random tail minted here; callers own `(ts, seq)`.
///
/// # Panics
///
/// If `seq >= SEQ_LIMIT`. That is a programming error, not a runtime
/// condition: every caller in this crate either draws `seq` below the limit
/// or holds the [`MonotonicMint`] invariant that keeps it there. The Python
/// reference raises `ValueError` at the same point, and nothing catches it.
pub fn format_uuid7(ts_ms: u64, seq: u16, rng: &mut impl Rng) -> String {
    assert!(seq < SEQ_LIMIT, "seq {seq} outside 12-bit space");
    let t = format!("{:012x}", ts_ms & 0xFFFF_FFFF_FFFF);
    let variant = VARIANT[(rng.next_u32() % 4) as usize] as char;
    // 15 random hex nibbles: three after the variant, then twelve. Eight
    // random bytes give sixteen; the first is dropped. The distribution is
    // what the reference produces (uniform hex); the specific draws are not
    // part of the contract.
    let mut tail = [0u8; 8];
    rng.fill_bytes(&mut tail);
    let hex = hex16(&tail);
    format!(
        "{}-{}-7{seq:03x}-{variant}{}-{}",
        &t[0..8],
        &t[8..12],
        &hex[1..4],
        &hex[4..16],
    )
}

/// A fresh non-monotonic uuid7 (volume / mount / lock ids).
pub fn stateless_uuid7(now_ms: u64, rng: &mut impl Rng) -> String {
    let seq = (rng.next_u32() % u32::from(SEQ_LIMIT)) as u16;
    format_uuid7(now_ms, seq, rng)
}

/// Strictly-increasing `(ts, seq)` state for one volume within one mount
/// session. In-memory only — a restarted session re-fences from the volume
/// row (D-2); nothing here is ever persisted per mount.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct MonotonicMint {
    /// `None` until the first fence or mint. The reference models this as
    /// `(ts=0, seq=-1)`; `None` is the same value without a negative
    /// sequence — any real `(ts, seq)` is greater than it, which is all the
    /// comparisons below need.
    state: Option<(u64, u16)>,
}

impl MonotonicMint {
    /// A mint that has produced nothing and been fenced by nothing.
    pub fn new() -> Self {
        Self::default()
    }

    /// The current `(ts, seq)`, or `None` before the first fence or mint.
    /// What the conformance vectors assert after each step.
    pub fn state(&self) -> Option<(u64, u16)> {
        self.state
    }

    /// Raise the floor to the volume's high-water mark. Idempotent and
    /// monotonic: fencing below the current state is a no-op, so re-fencing
    /// (e.g. after a reconnect) can never move the mint backwards.
    pub fn fence(&mut self, wm_ts: u64, wm_seq: u16) {
        if Some((wm_ts, wm_seq)) > self.state {
            self.state = Some((wm_ts, wm_seq));
        }
    }

    /// The next id, strictly above every id this mint has produced and above
    /// the fence. Clock regression is absorbed: a `now` at or below the
    /// current timestamp advances the sequence (borrowing 1 ms forward on
    /// overflow) instead of ever reusing or reversing `(ts, seq)`.
    pub fn mint(&mut self, now_ms: u64, rng: &mut impl Rng) -> String {
        let (ts, seq) = match self.state {
            Some((ts, _)) if now_ms > ts => (now_ms, 0),
            Some((ts, seq)) if seq + 1 < SEQ_LIMIT => (ts, seq + 1),
            Some((ts, _)) => (ts + 1, 0),
            None => (now_ms, 0),
        };
        self.state = Some((ts, seq));
        format_uuid7(ts, seq, rng)
    }
}

// ---------------------------------------------------------------------------
// depth: hex
// ---------------------------------------------------------------------------

fn hex16(bytes: &[u8; 8]) -> String {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    let mut s = String::with_capacity(16);
    for b in bytes {
        s.push(DIGITS[(b >> 4) as usize] as char);
        s.push(DIGITS[(b & 0x0f) as usize] as char);
    }
    s
}

#[cfg(test)]
mod tests {
    //! Shape tests only. The byte-for-byte contract is exercised by the
    //! conformance crate against `conformance/vectors/ids-v1.json`, which is
    //! the same file every other implementation reads.

    use super::*;

    /// A counter, not entropy: enough to exercise the layout deterministically.
    /// rand_core 0.10 is implemented through `TryRng`; `Rng` is the blanket
    /// over `Error = Infallible`.
    struct Counter(u64);
    impl rand_core::TryRng for Counter {
        type Error = rand_core::Infallible;
        fn try_next_u32(&mut self) -> Result<u32, Self::Error> {
            Ok(self.try_next_u64()? as u32)
        }
        fn try_next_u64(&mut self) -> Result<u64, Self::Error> {
            self.0 = self.0.wrapping_add(0x9E37_79B9_7F4A_7C15);
            Ok(self.0)
        }
        fn try_fill_bytes(&mut self, dst: &mut [u8]) -> Result<(), Self::Error> {
            for chunk in dst.chunks_mut(8) {
                let v = self.try_next_u64()?.to_le_bytes();
                chunk.copy_from_slice(&v[..chunk.len()]);
            }
            Ok(())
        }
    }

    #[test]
    fn layout_is_36_chars_with_version_and_variant_in_place() {
        let mut rng = Counter(1);
        let id = format_uuid7(1_723_542_000_123, 7, &mut rng);
        assert_eq!(id.len(), 36);
        assert_eq!(&id[14..15], "7", "version nibble");
        assert!(b"89ab".contains(&id.as_bytes()[19]), "variant nibble: {id}");
        assert!(id.bytes().all(|b| b == b'-' || b.is_ascii_hexdigit()));
        assert!(
            id.bytes().all(|b| !b.is_ascii_uppercase()),
            "lowercase only"
        );
    }

    #[test]
    fn ids_from_one_mint_sort_strictly_as_strings() {
        // NODE-5 and the D-2 contract both lean on bytewise string order of
        // the whole id, not just the prefix.
        let mut rng = Counter(7);
        let mut m = MonotonicMint::new();
        let a = m.mint(5000, &mut rng);
        let b = m.mint(5000, &mut rng);
        let c = m.mint(4000, &mut rng); // regression absorbed
        assert!(a < b && b < c, "{a} {b} {c}");
    }

    #[test]
    #[should_panic(expected = "outside 12-bit space")]
    fn seq_out_of_range_is_a_contract_violation() {
        format_uuid7(0, SEQ_LIMIT, &mut Counter(0));
    }
}
