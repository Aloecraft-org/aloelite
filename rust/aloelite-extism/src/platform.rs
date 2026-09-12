//! The two things the engine takes from the world, on a target that has no
//! `ego_platform`.
//!
//! Every other frontend gets its clock and entropy from `aloelite-store`,
//! which adapts `ego_platform`'s. That crate does not compile for
//! `wasm32-wasip1`: its `detect()` gates the WASI arm on `target_env = "p2"`,
//! so a preview-1 build matches none of its three arms and fails to resolve.
//! The fix is one `cfg` upstream (`target_os = "wasi"` covers p1 and p2);
//! until it lands, these thirty lines are what this crate would otherwise
//! import, and deleting them is the whole of the follow-up.
//!
//! Both work natively too, which is what lets `cargo test` exercise this
//! crate without a wasm runtime.

use aloelite_core::platform::Clock;
use rand_core::{Infallible, TryCryptoRng, TryRng};

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// Wall-clock time from the host: `clock_time_get` under WASI, the OS clock
/// natively. Unlike the browser, this is a target where `std::time` works.
#[derive(Debug, Clone, Copy, Default)]
pub struct HostClock;

/// The host CSPRNG: `random_get` under WASI, the OS source natively.
#[derive(Debug, Clone, Copy, Default)]
pub struct HostEntropy;

impl Clock for HostClock {
    fn now_ns(&self) -> i64 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos().min(i64::MAX as u128) as i64)
            .unwrap_or(0)
    }
}

// ---------------------------------------------------------------------------
// depth: rand_core 0.10's vocabulary over getrandom
// ---------------------------------------------------------------------------

impl TryRng for HostEntropy {
    type Error = Infallible;

    fn try_next_u32(&mut self) -> Result<u32, Infallible> {
        let mut b = [0u8; 4];
        self.try_fill_bytes(&mut b)?;
        Ok(u32::from_le_bytes(b))
    }

    fn try_next_u64(&mut self) -> Result<u64, Infallible> {
        let mut b = [0u8; 8];
        self.try_fill_bytes(&mut b)?;
        Ok(u64::from_le_bytes(b))
    }

    /// # Panics
    ///
    /// If the host entropy source fails, which on every supported target
    /// means the environment itself is broken -- the same posture
    /// `ego_platform::entropy::SystemEntropy` and `rand_core`'s `OsRng` take.
    /// A volume key drawn from a degraded source is worse than no volume.
    fn try_fill_bytes(&mut self, dst: &mut [u8]) -> Result<(), Infallible> {
        getrandom::getrandom(dst).expect("host entropy");
        Ok(())
    }
}

// `Rng` and `CryptoRng` follow from rand_core's blankets over an
// `Error = Infallible` `TryRng`; this marker is the claim that has to be made
// by hand, and it is the one the engine checks before drawing a key.
impl TryCryptoRng for HostEntropy {}
