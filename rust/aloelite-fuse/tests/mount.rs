//! The compatibility table (`doc/COMPATIBILITY.md`) re-established against a
//! live kernel mount — the port of `tests/test_fuse_mount.py` and
//! `tests/test_posix_surface.py`, which are the only oracle this crate has.
//!
//! Self-skipping like the reference suite: without `/dev/fuse`, or in an
//! environment that cannot mount FUSE (no privilege, no `fusermount3`), the
//! test prints why and returns rather than failing — a real daemon
//! regression still fails, an un-mountable CI box does not.
//!
//! Not ported here (they need external tools or heavier setup, and are held
//! by the Python suite): the `git push` end-to-end, sqlite-on-mount, and
//! `mmap MAP_SHARED` cross-process coherence.
#![cfg(target_os = "linux")]

use std::ffi::CString;
use std::io::Write;
use std::os::unix::fs::{FileTypeExt, MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU32, Ordering};
use std::time::{Duration, Instant};

use aloelite_core::types::Access;
use aloelite_fuse::daemon::{Mount, Options};

// ---------------------------------------------------------------------------
// harness
// ---------------------------------------------------------------------------

static SEQ: AtomicU32 = AtomicU32::new(0);

fn scratch(label: &str) -> PathBuf {
    let n = SEQ.fetch_add(1, Ordering::Relaxed);
    let dir =
        std::env::temp_dir().join(format!("aloelite-fuse-{label}-{}-{n}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

/// A live mount, or `None` when the environment cannot provide one.
struct Mounted {
    mp: PathBuf,
    mount: Option<Mount>,
    root: PathBuf,
}

impl Mounted {
    fn start(label: &str) -> Option<Mounted> {
        if !Path::new("/dev/fuse").exists() {
            eprintln!("skip {label}: /dev/fuse not available");
            return None;
        }
        let root = scratch(label);
        let file = root.join("test.fs");
        let mp = root.join("mnt");
        std::fs::create_dir_all(&mp).unwrap();
        let opts = Options {
            file: &file,
            volume: "data",
            mountpoint: &mp,
            pin: None,
            access: Access::Rw,
            create: true,
            allow_other: false,
        };
        match Mount::spawn(&opts) {
            Ok(mount) => {
                let deadline = Instant::now() + Duration::from_secs(5);
                while Instant::now() < deadline && !is_mounted(&mp) {
                    std::thread::sleep(Duration::from_millis(50));
                }
                assert!(is_mounted(&mp), "mount did not appear");
                Some(Mounted {
                    mp,
                    mount: Some(mount),
                    root,
                })
            }
            Err(e) => {
                eprintln!("skip {label}: cannot mount FUSE here ({e})");
                std::fs::remove_dir_all(&root).ok();
                None
            }
        }
    }

    fn path(&self, name: &str) -> PathBuf {
        self.mp.join(name)
    }
}

impl Drop for Mounted {
    fn drop(&mut self) {
        if let Some(mount) = self.mount.take() {
            let _ = mount.unmount();
        }
        std::fs::remove_dir_all(&self.root).ok();
    }
}

fn is_mounted(mp: &Path) -> bool {
    std::fs::read_to_string("/proc/mounts")
        .map(|s| {
            let want = mp.to_string_lossy();
            s.lines()
                .filter_map(|l| l.split_whitespace().nth(1))
                .any(|p| p == want)
        })
        .unwrap_or(false)
}

/// A raw fd with fadvise/pread/pwrite, so a read proves what the daemon
/// served rather than what the page cache holds.
struct Fd(i32);

impl Fd {
    fn open(path: &Path, flags: i32) -> Fd {
        let c = CString::new(path.as_os_str().as_encoded_bytes()).unwrap();
        // 0o644 for O_CREAT
        let fd = unsafe { libc::open(c.as_ptr(), flags, 0o644 as libc::c_uint) };
        assert!(
            fd >= 0,
            "open {path:?}: {}",
            std::io::Error::last_os_error()
        );
        Fd(fd)
    }

    fn write_at(&self, off: i64, data: &[u8]) -> isize {
        let n = unsafe { libc::pwrite(self.0, data.as_ptr().cast(), data.len(), off) };
        assert!(n >= 0, "pwrite: {}", std::io::Error::last_os_error());
        n
    }

    fn write(&self, data: &[u8]) -> isize {
        let n = unsafe { libc::write(self.0, data.as_ptr().cast(), data.len()) };
        assert!(n >= 0, "write: {}", std::io::Error::last_os_error());
        n
    }

    /// Read `len` bytes at `off`, dropping cached pages first.
    fn read_uncached(&self, off: i64, len: usize) -> Vec<u8> {
        unsafe { libc::posix_fadvise(self.0, 0, 0, libc::POSIX_FADV_DONTNEED) };
        let mut buf = vec![0u8; len];
        let n = unsafe { libc::pread(self.0, buf.as_mut_ptr().cast(), len, off) };
        assert!(n >= 0, "pread: {}", std::io::Error::last_os_error());
        buf.truncate(n as usize);
        buf
    }

    fn size(&self) -> u64 {
        let mut st: libc::stat = unsafe { std::mem::zeroed() };
        assert_eq!(unsafe { libc::fstat(self.0, &mut st) }, 0);
        st.st_size as u64
    }
}

impl Drop for Fd {
    fn drop(&mut self) {
        unsafe { libc::close(self.0) };
    }
}

// ---------------------------------------------------------------------------
// cross-handle coherence — the incident the shared overlay was written for
// ---------------------------------------------------------------------------

#[test]
fn a_second_fd_sees_unflushed_writes() {
    let Some(m) = Mounted::start("second-fd") else {
        return;
    };
    let p = m.path("t.bin");
    let w = Fd::open(&p, libc::O_RDWR | libc::O_CREAT | libc::O_TRUNC);
    w.write(&[b'A'; 60000]);
    let r = Fd::open(&p, libc::O_RDONLY);
    assert_eq!(w.size(), 60000);
    assert_eq!(r.size(), 60000);
    assert_eq!(r.read_uncached(0, 70000), vec![b'A'; 60000]);
}

#[test]
fn concurrent_rw_handles_do_not_lose_updates() {
    let Some(m) = Mounted::start("concurrent-rw") else {
        return;
    };
    let p = m.path("two.bin");
    let w1 = Fd::open(&p, libc::O_RDWR | libc::O_CREAT | libc::O_TRUNC);
    let w2 = Fd::open(&p, libc::O_RDWR);
    w1.write_at(0, &[b'X'; 100]);
    w2.write_at(200, &[b'Y'; 100]);
    drop(w1);
    drop(w2);
    let r = Fd::open(&p, libc::O_RDONLY);
    let mut want = vec![b'X'; 100];
    want.extend(std::iter::repeat_n(0u8, 100));
    want.extend(std::iter::repeat_n(b'Y', 100));
    assert_eq!(r.read_uncached(0, 400), want);
}

#[test]
fn an_append_batch_is_visible_before_close() {
    let Some(m) = Mounted::start("append") else {
        return;
    };
    let p = m.path("log.txt");
    let a = Fd::open(&p, libc::O_WRONLY | libc::O_CREAT | libc::O_APPEND);
    a.write(b"line1\n");
    let r = Fd::open(&p, libc::O_RDONLY);
    assert_eq!(r.read_uncached(0, 100), b"line1\n");
}

#[test]
fn a_plain_write_close_reopen_round_trips() {
    let Some(m) = Mounted::start("roundtrip") else {
        return;
    };
    let p = m.path("clean.bin");
    {
        let mut f = std::fs::File::create(&p).unwrap();
        f.write_all(&[b'Q'; 60000]).unwrap();
    }
    let r = Fd::open(&p, libc::O_RDONLY);
    assert_eq!(r.read_uncached(0, 70000), vec![b'Q'; 60000]);
}

// ---------------------------------------------------------------------------
// the POSIX surface
// ---------------------------------------------------------------------------

#[test]
fn directories_and_listing() {
    let Some(m) = Mounted::start("dirs") else {
        return;
    };
    std::fs::create_dir(m.path("d")).unwrap();
    std::fs::write(m.path("d/f"), b"hi").unwrap();
    let mut names: Vec<String> = std::fs::read_dir(m.path("d"))
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    names.sort();
    assert_eq!(names, vec!["f"]);
    assert_eq!(std::fs::read(m.path("d/f")).unwrap(), b"hi");
    std::fs::remove_file(m.path("d/f")).unwrap();
    std::fs::remove_dir(m.path("d")).unwrap();
    assert!(!m.path("d").exists());
}

#[test]
fn hardlinks_share_a_node_and_count_placements() {
    let Some(m) = Mounted::start("hardlink") else {
        return;
    };
    let a = m.path("a");
    let b = m.path("b");
    std::fs::write(&a, b"original").unwrap();
    std::fs::hard_link(&a, &b).unwrap();
    let ino_a = std::fs::metadata(&a).unwrap().ino();
    let ino_b = std::fs::metadata(&b).unwrap().ino();
    assert_eq!(ino_a, ino_b);
    assert_eq!(std::fs::metadata(&a).unwrap().nlink(), 2);
    std::fs::write(&b, b"rewritten through b").unwrap();
    let r = Fd::open(&a, libc::O_RDONLY);
    assert_eq!(r.read_uncached(0, 64), b"rewritten through b");
    std::fs::remove_file(&a).unwrap();
    assert!(!a.exists());
    assert_eq!(std::fs::read(&b).unwrap(), b"rewritten through b");
    assert_eq!(std::fs::metadata(&b).unwrap().nlink(), 1);
}

#[test]
fn symlinks_are_first_class() {
    let Some(m) = Mounted::start("symlink") else {
        return;
    };
    std::fs::write(m.path("target"), b"payload").unwrap();
    std::os::unix::fs::symlink("target", m.path("link")).unwrap();
    assert_eq!(
        std::fs::read_link(m.path("link")).unwrap(),
        Path::new("target")
    );
    // following the link reads the target's content
    assert_eq!(std::fs::read(m.path("link")).unwrap(), b"payload");
}

#[test]
fn fifos_are_made_and_device_nodes_refused() {
    let Some(m) = Mounted::start("mknod") else {
        return;
    };
    let fifo = CString::new(m.path("fifo").as_os_str().as_encoded_bytes()).unwrap();
    assert_eq!(unsafe { libc::mkfifo(fifo.as_ptr(), 0o640) }, 0);
    let md = std::fs::metadata(m.path("fifo")).unwrap();
    assert!(md.file_type().is_fifo());
    assert_eq!(md.permissions().mode() & 0o7777, 0o640);
    // a device node is refused with EPERM (D-3)
    let dev = CString::new(m.path("dev").as_os_str().as_encoded_bytes()).unwrap();
    let rc = unsafe { libc::mknod(dev.as_ptr(), libc::S_IFBLK | 0o600, libc::makedev(1, 3)) };
    assert_eq!(rc, -1);
    assert_eq!(
        std::io::Error::last_os_error().raw_os_error(),
        Some(libc::EPERM)
    );
}

#[test]
fn user_xattrs_round_trip_and_other_namespaces_are_refused() {
    let Some(m) = Mounted::start("xattr") else {
        return;
    };
    let p = m.path("xf");
    std::fs::write(&p, b"x").unwrap();
    set_xattr(&p, "user.test", b"value");
    set_xattr(&p, "user.other", b"\x00\xffbinary");
    assert_eq!(get_xattr(&p, "user.test"), Some(b"value".to_vec()));
    let mut names = list_xattr(&p);
    names.sort();
    assert_eq!(
        names,
        vec!["user.other".to_string(), "user.test".to_string()]
    );
    set_xattr(&p, "user.test", b"replaced");
    assert_eq!(get_xattr(&p, "user.test"), Some(b"replaced".to_vec()));
    remove_xattr(&p, "user.test");
    assert_eq!(get_xattr(&p, "user.test"), None); // ENODATA
    // a non-user namespace is refused, like the reference's pytest.raises.
    // The exact errno is the environment's, not ours: the daemon answers
    // ENOTSUP, but the kernel screens `trusted.*` for CAP_SYS_ADMIN first, so
    // an unprivileged caller is refused with EPERM before the handler runs.
    let name = CString::new("trusted.evil").unwrap();
    let cp = CString::new(p.as_os_str().as_encoded_bytes()).unwrap();
    let rc = unsafe { libc::setxattr(cp.as_ptr(), name.as_ptr(), c"x".as_ptr().cast(), 1, 0) };
    assert_eq!(rc, -1, "a non-user xattr namespace must be refused");
}

#[test]
fn ownership_and_times_are_real() {
    let Some(m) = Mounted::start("owner") else {
        return;
    };
    let p = m.path("of");
    std::fs::write(&p, b"x").unwrap();
    std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o751)).unwrap();
    assert_eq!(
        std::fs::metadata(&p).unwrap().permissions().mode() & 0o7777,
        0o751
    );
    // utimensat with nanosecond precision, preserved exactly
    let atime = 1_234_567_890_123_456_789i64;
    let mtime = 1_555_555_555_123_456_789i64;
    let times = [ts(atime), ts(mtime)];
    let cp = CString::new(p.as_os_str().as_encoded_bytes()).unwrap();
    let rc = unsafe { libc::utimensat(libc::AT_FDCWD, cp.as_ptr(), times.as_ptr(), 0) };
    assert_eq!(rc, 0, "utimensat: {}", std::io::Error::last_os_error());
    let md = std::fs::metadata(&p).unwrap();
    assert_eq!(md.mtime_nsec(), 123_456_789);
    assert_eq!(md.mtime(), 1_555_555_555);
}

#[test]
fn a_write_past_eof_zero_fills_the_gap() {
    let Some(m) = Mounted::start("sparse") else {
        return;
    };
    let p = m.path("sparse");
    let off = 1 << 20;
    let f = Fd::open(&p, libc::O_RDWR | libc::O_CREAT);
    f.write_at(off, b"END");
    drop(f);
    assert_eq!(std::fs::metadata(&p).unwrap().len(), off as u64 + 3);
    let r = Fd::open(&p, libc::O_RDONLY);
    assert_eq!(r.read_uncached(0, 4096), vec![0u8; 4096]);
    assert_eq!(r.read_uncached(off, 3), b"END");
}

#[test]
fn a_random_overwrite_lands_and_truncate_shrinks() {
    let Some(m) = Mounted::start("rw-overwrite") else {
        return;
    };
    let p = m.path("edit.bin");
    std::fs::write(&p, b"0123456789").unwrap();
    let f = Fd::open(&p, libc::O_RDWR);
    f.write_at(4, b"AB");
    drop(f);
    assert_eq!(std::fs::read(&p).unwrap(), b"0123AB6789");
    let f = std::fs::OpenOptions::new().write(true).open(&p).unwrap();
    f.set_len(4).unwrap();
    drop(f);
    assert_eq!(std::fs::read(&p).unwrap(), b"0123");
}

// ---------------------------------------------------------------------------
// xattr helpers (no std API)
// ---------------------------------------------------------------------------

fn set_xattr(path: &Path, name: &str, value: &[u8]) {
    let p = CString::new(path.as_os_str().as_encoded_bytes()).unwrap();
    let n = CString::new(name).unwrap();
    let rc = unsafe {
        libc::setxattr(
            p.as_ptr(),
            n.as_ptr(),
            value.as_ptr().cast(),
            value.len(),
            0,
        )
    };
    assert_eq!(
        rc,
        0,
        "setxattr {name}: {}",
        std::io::Error::last_os_error()
    );
}

fn get_xattr(path: &Path, name: &str) -> Option<Vec<u8>> {
    let p = CString::new(path.as_os_str().as_encoded_bytes()).unwrap();
    let n = CString::new(name).unwrap();
    let mut buf = vec![0u8; 256];
    let rc = unsafe { libc::getxattr(p.as_ptr(), n.as_ptr(), buf.as_mut_ptr().cast(), buf.len()) };
    if rc < 0 {
        return None;
    }
    buf.truncate(rc as usize);
    Some(buf)
}

fn list_xattr(path: &Path) -> Vec<String> {
    let p = CString::new(path.as_os_str().as_encoded_bytes()).unwrap();
    let mut buf = vec![0u8; 1024];
    let rc = unsafe { libc::listxattr(p.as_ptr(), buf.as_mut_ptr().cast(), buf.len()) };
    assert!(rc >= 0, "listxattr: {}", std::io::Error::last_os_error());
    buf.truncate(rc as usize);
    buf.split(|b| *b == 0)
        .filter(|s| !s.is_empty())
        .map(|s| String::from_utf8_lossy(s).into_owned())
        .collect()
}

fn remove_xattr(path: &Path, name: &str) {
    let p = CString::new(path.as_os_str().as_encoded_bytes()).unwrap();
    let n = CString::new(name).unwrap();
    let rc = unsafe { libc::removexattr(p.as_ptr(), n.as_ptr()) };
    assert_eq!(
        rc,
        0,
        "removexattr {name}: {}",
        std::io::Error::last_os_error()
    );
}

fn ts(ns: i64) -> libc::timespec {
    libc::timespec {
        tv_sec: (ns / 1_000_000_000) as libc::time_t,
        tv_nsec: (ns % 1_000_000_000) as _,
    }
}
