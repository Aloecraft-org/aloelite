# FAQ

<div align="center">

<img src="https://raw.githubusercontent.com/Aloecraft-org/aloelite/refs/heads/main/doc/aloelite.png" style="height:96px; width:96px;"/>

**Aloelite SQLite Filesystem**

[Overview](/README.md) | [Getting Started](/doc/GETTING_STARTED.md) |  **Frequently Asked Questions (This Document)** 

[Troubleshooting](/doc/TROUBLESHOOTING.md) | [Requirements Spec](/doc/REQUIREMENTS.md) | [Encryption Spec](/doc/ENCRYPTION.md) | [Roadmap](/doc/ROADMAP.md)
</div>

## Frequently Asked Questions

### Why Aloelite?

- **One file**: the whole filesystem copies, ships, and backs up as a
  single artifact
- **Encrypted at rest**: per-volume ChaCha20-Poly1305; the PIN is never
  stored
- **Deduplicated**: identical content is stored once, including across
  repeated backups
- **Atomic and versioned**: writes commit fully or not at all; history
  is kept until you prune
- **Use it your way**: Python API, CLI, browser WebUI (`aloelite-web`),
  FUSE directory, or container-volume manager over one format
- **Runs anywhere SQLite does**: which is nearly everywhere

### What is Aloelite, in one sentence?

Aloelite is a filesystem (i.e. files, folders, metadata, and content) stored in one portable artifact as a single SQLite file.

### Why would I use this instead of a plain directory?

When the filesystem needs to be a *thing you can hold*: one file to
copy, version, encrypt, ship, or hand to a container — with deduplication,
atomic versioned writes, and at-rest encryption built in.

### Why SQLite?

Portability and inherited maturity. A SQLite file opens anywhere SQLite
runs (which is nearly everywhere) and decades of work on storage
layout, crash safety, and transactional integrity come for free. Aloelite
supplies the filesystem model on top; SQLite supplies the durability.

### Is it a database? Can I query my files with SQL?

It's a filesystem that *uses* a database. You can open the file with any
SQLite tool to inspect or audit it, but the supported interface is the
Mount API (Python/CLI/FUSE/HTTP); writing to the tables directly bypasses
the invariants the API enforces.

### Where did the volume named 'main' come from?

Running `aloelite -f FILE` on a file that doesn't exist yet creates it
with one default volume named `main` (encrypted if a `--pin*` flag was
given), so the CLI works immediately without learning volume management
first. Additional volumes are created explicitly with
`aloelite [--pin] -f FILE volume create NAME`.

### Which command do I use — aloelite, aloelite-fuse, aloelite-web, aloelite-admin?

One rule: `aloelite` operates on files *inside* volumes (ls, put, cat);
`aloelite-admin` operates on volumes, keys, and the file itself (pin
change, snapshot, export, verify); `aloelite-fuse` mounts a volume as a
real directory; `aloelite-web` runs the browser manager. You only ever
need the first one installed to reach the others: `aloelite fuse ...`,
`aloelite web ...`, and `aloelite admin ...` dispatch to them.

### Why does every command on an encrypted volume take a second, and why must I re-enter the PIN?

Each invocation opens a fresh session: the PIN is stretched through
Argon2id (deliberately expensive — that cost is what makes a stolen
file resist brute force) and forgotten when the command exits. Nothing
PIN-derived is ever stored. For scripts, read the PIN once and pass it
by environment (`--pin-env`) so you type it once per script, not per
command; the KDF cost remains per command and is the honest price of
the security model.

### Is it safe to Ctrl-C a FUSE mount (or kill a writer)?

Yes. Committed versions are the only definition of current bytes, and
commits are atomic — an interrupted write leaves the previous committed
version intact and at most some staged chunks for `prune` to reclaim.
Ctrl-C on `aloelite-fuse` also unmounts cleanly on the way out. What you
lose is only the write that was in flight, never existing data.

### When does a file get created vs required to exist?

Only the bare invocation (`aloelite -f new.fs`, with or without pin
flags) creates a missing file — that is the deliberate on-ramp. Every
other command, including quick writes and `pin check`, errors on a
missing file instead of silently creating an empty one.

### What's the difference between a volume and a mount?

A **volume** is a filesystem tree with a root and everything under it. A
**mount** is a durable access point into a volume, anchored at a
specific node. All access goes through a mount, never directly at a
volume. One file can hold many volumes; one volume can have many mounts.

### Why do mounts stick around after I close my program?

A mount is a durable record by design. It can carry a
TTL, be listed later (`fs.list_mounts()`), and be re-attached to.
Retiring one (`unmount`) is permanent. Open a new mount instead.

### How does encryption work? What's actually protected?

Chunk *content* is encrypted (ChaCha20-Poly1305; the PIN is stretched
with Argon2id and never stored). Node *metadata* — names, paths,
timestamps, tree structure — is **plaintext**: someone with the file but
no PIN sees the tree and file sizes, not contents. If the tree shape is
sensitive, keep the file on an encrypted disk or `pack` before transport.

### Can I recover a forgotten PIN?

No. There is no back door, recovery key, or reset. The volume key is
sealed under your PIN and nothing else.

### Are two identical files stored twice? (Deduplication)

No. Content is chunked and stored by address in a shared pool, so
identical data is stored a single time — including across repeated
backups. Encrypted volumes keep dedup by default (convergent mode);
`enc_mode="random"` trades dedup for zero equality leakage.

### Does deleting a file shrink the .fs file? 

Not immediately. Old versions and detached subtrees are
kept until pruned (`fs.prune()`, `fs.prune_content()`), which is what
makes accidental deletion recoverable. Run `VACUUM` when you want the
bytes back.

### Is it safe to copy the .fs file while it's in use?

Copy the file directly and you may catch it mid-write. Use the export
mechanisms instead: the manager's `/volumes/<id>/export` endpoint
checkpoints and streams a consistent snapshot *while mounted*, or run
`PRAGMA wal_checkpoint(TRUNCATE)` and copy when quiescent.

### Can two processes use the same file at once?

Yes, within SQLite's rules: WAL mode lets readers coexist with one
writer, and the advisory lock layer coordinates on top. Many processes
on one machine is fine; a network share underneath the file is not
(a SQLite limitation).

### Can I run a database (like SQLite itself) inside a FUSE-mounted volume?

Yes, in rollback-journal mode — validated with concurrent readers and
writers. Use `PRAGMA journal_mode=PERSIST` (or `TRUNCATE`) and a
`busy_timeout`, and set a retention policy on the database file (see
Troubleshooting). WAL mode needs mmap support and is not there yet.
Ordinary applications work as well — validated backing a mail server
(docker-mailserver) and a full git workflow on mounted volumes.

### How do I back up a volume?

Three layers, cheapest first: the file itself is a single SQLite
database, so copying the file (while no writer is active) is a complete
backup of everything in it. Within a file,
`aloelite-admin snapshot NAME` forks a volume in place — near-free for
unencrypted volumes. Across files, `aloelite-admin export dest.fs`
copies a volume's committed state into another file with a fresh key
ladder (optionally under a different PIN), and `verify --deep` proves
the result byte-perfect. Superseded version history and retention
policies stay behind on the source; transfers carry current state.

### Can I store an aloelite file inside another aloelite volume?

Yes, two ways. As plain content (`put`/`get`) it behaves like any other
file — chunked, deduplicated across repeated backups, byte-identical on
the way out. Opened *live* on a FUSE mount of the outer volume, it also
works: the engine falls back from WAL to a rollback journal
automatically. Live nesting trades away reader/writer concurrency on
the inner file and amplifies writes into outer-volume versions, so set
a retention policy and prune the outer file if the inner one is busy
(see Troubleshooting).

### How big can files and volumes get?

Streaming I/O is bounded-memory and validated against files in the tens
of gigabytes. The format ceiling is SQLite's (~281 TB); backup transfer
times matter long before that.

### What happens if my program crashes mid-write?

Committed data is untouched. A write becomes visible only when its
version pointer atomically advances; a crash before that leaves the
previous version intact, and the next `prune_content()` reclaims any
staged chunks. This holds for streaming writes too.

### Is Aloelite POSIX-complete?

The common paths — sequential and random-access reads/writes,
directories, rename/move, timestamps, symlinks, and permission bits
(chmod, executables) — work through FUSE today, including live SQLite
databases in rollback-journal mode and full git repositories (clone,
push, gc, hooks). Hard links, shared-writable mmap, and byte-range
locks across separate mounts are not there yet.

### Which platforms are supported?

The Python library, CLI, and WebUI (`aloelite-web`, direct mode) run
anywhere Python does. FUSE is Linux-only. The manager's FUSE-provisioning
mode targets Linux containers (Docker/Podman).

### What license does this use?

Apache 2.0.

Aloelite is intended as free and commercially friendly software released under permissive Apache 2 terms.