//! Mounting: open the engine, mount the volume, hand `fuser` the filesystem,
//! renew the lease while alive, and unmount cleanly.
//!
//! The lease matters (ACC-3): the daemon's engine mount carries a TTL and a
//! background thread renews it, so a crashed daemon's mount row expires
//! rather than blocking the volume's rw-per-subtree admission (D-4) forever.
//! The renew thread shares the one engine connection through the same mutex
//! as the FUSE handlers, so the connection is never touched concurrently.
//!
//! [`Mount::spawn`] returns while the filesystem serves in the background —
//! what a test drives. [`serve`] adds the signal wait for the `aloelite-fuse`
//! binary.

use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use aloelite_core::FsError;
use aloelite_core::crypto::EncMode;
use aloelite_core::ops::{self, MountOptions};
use aloelite_core::types::{Access, VolumeId};
use aloelite_store::StoreError;

use crate::fs::{AloeFuse, Inner};

// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------
//
// Entry points: Mount::spawn (background), Mount::unmount, serve (blocking).
// Configurable: MOUNT_TTL_MS, RENEW_EVERY, DEFAULT_CHUNK_SIZE.

/// The engine mount's lease, renewed while the daemon lives.
pub const MOUNT_TTL_MS: i64 = 300_000;

/// How often the lease is renewed; comfortably inside [`MOUNT_TTL_MS`].
pub const RENEW_EVERY: Duration = Duration::from_secs(60);

/// A volume created by `--create` chunks at 1 MiB, the spec default.
pub const DEFAULT_CHUNK_SIZE: usize = 1 << 20;

/// What mounting can go wrong on.
#[derive(Debug, thiserror::Error)]
pub enum DaemonError {
    #[error(transparent)]
    Engine(#[from] FsError),
    #[error(transparent)]
    Store(#[from] StoreError),
    #[error(transparent)]
    Io(#[from] std::io::Error),
    #[error("{0}")]
    Config(String),
}

/// What to mount and how.
pub struct Options<'a> {
    /// The `.fs`/`.sqlite` file holding the volume.
    pub file: &'a Path,
    /// Volume name within the file.
    pub volume: &'a str,
    /// An empty directory to mount at.
    pub mountpoint: &'a Path,
    /// PIN for an encrypted volume (and to create one).
    pub pin: Option<&'a [u8]>,
    /// `rw` (default) or `ro` — both the kernel option and the D-4 gate.
    pub access: Access,
    /// Create the volume if the file has none by that name.
    pub create: bool,
    /// Let other UIDs reach the mount (consumer containers).
    pub allow_other: bool,
}

/// A running mount. Dropping it unmounts; [`Mount::unmount`] does the same
/// and surfaces any error.
pub struct Mount {
    session: Option<fuser::BackgroundSession>,
    inner: Option<Arc<Mutex<Inner>>>,
    renew_stop: Arc<AtomicBool>,
    renew: Option<JoinHandle<()>>,
}

impl Mount {
    /// Open the engine, mount the volume, and start serving in the
    /// background. Returns once the mount is live.
    pub fn spawn(opts: &Options) -> Result<Mount, DaemonError> {
        if !opts.file.exists() && !opts.create {
            return Err(DaemonError::Config(format!(
                "{}: no such file (pass create for a new one)",
                opts.file.display()
            )));
        }
        let mut db = aloelite_store::file::open(opts.file)?;
        let volume = find_or_create_volume(&mut db, opts.volume, opts.pin, opts.create)?;
        let mount = ops::mount(
            &mut db,
            &volume,
            &MountOptions {
                at: "/",
                ttl_ms: Some(MOUNT_TTL_MS),
                pin: opts.pin,
                access: opts.access,
                principal: None,
                allow_overlap: false,
            },
        )?;
        let fs = AloeFuse::new(db, mount)?;
        let inner = fs.inner.clone();

        let renew_stop = Arc::new(AtomicBool::new(false));
        let renew = spawn_renew(inner.clone(), renew_stop.clone());

        let mut cfg = fuser::Config::default();
        cfg.mount_options = vec![fuser::MountOption::FSName("aloefuse".to_owned())];
        if opts.access == Access::Ro {
            cfg.mount_options.push(fuser::MountOption::RO);
        }
        if opts.allow_other {
            cfg.acl = fuser::SessionACL::All;
        }

        // On a mount failure, the renew thread and engine must still be torn
        // down; do it here rather than leaking them.
        let session = match fuser::spawn_mount(fs, opts.mountpoint, &cfg) {
            Ok(s) => s,
            Err(e) => {
                renew_stop.store(true, Ordering::Relaxed);
                let _ = renew.join();
                if let Ok(mutex) = Arc::try_unwrap(inner) {
                    let _ = mutex
                        .into_inner()
                        .unwrap_or_else(|p| p.into_inner())
                        .finish();
                }
                return Err(e.into());
            }
        };
        Ok(Mount {
            session: Some(session),
            inner: Some(inner),
            renew_stop,
            renew: Some(renew),
        })
    }

    /// Unmount, stop the lease renewal, and close the engine.
    pub fn unmount(mut self) -> Result<(), DaemonError> {
        self.teardown()
    }

    fn teardown(&mut self) -> Result<(), DaemonError> {
        // 1. unmount the kernel filesystem and join fuser's thread — this
        //    drops the AloeFuse it owns, releasing that reference to `inner`.
        if let Some(session) = self.session.take() {
            session.umount_and_join()?;
        }
        // 2. stop the lease thread and join it, releasing its reference.
        self.renew_stop.store(true, Ordering::Relaxed);
        if let Some(h) = self.renew.take() {
            let _ = h.join();
        }
        // 3. `inner` is now the sole owner: recover the engine and close it.
        if let Some(arc) = self.inner.take() {
            match Arc::try_unwrap(arc) {
                Ok(mutex) => {
                    let inner = mutex.into_inner().unwrap_or_else(|p| p.into_inner());
                    inner.finish()?;
                }
                Err(arc) => {
                    // An unexpected extra reference survives; best-effort.
                    arc.lock().unwrap_or_else(|p| p.into_inner()).unmount_only();
                }
            }
        }
        Ok(())
    }
}

impl Drop for Mount {
    fn drop(&mut self) {
        let _ = self.teardown();
    }
}

/// Mount and serve until a termination signal (SIGINT/SIGTERM) or an
/// external unmount, then tear down. What the `aloelite-fuse` binary runs.
pub fn serve(opts: &Options) -> Result<(), DaemonError> {
    install_signal_handlers();
    let mount = Mount::spawn(opts)?;
    let mountpoint = opts.mountpoint.to_owned();
    while !STOP.load(Ordering::Relaxed) && is_mounted(&mountpoint) {
        thread::sleep(Duration::from_millis(200));
    }
    mount.unmount()
}

// ---------------------------------------------------------------------------
// depth
// ---------------------------------------------------------------------------

fn find_or_create_volume(
    db: &mut aloelite_core::Db,
    name: &str,
    pin: Option<&[u8]>,
    create: bool,
) -> Result<VolumeId, DaemonError> {
    for v in ops::list_volumes(db)? {
        if v.name.as_deref() == Some(name) {
            return Ok(v.id);
        }
    }
    if !create {
        return Err(DaemonError::Config(format!(
            "no volume named {name:?} in this file (pass create to bootstrap one)"
        )));
    }
    // pin=None yields a plain volume regardless of enc_mode (create_volume
    // installs the identity cipher when there is no PIN).
    Ok(ops::create_volume(db, Some(name), DEFAULT_CHUNK_SIZE, pin, EncMode::Convergent)?.id)
}

fn spawn_renew(inner: Arc<Mutex<Inner>>, stop: Arc<AtomicBool>) -> JoinHandle<()> {
    thread::spawn(move || {
        let mut waited = Duration::ZERO;
        while !stop.load(Ordering::Relaxed) {
            thread::sleep(Duration::from_millis(200));
            waited += Duration::from_millis(200);
            if waited < RENEW_EVERY {
                continue;
            }
            waited = Duration::ZERO;
            let _ = inner
                .lock()
                .unwrap_or_else(|p| p.into_inner())
                .renew(MOUNT_TTL_MS);
        }
    })
}

/// True while `mountpoint` appears in the kernel's mount table.
fn is_mounted(mountpoint: &Path) -> bool {
    let Ok(contents) = std::fs::read_to_string("/proc/mounts") else {
        return true; // cannot tell: assume still mounted, wait for the signal
    };
    let want = mountpoint.to_string_lossy();
    contents
        .lines()
        .filter_map(|l| l.split_whitespace().nth(1))
        .any(|p| p == want)
}

static STOP: AtomicBool = AtomicBool::new(false);

extern "C" fn on_signal(_sig: i32) {
    STOP.store(true, Ordering::Relaxed);
}

fn install_signal_handlers() {
    // SAFETY: on_signal only stores into an AtomicBool, which is
    // async-signal-safe.
    unsafe {
        libc::signal(libc::SIGINT, on_signal as *const () as libc::sighandler_t);
        libc::signal(libc::SIGTERM, on_signal as *const () as libc::sighandler_t);
    }
}
