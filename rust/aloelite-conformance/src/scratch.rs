//! A per-scenario database path, and the one platform seam in this crate.
//!
//! Every scenario starts from a fresh volume in its own file so scenarios
//! never see each other (and `two_mounts_one_volume` can open the SAME file
//! twice, which a `:memory:` database cannot offer). Natively that file lives
//! in the OS temp directory; in the browser `std::env::temp_dir()` panics and
//! the default `sqlite-wasm-rs` VFS is an in-memory map keyed by name, so any
//! name is a fresh, shareable file. That is the whole difference, and it is
//! the only `cfg` in the runner.

use rand_core::Rng;

use ego_platform::entropy::SystemEntropy;

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// A unique database path that removes its files (and SQLite's sidecars)
/// when dropped — natively; in the browser the removal is a no-op error the
/// drop ignores.
pub struct Scratch {
    pub path: String,
}

impl Scratch {
    pub fn new(label: &str) -> Scratch {
        let tag: String = label
            .chars()
            .map(|c| if c.is_ascii_alphanumeric() { c } else { '-' })
            .take(48)
            .collect();
        let nonce = SystemEntropy.next_u64();
        Scratch {
            path: format!("{}aloelite-conformance-{tag}-{nonce:016x}.fs", dir()),
        }
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        for suffix in ["", "-wal", "-shm", "-journal"] {
            let _ = std::fs::remove_file(format!("{}{suffix}", self.path));
        }
    }
}

// ---------------------------------------------------------------------------
// depth
// ---------------------------------------------------------------------------

#[cfg(all(target_arch = "wasm32", target_os = "unknown"))]
fn dir() -> String {
    "/".to_owned()
}

#[cfg(not(all(target_arch = "wasm32", target_os = "unknown")))]
fn dir() -> String {
    let mut d = std::env::temp_dir();
    d.push(""); // trailing separator
    d.to_string_lossy().into_owned()
}
