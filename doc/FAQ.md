# FAQ

<div align="center">

<img src="aloelite.png" style="height:96px; width:96px;"/>

**Aloelite SQLite Filesystem**

[Overview](/README.md) | [Getting Started](/doc/GETTING_STARTED.md) |  **Frequently Asked Questions (This Document)** 

[Troubleshooting](/doc/TROUBLESHOOTING.md) | [Requirements Spec](/doc/REQUIREMENTS.md) | [Encryption Spec](/doc/ENCRYPTION.md)
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
- **Use it your way**: Python API, CLI, FUSE directory, or
  container-volume manager over one format
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

Not yet reliably — a live database needs byte-range locking and shared
memory the FUSE layer doesn't provide. Ordinary applications (mail
servers, syncing, editors, build trees) work today. To back up another
service's SQLite file, snapshot it first (`VACUUM INTO`) and store that.

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
directories, rename/move, timestamps — work through FUSE today.
Symlinks, hard links, and permissions modeling are not there yet.

### Which platforms are supported?

The Python library and CLI run anywhere Python does. FUSE is Linux-only.
The manager targets Linux containers (Docker/Podman).

### What license does this use?

Apache 2.0.

Aloelite is intended as free and commercially friendly software released under permissive Apache 2 terms.