//! The two things the engine needs from the world and refuses to fetch itself.
//!
//! `aloelite-core` compiles for native, `wasm32-wasip2` and
//! `wasm32-unknown-unknown` with zero `cfg`, which is only possible because it
//! never asks the platform for anything: the connection arrives opened, the
//! time arrives from a [`Clock`], and randomness arrives from a
//! [`CryptoRngCore`]. `std::time::SystemTime::now()` compiles for the browser
//! target and panics when called; keeping it behind a trait the host
//! implements is what keeps that panic out of this crate.
//!
//! `aloelite-store` adapts `ego_platform`'s clock and entropy onto these; a
//! test hands in a fixed clock and the platform generator. The bounds are the
//! `rand_core` 0.10 vocabulary so `ego_platform::entropy::SystemEntropy`
//! satisfies them unchanged, and `SeededEntropy` is refused by the compiler
//! wherever a key or nonce is drawn (it is deliberately not `CryptoRng`).

use rand_core::{CryptoRng, Rng};

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// Wall-clock time as unix-epoch nanoseconds (era 2 timestamps).
///
/// `Send` so an engine handle can move between threads on hosts that have
/// them (the FUSE binding); a clock with no fields is `Send` on every target.
pub trait Clock: Send {
    fn now_ns(&self) -> i64;
}

/// `Rng + CryptoRng`, as one nameable bound — the same shape and blanket as
/// `ego_platform::entropy::CryptoRngCore`, so the two are satisfied by exactly
/// the same types without this crate depending on that one.
pub trait CryptoRngCore: Rng + CryptoRng {}
impl<T: Rng + CryptoRng + ?Sized> CryptoRngCore for T {}

/// A clock frozen at one instant, for tests and for hosts that stamp time
/// themselves. Ids stay monotonic under it (the mint borrows into the next
/// millisecond); only timestamps stop moving.
#[derive(Debug, Clone, Copy)]
pub struct FixedClock(pub i64);

impl Clock for FixedClock {
    fn now_ns(&self) -> i64 {
        self.0
    }
}
