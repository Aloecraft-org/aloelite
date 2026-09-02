//! The FUSE daemon.
//!
//! `fuser` is the reason a Rust FUSE layer is worth having at all: unlike
//! pyfuse3 it exposes `getlk`/`setlk`, `lseek`, `fallocate` and
//! `copy_file_range`. That is the D-4 cross-mount lock upgrade (POSIX
//! `fcntl` routed through to the ACC-11 engine locks that already exist)
//! plus every ⚠️/❌ row in doc/COMPATIBILITY.md, unlocked by the binding
//! rather than by cleverness.
//!
//! This crate has no portable oracle. `tests/test_posix_surface.py` and
//! `tests/test_fuse_mount.py` are ~660 lines of pytest against a live
//! kernel mount, and they are the only thing pinning COMPATIBILITY.md; every
//! row has to be re-established here by hand. That is the single largest
//! cost in the port and it is budgeted as such (doc/RUST_PORT.md).
//!
//! Scaffold: no daemon yet.
