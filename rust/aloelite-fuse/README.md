# aloelite-fuse

Mount an Aloelite volume as a Linux directory, over
[`fuser`](https://crates.io/crates/fuser). Native and Linux only: there is
no kernel FUSE interface in a browser or a WASI component. A port of
`aloelite/fuse.py`, handler for handler.

## Use

```sh
# a plain volume (single-volume file: -v optional)
aloelite-fuse -f photos.fs /mnt/photos

# an encrypted volume (new or existing)
aloelite-fuse -f vault.fs -v vault --create --pin-env VAULT_PIN /mnt/vault

# unmount
fusermount3 -u /mnt/photos      # or Ctrl-C the daemon
```

Flags mirror the Python entry point: `-f/--file` (or `$ALOELITE_FILE`),
`-v/--volume`, `--create`, `--ro`, `--allow-other`, `--pin` / `--pin-file` /
`--pin-env`, `--debug`. The one difference: a bare `--pin` with no value is an
error here (use `--pin-file`/`--pin-env`, or give the value) rather than an
interactive prompt.

## The handle model

The part worth understanding, and what `tests/mount.rs` pins. How a file is
opened decides how its bytes move:

| open as | handle | behaviour |
|---|---|---|
| `O_RDONLY` | ranged stream reader | reads straight from the engine, one chunk at a time; other handles' pending writes are committed first so the read is coherent |
| `O_WRONLY \| O_TRUNC` | sequential stream writer | writes straight to the engine (bounded memory for any size); a non-sequential write is `ENOTSUP`; `flush` commits and the handle becomes random-access so a dup'd fd keeps working |
| `O_WRONLY \| O_APPEND` | append batcher | buffered, committed per 1 MiB and on flush; a reader that arrives mid-batch still sees the bytes |
| `O_RDWR`, or a partial `O_WRONLY` | dirty-extent overlay, **one per inode** shared by every such handle | writes buffer as sorted extents flushed as atomic `write_range`s; reads overlay them on committed content |

Memory is bounded by dirty bytes, never file size. `getattr` reports a size
overlaid with unflushed state, so `fstat` agrees with what a second fd can
read — the coherence `git push`'s `index-pack` depends on.

## Compatibility

`doc/COMPATIBILITY.md` is the table; every row is re-established here against
a live kernel mount by `tests/mount.rs` (the Rust twin of the reference's
`tests/test_fuse_mount.py` and `tests/test_posix_surface.py`). Hardlinks,
symlinks, fifos, `user.*` xattrs, real `uid`/`gid`/`mode`, nanosecond times,
sparse writes, and cross-handle coherence all hold; device nodes are refused
(`EPERM`, D-3).

Deliberately at their `fuser` defaults, matching the reference so the table
reads the same for both daemons:

- **`fallocate`** — no handler; glibc emulates `posix_fallocate` with zero
  writes (correct, slower).
- **`lseek`** — no handler; `SEEK_HOLE`/`SEEK_DATA` report the whole file as
  data (correct for readers, no hole enumeration).
- **`getlk`/`setlk`** — POSIX byte-range locks stay kernel-arbitrated **per
  mount**, as in the reference. Claiming lock support would move intra-mount
  arbitration into the daemon too; wiring `fcntl` through to the engine's
  cross-mount locks (which exist — ACC-11) is the D-4 upgrade, a recorded
  follow-up rather than part of this port.

## Tests

```sh
cargo test -p aloelite-fuse            # unit tests, plus the live mount if /dev/fuse is usable
```

`tests/mount.rs` self-skips (prints why, passes) when the environment cannot
mount FUSE — no `/dev/fuse`, or no privilege and no `fusermount3` — so a real
daemon regression fails while an un-mountable box does not. CI installs
`fuse3` so the rows are verified on every push.
