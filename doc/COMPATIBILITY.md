# Compatibility — what works on a mount, verified

<div align="center">

<img src="https://raw.githubusercontent.com/Aloecraft-org/aloelite/refs/heads/main/doc/aloelite.png" style="height:96px; width:96px;"/>

**Aloelite Single-File Filesystem**

[Overview](/README.md) | [Getting Started](/doc/GETTING_STARTED.md) | [Frequently Asked Questions](/doc/FAQ.md)

[Troubleshooting](/doc/TROUBLESHOOTING.md) | [Requirements Spec](/doc/REQUIREMENTS.md) | [Encryption Spec](/doc/ENCRYPTION.md)
</div>

Every ✅ row below is pinned by a test named in the table, run in CI on a
real kernel mount (`tests/test_fuse_mount.py` and
`tests/test_posix_surface.py`; both self-skip without `/dev/fuse`). Nothing
here is aspirational: if a row's behavior regressed, CI would go red. The
goal is zero *surprising* asterisks — the caveats that remain are stated
plainly, with the reason.

The structural background for several rows: pyfuse3 exposes no lock,
fallocate, lseek, or copy_file_range handlers, so those syscalls never reach
the Python daemon. The kernel fills in per-mount behavior for locks and mmap;
what cannot be filled in is *cross-mount* coordination, which is the Rust
engine's charter (doc/DECISIONS.md D-4).

## Applications

| Workload | Status | Verified by |
|---|---|---|
| **git** (active repo: init, add, commit; push into a bare repo on the mount through `index-pack`, `fsck --strict` clean) | ✅ | `test_git_push_pack_over_unpack_limit` — exercises git's `link()`-based object finalization and read-back-while-writing |
| **sqlite** database on a mount, rollback journal | ✅ | `test_sqlite_database_on_mount[delete]` |
| **sqlite** database on a mount, **WAL**, including a second process reading concurrently (the `-shm` shared-memory path) | ✅ | `test_sqlite_database_on_mount[wal]`, `test_sqlite_wal_concurrent_second_process` |
| **aloelite nested on aloelite** — a volume file living on a mount, two connections, durable across reopen (negotiates WAL; the historical PERSIST downgrade no longer applies) | ✅ | `test_aloelite_nested_on_its_own_mount` |
| Editors / tools using write-temp-then-rename atomicity | ✅ | git ref updates in the test above; `rename` is atomic in one engine transaction |

## Syscall surface

| Feature | Status | Notes / verified by |
|---|---|---|
| Byte-range locks (`fcntl`) | ✅ per mount | Kernel-arbitrated. Conflicting range denied, disjoint granted, across processes — `test_fcntl_range_locks_arbitrate_within_mount`. **Not coordinated across separate mounts of one volume** (locks are advisory everywhere; processes on different mounts are not one cooperation domain — D-4) |
| `flock` | ✅ per mount | `test_flock_arbitrates_within_mount`; same cross-mount caveat |
| `mmap` MAP_SHARED | ✅ per mount | Dirty pages reach the daemon via msync/writeback; cross-process coherent on one mount — `test_mmap_shared_*`. Cross-mount coherence bounded by the 1s attr TTL + open-time cache drop |
| Hardlinks (`link`) | ✅ | Shared node, correct `st_nlink`, unlink-one-keeps-other — `test_hardlinks` (era 2, D-5) |
| Symlinks | ✅ | First-class node type (era 2); era-1 metadata-flagged symlinks still read |
| FIFOs / sockets (`mknod`) | ✅ | `test_mkfifo_and_device_refusal` (era 2, D-3) |
| Device nodes | ❌ refused (EPERM) | By decision (D-3), not omission: a security surface with no identified use case; a future era can widen the same guard trigger |
| xattrs (`user.*`) | ✅ | Binary-safe round-trip — `test_xattrs`. `security./trusted./system.` namespaces are ENOTSUP: they carry kernel-enforced semantics this filesystem does not implement |
| chmod / chown | ✅ | Real `uid`/`gid`/`mode` columns (era 2); `ctime` bumps, `mtime` does not — `test_ownership_and_times_are_real_columns` |
| `utimens` (mtime + atime) | ✅ ns precision | Timestamps are stored in nanoseconds (era 2). No per-read atime updates (`noatime` semantics; an explicit `touch -a` works) |
| Sparse writes (seek past EOF) | ✅ | Gap reads back as zeros from the daemon — `test_seek_past_eof_write_zero_fills`. Zero chunks dedup in the content-addressed pool, so storage cost is one chunk |
| `posix_fallocate` | ✅ via glibc fallback | No fallocate handler exists in pyfuse3; glibc emulates with zero writes (correct, slower) — `test_posix_fallocate_extends_via_glibc_fallback` |
| `SEEK_HOLE`/`SEEK_DATA` | ⚠️ degrades | No lseek handler in pyfuse3: the kernel reports the whole file as data. Correct for readers, no hole enumeration |
| Read-only mounts | ✅ | `aloelite-fuse --ro`: kernel `ro` option plus the engine's D-4 gate (EROFS on anything that slips past) |

## Known-unsupported (the honest asterisks)

| Feature | Why |
|---|---|
| Cross-mount POSIX lock coherence | The gap is the FUSE path, not the engine: pyfuse3 exposes no lock handlers, so an application's `fcntl` never reaches the daemon and cannot be routed to the engine's cross-mount locks — which **do** exist (`lock`/`unlock`/`renew_lock`, ACC-11) and are what WebDAV class 2 arbitrates with. Wiring the two together is the Rust engine's upgrade — D-4, as amended. The default mount policy (one rw mount per subtree, overlap by explicit opt-in) keeps the gap from surprising anyone |
| Cross-mount `mmap MAP_SHARED` coherence | Same page cache is per mount; staleness bounded by the attr TTL, not eliminated |
| `copy_file_range` | No pyfuse3 handler; the kernel falls back to read/write loops (correct, no reflink speedup) |
| Random writes on an `O_WRONLY\|O_TRUNC` streaming handle | ENOTSUP by design; open O_RDWR for random access (see `aloelite/fuse.py` module docs) |

## Engine-level guarantees that back the table

- Content commits are atomic per entry and serialized by the schema-backed
  entry write lock **across mounts and processes** (CV-3, ACC-6/7) — the
  cross-mount story for *aloelite's own* atomicity is not subject to the
  per-mount caveats above.
- Handle coherence within a mount (bytes written through one fd visible to
  every other fd before close, sizes consistent with fstat) is pinned by
  `tests/test_fuse_mount.py`, with the page cache dropped so the tests prove
  daemon behavior, not cache behavior.
- Id ordering under concurrent mounts follows the D-2 contract (strict per
  mount; strict across mounts beyond the coordination window; arbitrary
  within it), fenced against clock regression and failover skew by the
  volume high-water mark — `tests/test_ids.py`.
