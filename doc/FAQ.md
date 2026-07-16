# FAQ

<div align="center">

<img src="aloelite.png" style="height:96px; width:96px;"/>

**Aloelite SQLite Filesystem**
</div>

### Quick Links 

- [Overview](/README.md)
- [Getting Started](/doc/GETTING_STARTED.md)
- **Frequently Asked Questions (This Document)**
- [Troubleshooting](/doc/TROUBLESHOOTING.md)
- [Requirements Spec](/doc/REQUIREMENTS.md)
- [Encryption Spec](/doc/ENCRYPTION.md)

(see `doc/` for more)

## Frequently Asked Questions

### Why Aloelite?

- **One file** — the whole filesystem copies, ships, and backs up as a
  single artifact
- **Encrypted at rest** — per-volume ChaCha20-Poly1305; the PIN is never
  stored
- **Deduplicated** — identical content is stored once, including across
  repeated backups
- **Atomic and versioned** — writes commit fully or not at all; history
  is kept until you prune
- **Use it your way** — Python API, CLI, FUSE directory, or
  container-volume manager over one format
- **Runs anywhere SQLite does** — which is nearly everywhere

### What is Aloelite, in one sentence?

Aloelite is a filesystem (i.e. files, folders, metadata, and content) stored in one portable artifact as a single SQLite file.

### Why would I use this instead of a plain directory?

When "the filesystem" needs to be a *thing you can hold*: one file to
copy, version, encrypt, ship, or hand to a container. You also get
content deduplication, atomic writes with version history, and at-rest
encryption without setting up disk encryption.

### Why SQLite?

Portability and inherited maturity. A SQLite file opens anywhere SQLite
runs (which is nearly everywhere) and decades of work on storage
layout, crash safety, and transactional integrity come for free. Aloelite
supplies the filesystem model on top; SQLite supplies the durability.

### Is it a database? Can I query my files with SQL?

It's a filesystem that *uses* a database. You *can* open the file with
any SQLite tool and inspect the schema. That's a feature for auditing,
but the supported interface is the Mount API (Python/CLI/FUSE/HTTP).
Writing to the tables directly bypasses the invariants the API enforces.

### What's the difference between a volume and a mount?

A **volume** is a filesystem tree with a root and everything under it. A
**mount** is a durable access point into a volume, anchored at a
specific node. All access goes through a mount, never directly at a
volume. One file can hold many volumes; one volume can have many mounts.

### Why do mounts stick around after I close my program?

By design. A mount is a durable record, not a session. It can carry a
TTL, be listed later (`fs.list_mounts()`, `aloelite ... mounts`), and be
re-attached to. Retiring one (`unmount`) is permanent; you open a new
mount rather than reviving an old one. Retired mounts are hidden from
listings by default.

### How does encryption work? What's actually protected?

Chunk *content* is encrypted (ChaCha20-Poly1305; the PIN is stretched
with Argon2id and never stored — a wrong PIN is rejected by the
cryptography itself, not a comparison). Node *metadata* — names, paths,
timestamps, tree structure — is **plaintext**. Someone with the file but
no PIN can see your directory tree and file sizes, but not contents. If
the tree shape itself is sensitive, keep the file on an encrypted disk,
or `pack` a subtree before transport.

### Can I recover a forgotten PIN?

No. There is no back door, recovery key, or reset. The volume key is
sealed under your PIN and nothing else.

### Two identical files — stored twice?

Once. Content is chunked and stored by address in a shared pool, so
identical data (whole files or common prefixes at chunk granularity)
is stored a single time — including across repeated backups of
similar files. Encrypted volumes keep dedup by default ("convergent"
mode); if you'd rather no observer can tell two files are equal, create
the volume with `enc_mode="random"` and trade dedup away.

### Does deleting a file shrink the .fs file?

Not immediately — and this is deliberate. Removal detaches; old versions
and detached subtrees are kept until you prune (`fs.prune()`,
`fs.prune_content()`), which is also what makes accidental deletion
recoverable in the meantime. SQLite additionally recycles freed pages
internally rather than returning them to the OS; a `VACUUM` compacts the
file itself when you want the bytes back.

### Is it safe to copy the .fs file while it's in use?

Copy the file directly and you may catch it mid-write. Use the export
mechanisms instead: the manager's `/volumes/<id>/export` endpoint
checkpoints and streams a consistent snapshot *while mounted*, or run
`PRAGMA wal_checkpoint(TRUNCATE)` and copy when quiescent.

### Can two processes use the same file at once?

Yes, within SQLite's rules: WAL mode lets readers coexist with one
writer, and writers queue briefly rather than fail. The advisory lock
layer (per-mount exclusive write locks on entries) coordinates on top of
that. Many *processes* on one machine is fine; a network share
underneath the .fs file is not (that's a SQLite limitation).

### Can I run a database (like SQLite itself) inside a FUSE-mounted volume?

Not yet reliably — a live database needs byte-range locking and shared
memory the FUSE layer doesn't provide yet. It's an explicit roadmap goal.
Ordinary applications (mail servers, file syncing, editors, build trees)
work today. For *backing up* other services' SQLite files, snapshot them
first (e.g. `VACUUM INTO`) and store the snapshot.

### How big can files and volumes get?

Streaming I/O is bounded-memory and validated against files in the tens
of gigabytes; a copy never buffers the whole file. The ceiling is
SQLite's (theoretical ~281 TB per file) — in practice you'll care about
backup transfer times long before format limits.

### What happens if my program crashes mid-write?

Committed data is untouched. A write becomes visible only when its
version pointer is atomically advanced; a crash before that leaves the
previous version intact and some orphaned staged chunks, which the next
`prune_content()` reclaims. This holds for streaming writes too.

### Is Aloelite POSIX-complete?

No, and it doesn't claim to be. The common paths — sequential and
random-access reads/writes, directories, rename/move, timestamps — work
through FUSE today. Symlinks, hard links, and permissions modeling are
not there yet (transparency is the stated direction of travel; see the
roadmap).

### Which platforms?

The Python library and CLI run anywhere Python does. FUSE is Linux-only.
The manager targets Linux containers (Docker/Podman). Rust, JS/WASM, and
Kotlin implementations of the same file format are in development, with
the Python implementation as the reference.

### What license?

Apache 2.0.