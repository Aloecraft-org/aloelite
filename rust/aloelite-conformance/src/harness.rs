//! Harnesses — named starting states a plain operation sequence cannot
//! express (a second volume, a second mount, a connection deliberately
//! holding the wrong cipher). A scenario names one; the runner constructs
//! it and exposes named mounts that steps select with `via`.
//!
//! Every harness in `conformance/README.md` is implemented. The README
//! allows a runner to SKIP a scenario whose harness it lacks; this runner
//! instead fails loudly on an unknown name (see [`Rig::build`]), because a
//! skip reads exactly like a pass and the fixture check
//! `scenarios_only_name_implemented_harnesses` already refuses the typo
//! that would otherwise skip a scenario forever.

use std::collections::BTreeMap;

use aloelite_core::crypto::EncMode;
use aloelite_core::ops::{self, MountOptions};
use aloelite_core::types::{Access, MountId, VolumeId};
use aloelite_core::{Db, Result};
use ego_platform::entropy::SystemEntropy;
use rusqlite::Connection;

use crate::scratch::Scratch;

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------

/// The harness names this runner implements, as `conformance/README.md`
/// lists them.
pub const HARNESSES: &[&str] = &[
    "default",
    "ro_and_rw_mounts",
    "attach_without_key",
    "keyed_cipher_plain_volume",
    "two_entries_same_bytes_convergent",
    "two_entries_same_bytes_random",
    "two_mounts_one_volume",
];

/// The PIN every encrypted harness uses.
pub const PIN: &[u8] = b"pw";

/// A scenario's `setup:` block.
#[derive(Debug, Clone, Copy)]
pub struct Setup {
    pub chunk_size: usize,
}

impl Default for Setup {
    fn default() -> Self {
        Setup {
            chunk_size: 1_048_576,
        }
    }
}

/// The connections and named mounts a scenario runs against.
pub struct Rig {
    dbs: Vec<Db>,
    mounts: BTreeMap<String, (usize, MountId)>,
    _scratch: Scratch,
}

/// Open (or re-open) the scenario database the way a host would: a plain
/// connection by path, the platform clock, the platform entropy.
pub fn open(path: &str) -> Db {
    let conn = Connection::open(path).unwrap_or_else(|e| panic!("open {path}: {e}"));
    Db::open(conn, aloelite_store::clock::system_clock(), SystemEntropy)
        .unwrap_or_else(|e| panic!("Db::open {path}: {e}"))
}

impl Rig {
    /// Build the named harness; `None` for a name this runner does not know.
    pub fn build(harness: &str, setup: &Setup, scratch: Scratch) -> Option<Rig> {
        let cs = setup.chunk_size;
        let path = scratch.path.clone();
        let (dbs, mounts) = match harness {
            "default" => {
                let mut db = open(&path);
                let volume = plain_volume(&mut db, "conformance", cs);
                let mid = mount(&mut db, &volume, &MountOptions::default());
                (vec![db], named([("default", 0, mid)]))
            }
            "ro_and_rw_mounts" => {
                // one volume, one connection, an rw mount and an ro mount
                // (D-4: ro never conflicts); the ro mount is what the policy
                // scenarios operate through
                let mut db = open(&path);
                let volume = plain_volume(&mut db, "conformance", cs);
                let rw = mount(&mut db, &volume, &MountOptions::default());
                let ro = mount(
                    &mut db,
                    &volume,
                    &MountOptions {
                        access: Access::Ro,
                        ..Default::default()
                    },
                );
                (vec![db], named([("rw", 0, rw), ("ro", 0, ro)]))
            }
            "attach_without_key" => {
                // an encrypted volume holding content, then a FRESH connection
                // bound to the same durable mount without ever supplying a
                // PIN -- no cipher installed
                let mut keyed = open(&path);
                let volume = keyed_volume(&mut keyed, "vault", cs, EncMode::Convergent);
                let mid = mount(&mut keyed, &volume, &pin_options());
                ops::create_entry(&mut keyed, &mid, "/secret.txt", Some(b"TOP SECRET"))
                    .expect("seed");
                keyed.close().expect("close");
                (vec![open(&path)], named([("default", 0, mid)]))
            }
            "keyed_cipher_plain_volume" => {
                // the mirror: a connection holding a volume key, pointed at a
                // mount on an UNENCRYPTED volume. Mounting the encrypted
                // volume second is what leaves the plain volume's mount stale.
                let mut db = open(&path);
                let plain = plain_volume(&mut db, "open", cs);
                let plain_mount = mount(&mut db, &plain, &MountOptions::default());
                ops::create_entry(
                    &mut db,
                    &plain_mount,
                    "/plain.txt",
                    Some(b"readable by anyone"),
                )
                .expect("seed");
                let vault = keyed_volume(&mut db, "vault", cs, EncMode::Convergent);
                mount(&mut db, &vault, &pin_options()); // installs a chunk cipher connection-wide
                (vec![db], named([("default", 0, plain_mount)]))
            }
            "two_entries_same_bytes_convergent" | "two_entries_same_bytes_random" => {
                let mode = if harness.ends_with("random") {
                    EncMode::Random
                } else {
                    EncMode::Convergent
                };
                let mut db = open(&path);
                let volume = keyed_volume(&mut db, "vault", cs, mode);
                let mid = mount(&mut db, &volume, &pin_options());
                ops::create_entry(&mut db, &mid, "/a.bin", Some(b"identical payload"))
                    .expect("seed");
                ops::create_entry(&mut db, &mid, "/b.bin", Some(b"identical payload"))
                    .expect("seed");
                (vec![db], named([("default", 0, mid)]))
            }
            "two_mounts_one_volume" => {
                // two mounts on one volume, on separate CONNECTIONS: a lock is
                // mount-scoped (ACC-6) and contention is only meaningful
                // between them
                let mut first = open(&path);
                let volume = plain_volume(&mut first, "conformance", cs);
                let mut second = open(&path);
                let a = mount(&mut first, &volume, &MountOptions::default());
                // overlapping rw is what these scenarios TEST (allow_overlap, D-4)
                let b = mount(
                    &mut second,
                    &volume,
                    &MountOptions {
                        allow_overlap: true,
                        ..Default::default()
                    },
                );
                (
                    vec![first, second],
                    named([("first", 0, a), ("second", 1, b)]),
                )
            }
            _ => return None,
        };
        Some(Rig {
            dbs,
            mounts,
            _scratch: scratch,
        })
    }

    /// The mount a step without `via` acts through.
    pub fn default_via(&self) -> &'static str {
        if self.mounts.contains_key("default") {
            "default"
        } else {
            "first"
        }
    }

    /// The connection and mount id behind a `via` name.
    pub fn target(&mut self, via: &str) -> Option<(&mut Db, MountId)> {
        let (idx, mid) = self.mounts.get(via)?.clone();
        Some((&mut self.dbs[idx], mid))
    }

    /// Just the connection behind a `via` name (for descriptor steps).
    pub fn db_for(&mut self, via: &str) -> Option<&mut Db> {
        let (idx, _) = self.mounts.get(via)?;
        Some(&mut self.dbs[*idx])
    }

    /// The first connection: where inspections look.
    pub fn primary(&self) -> &Db {
        &self.dbs[0]
    }

    pub fn close(self) {
        for db in self.dbs {
            let _ = db.close();
        }
        // _scratch drops last and removes the files
    }
}

// ---------------------------------------------------------------------------
// depth
// ---------------------------------------------------------------------------

fn named<const N: usize>(
    entries: [(&str, usize, MountId); N],
) -> BTreeMap<String, (usize, MountId)> {
    entries
        .into_iter()
        .map(|(name, idx, mid)| (name.to_owned(), (idx, mid)))
        .collect()
}

fn plain_volume(db: &mut Db, name: &str, chunk_size: usize) -> VolumeId {
    expect(
        "create_volume",
        ops::create_volume(db, Some(name), chunk_size, None, EncMode::Convergent),
    )
    .id
}

fn keyed_volume(db: &mut Db, name: &str, chunk_size: usize, mode: EncMode) -> VolumeId {
    expect(
        "create_volume",
        ops::create_volume(db, Some(name), chunk_size, Some(PIN), mode),
    )
    .id
}

fn pin_options() -> MountOptions<'static> {
    MountOptions {
        pin: Some(PIN),
        ..Default::default()
    }
}

fn mount(db: &mut Db, volume: &VolumeId, opts: &MountOptions<'_>) -> MountId {
    expect("mount", ops::mount(db, volume, opts))
}

fn expect<T>(what: &str, r: Result<T>) -> T {
    r.unwrap_or_else(|e| panic!("harness {what}: {e}"))
}
