//! The FUSE daemon: an Aloelite volume as a directory, over `fuser`.
//!
//! A port of `aloelite/fuse.py`, handler for handler, with the same handle
//! model — the part of that file that took the longest to get right and is
//! pinned by `tests/test_fuse_mount.py`'s coherence cases:
//!
//! | open as | handle | bytes go |
//! |---|---|---|
//! | `O_RDONLY` | ranged stream reader | straight from the engine, one chunk at a time; other handles' pending state is settled first |
//! | `O_WRONLY \| O_TRUNC` | sequential stream writer | straight to the engine; a non-sequential write is `ENOTSUP`, and `flush` commits and converts the handle to random access |
//! | `O_WRONLY \| O_APPEND` | append batcher | buffered, committed per 1 MiB batch and on flush |
//! | `O_RDWR`, or plain `O_WRONLY` | dirty-extent overlay, ONE per inode shared by every such handle | buffered as sorted extents, flushed as atomic `write_range`s; reads overlay them on committed content |
//!
//! Memory is bounded by dirty bytes, never by file size. Sizes reported by
//! `getattr` are overlaid with unflushed state so `fstat` agrees with what a
//! second fd can read — the property `git push` needs.
//!
//! What `fuser` adds over pyfuse3, and what this daemon does with it:
//! `fallocate` and `lseek` are real handlers (see `doc/COMPATIBILITY.md`);
//! `getlk`/`setlk` are deliberately NOT implemented yet — answering them
//! means owning intra-mount byte-range arbitration too, since the kernel
//! stops doing it the moment a filesystem claims lock support — so POSIX
//! locks stay kernel-arbitrated per mount, as in the reference, and the
//! cross-mount upgrade D-4 describes is a recorded follow-up.
//!
//! Layout: [`overlay`] is the dirty-extent state, portable and unit-tested;
//! [`errno`] and [`inode`] are the two small mappings; [`fs`] is the
//! `Filesystem` impl; [`daemon`] mounts, renews the engine lease and
//! unmounts on a signal; [`cli`] is `aloelite-fuse`'s command line, the same
//! flags as the Python entry point.

pub mod cli;
pub mod errno;
pub mod inode;
pub mod overlay;

#[cfg(target_os = "linux")]
pub mod daemon;
#[cfg(target_os = "linux")]
pub mod fs;
