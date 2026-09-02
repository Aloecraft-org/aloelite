//! ego-platform's clock, adapted to the engine's.
//!
//! `aloelite_core::platform::Clock` asks one question (epoch nanoseconds);
//! `ego_platform::clock::Clock` answers it on every target — `std` time
//! natively and on WASI, `web_time` in the browser, where
//! `std::time::SystemTime::now()` would panic. This adapter is the whole
//! bridge, and it is why the engine never names a platform.

use aloelite_core::platform::Clock;
use ego_platform::UNIX_EPOCH;
use ego_platform::clock::{Clock as PlatformClock, SystemClock};

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// Any `ego_platform` clock — `SystemClock` in production, `ManualClock` in
/// a test that wants to drive expiry — as an engine clock.
#[derive(Debug, Clone, Copy, Default)]
pub struct EgoClock<C>(pub C);

/// The platform's own clock.
pub fn system_clock() -> EgoClock<SystemClock> {
    EgoClock(SystemClock)
}

impl<C: PlatformClock> Clock for EgoClock<C> {
    fn now_ns(&self) -> i64 {
        self.0
            .wall()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos().min(i64::MAX as u128) as i64)
            .unwrap_or(0)
    }
}
