# aloelite

<div align="center">

<img src="https://raw.githubusercontent.com/Aloecraft-org/aloelite/refs/heads/main/doc/icon.png" style="height:96px; width:96px;"/>

**Aloelite SQLite Filesystem**

[![PyPI Version](https://img.shields.io/pypi/v/aloelite.svg)](https://pypi.org/project/aloelite/)
[![Python Versions](https://img.shields.io/pypi/pyversions/aloelite.svg)](https://pypi.org/project/aloelite/)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)

[![CI Status](https://github.com/Aloecraft-org/aloelite/actions/workflows/main.yml/badge.svg)](https://github.com/Aloecraft-org/aloelite/actions/workflows/main.yml)
[![Downloads](https://static.pepy.tech/badge/aloelite)](https://pepy.tech/project/aloelite)

</div>

## Overview

Aloelite is a filesystem implemented as a SQLite database. The entire filesystem, (i.e. files, directories, metadata, and content) lives in a single portable `.sqlite` file that can be copied, versioned, and opened anywhere SQLite runs.

It is designed for situations where you want filesystem semantics (paths, directories, streaming I/O) but need more control than a raw filesystem gives you: portable snapshots, at-rest encryption, content deduplication, and a clean programmatic API. A single file is easier to back up, replicate, and audit than a directory tree.

**What it provides:**

- A Python API for creating and navigating volumes, with full streaming read/write support validated against multi-gigabyte files
- At-rest encryption per volume (ChaCha20-Poly1305, Argon2id key derivation), with the PIN accepted only at mount time and never stored
- Content deduplication via a chunk pool (identical data stored once across all files in a volume)
- FUSE integration so any application can use an Aloelite volume as a plain directory, without modification
- A container-ready volume manager that exposes volumes over HTTP and propagates FUSE mounts to other containers via bind mount. i.e. suitable as a lightweight Docker/Podman volume provisioner
- Export and checkpoint endpoints that produce clean, self-contained SQLite snapshots while the volume remains mounted, enabling simple backup workflows without coordination

**What it is not:** a general-purpose network filesystem, a database replacement, or a POSIX-complete block device. Random-access rewrites on large files fall back to a buffered path. Node metadata (paths, timestamps, directory structure) is stored in plaintext even on encrypted volumes. (see [Security Notes](#security-notes))

## Abstract

This document specifies the design of a portable filesystem implemented on top of SQLite. The system models a filesystem as a small set of relational primitives (i.e. nodes, edges, volumes, and mounts) rather than as a fixed on-disk layout, deferring byte packing, page management, and durability to SQLite's mature storage engine. It is deliberately interface-agnostic: it presents a coherent internal model of files, directories, placement, and access without committing to any single external protocol, while remaining structurally amenable to exposing one (WebDAV, FUSE, or others) in the future. The design favors a hierarchical tree as its default arrangement but encodes that hierarchy as a relaxable constraint rather than a structural assumption, leaving a clear path toward a more general graph-shaped namespace. Supporting concerns (e.g. content storage, archival, and verifiable modification) are accommodated as first-class parts of the model even where their full implementation is staged for later.

## Discussion

The motivation for building on SQLite is portability and reach. A filesystem expressed as a SQLite database is a single, self-describing file that can be opened, moved, and inspected anywhere SQLite runs, which is nearly everywhere, and it inherits decades of work on storage layout and transactional integrity for free. The cost of that choice is that the filesystem's structure must be expressed relationally; the contribution of this design is a set of primitives that do so cleanly while keeping future capabilities reachable rather than precluded.

The model separates four concerns that filesystems often conflate. A *node* is an identity: a file (Entry) or a directory (Container), bearing a stable time-ordered identifier and its own name. An *edge* is a placement: a directed, immutable relationship that situates a node beneath a container within a particular volume. A *volume* is an origin: the root to which a coherent tree of placements ultimately refers. A *mount* is an access context: a live, volume-bound session, anchored at a node, through which operation on the filesystem is brokered. Holding these four apart is what gives the design its flexibility. Because a node's name and existence are independent of where it sits, the same node can in principle be reachable from more than one place, which is the seam through which links, mounts, and an eventual graph layout enter without disturbing the core. Because placement lives in immutable edges, every structural change is expressed as the creation of a new edge rather than the mutation of an existing one, which keeps the history of where things have been available and gives later features (e.g. ordering, verification, recovery) a stable substrate to build on. Because origins are modeled explicitly rather than inferred, the boundary of a volume is a real, referenceable thing rather than a convention. And because access is brokered through mounts rather than ambient, the system has a concrete answer to a question filesystems usually answer with the operating system: who holds a handle, who holds a lock, and what to reclaim when a session ends.

File contents are held apart from node metadata, so that traversing and resolving the namespace touches only small, frequently-accessed rows and never drags large payloads along. Reading and writing a whole file is an atomic operation in the ordinary case, with a streaming, descriptor-like access path for large or incremental I/O. That access path is mediated by mounts: because the filesystem has no native notion of a process, a mount stands in as the session identity that holds open handles and locks, and locks are scoped to the mount that acquired them, so that ending a session has a well-defined effect on everything it held. This advisory locking coexists with rather than commandeers SQLite's own transactional concurrency. Archival packs a subtree into a portable serialized form within the safety of a single transaction, so that the act of consolidating data cannot lose it. And the design reserves room for cryptographic verification of modification (e.g. a Merkle structure over the tree) by ensuring that mutations flow through a single, well-defined path where such bookkeeping can later be attached. None of these later-stage capabilities is fully realized in the first iteration; the purpose of the model described here is to make each of them an addition rather than a redesign.

**Implementation Status**

The core model (i.e. nodes, edges, volumes, and mounts) is fully realized, including path resolution, structural operations (create, move, rename, copy, remove, pack/unpack), advisory locking, and mount-scoped session management. File content is stored in a content-addressed chunk pool with deduplication, per-version manifests, configurable retention, and bounded-memory streaming I/O for both reads and writes; the streaming descriptor is production-validated against files in the tens of gigabytes. At-rest encryption is implemented at the storage boundary (ChaCha20-Poly1305, Argon2id key derivation, per-volume wrapped key), with convergent-nonce and random-nonce modes and a FUSE front-end that accepts a PIN at mount time. A container manager (`manager/`) exposes volumes as FUSE-mounted directories over a nine-endpoint HTTP API, suitable for use as a Docker/Podman volume provisioner. Reserved but not yet realized: cryptographic verification of the node tree (Merkle structure over content and placement), content-defined chunking, key rotation, graph-shaped namespaces beyond the default hierarchical tree, and node metadata encryption (currently plaintext in the SQLite schema. (see [Security Notes](#security-notes)))

---

## Getting Started

```
pip install aloelite
```

For FUSE support (Linux only):

```bash
sudo apt install fuse3 libfuse3-dev
pip install aloelite[fuse]
```

### Python API

```python
from aloelite.aloelite import Aloelite
from aloelite.types import WriteMode, Whence

with Aloelite("photos.sqlite") as fs:
    vol = fs.create_volume("photos")

    with fs.mount(vol.id) as m:
        m.create_container("/2024")
        m.set_metadata("/2024", {"year": "2024", "album": "trip"})
        m.create_entry("/2024/caption.txt", b"a sunset")

        with m.open_write("/note.txt") as w:
            w.write(b"hello ")
            w.write(b"world")

        print(m.read_all("/note.txt"))   # -> b"hello world"

        with m.open_read("/note.txt") as r:
            head = r.read(5)
            r.seek(-5, Whence.END)
            tail = r.read()

        m.rename("/note.txt", "readme.txt")
        m.move("/readme.txt", "/2024/readme.txt")
        m.copy("/2024", "/backup")
        m.remove_recursive("/backup")

    fs.prune()
    print(fs.health_check())   # -> [] when consistent
```

### Encryption

```python
PIN = b"correct-horse-battery-staple"

with Aloelite("vault.sqlite") as fs:
    vol = fs.create_volume("vault", pin=PIN)   # Argon2id key derivation, ChaCha20-Poly1305

    with fs.mount(vol.id, pin=PIN) as m:
        m.create_entry("/secret.txt", b"eyes only")
        print(m.read_all("/secret.txt"))   # -> b"eyes only"

    # Wrong PIN is rejected at mount time (not at read time)
    from aloelite import errors
    try:
        fs.mount(vol.id, pin=b"wrong")
    except errors.BadKey:
        print("wrong PIN rejected ✓")
```

Encryption is invisible at the `Mount` API level. Use `enc_mode="random"` to trade chunk deduplication for zero equality leakage.

### Pathlib-style interface (`AloelitePath`)

The easiest way to work with files inside a volume. No FUSE required. Any `Mount` doubles as a path root:

```python
from aloelite.aloelite import Aloelite

with Aloelite("photos.sqlite") as fs:
    vol = fs.create_volume("photos")

    with fs.mount(vol.id) as m:
        docs = m / "docs"                        # Mount / str -> AloelitePath
        docs.mkdir(parents=True, exist_ok=True)

        note = docs / "note.txt"
        note.write_text("hello world")
        print(note.read_text())                  # -> "hello world"

        with (docs / "big.bin").open("wb") as w: # bounded-memory streaming
            w.write(b"chunk " * 100_000)

        for child in docs.iterdir():
            print(child, child.stat().size)

        for txt in m.path("/").rglob("*.txt"):   # '*' and '**' globbing
            print(txt)

        note.set_metadata({"author": "mg"})      # NODE-6 metadata
        note.copy("/docs/note.bak")
        note.rename("/docs/renamed.txt")         # full move, returns new path
```

`AloelitePath` is pure sugar over the `Mount` API — it adds nothing to the contract, so everything above is atomic, deduplicated, and encryption-transparent exactly like the underlying operations.

---

## FUSE

Mount an Aloelite volume as a regular directory (Linux, requires `fuse3`):

```bash
# Plain volume
aloelite-fuse photos.sqlite photos /mnt/photos

# Encrypted volume — three ways to supply the PIN
aloelite-fuse vault.sqlite vault /mnt/vault --pin "my secret"
aloelite-fuse vault.sqlite vault /mnt/vault --pin-file ~/.vaultpin
aloelite-fuse vault.sqlite vault /mnt/vault --pin-env VAULT_PIN

# Unmount
fusermount3 -u /mnt/photos
```

The FUSE driver uses bounded-memory streaming I/O for both reads and writes — a 15 GB copy does not buffer in RAM. Sequential writes flush one chunk at a time; non-sequential access on large files falls back to a buffered path.

---

## Volume Manager

The volume manager is a privileged container that manages multiple Aloelite volumes and exposes each as a FUSE-mounted subdirectory, accessible to other containers via bind mount.

### Run

```bash
# Host directories (once)
sudo mkdir -p /aloelite-root /mnt/aloelite

docker run -d --privileged \
  -v /aloelite-root:/aloelite-root \
  -v /mnt/aloelite:/mnt:rshared \
  --device /dev/fuse \
  -p 8080:8080 \
  aloecraft/aloelite-manager
```

`/aloelite-root` holds the backing SQLite files and persists across restarts. `/mnt/aloelite` is the host-visible mount root; FUSE mounts inside the container propagate here via `rshared`. `--privileged` (or at minimum `CAP_SYS_ADMIN`) is required.

### API

| Method | Path | Description |
|---|---|---|
| `POST` | `/volumes` | Create a volume |
| `GET` | `/volumes` | List all volumes |
| `DELETE` | `/volumes/<id>` | Delete a volume (unmounts first) |
| `POST` | `/volumes/<id>/mount` | Mount a volume |
| `DELETE` | `/volumes/<id>/mount` | Unmount a volume |
| `GET` | `/volumes/<id>/mount` | Mount status |
| `GET` | `/volumes/<id>/stat` | Backing file metadata (size, mtime) |
| `GET` | `/volumes/<id>/export` | Checkpoint + stream the SQLite file |
| `POST` | `/volumes/<id>/checkpoint` | Run `WAL_CHECKPOINT(TRUNCATE)` |
| `GET` | `/volumes/<id>/files?path=/` | List a directory in a mounted volume |
| `GET` | `/volumes/<id>/files/download?path=/f` | Download a file |
| `POST` | `/volumes/<id>/files/upload?path=/dir` | Upload a file (multipart field `file`) |
| `POST` | `/volumes/<id>/files/mkdir?path=/dir` | Create a directory |
| `DELETE` | `/volumes/<id>/files?path=/f` | Delete a file or directory (recursive) |
| `GET` | `/admin` | Admin panel: volumes + per-volume file explorer |

```bash
# Create and mount
curl -s -X POST http://localhost:8080/volumes \
  -H 'Content-Type: application/json' \
  -d '{"name": "myphotos"}' | tee /tmp/vol.json

VID=$(jq -r .id /tmp/vol.json)
curl -s -X POST http://localhost:8080/volumes/$VID/mount \
  -H 'Content-Type: application/json' -d '{}'

# The volume is now a plain directory on the host
ls /mnt/aloelite/$VID

# Consume from another container
docker run --rm -v /mnt/aloelite/$VID:/data alpine ls /data

# Backup: poll stat, export on change
curl -s http://localhost:8080/volumes/$VID/stat | jq
curl -s http://localhost:8080/volumes/$VID/export -o snapshot.sqlite

# Encrypted volume
curl -s -X POST http://localhost:8080/volumes \
  -H 'Content-Type: application/json' \
  -d '{"name": "vault", "encrypted": true, "pin": "correct-horse"}'
# Mount with: -d '{"pin": "correct-horse"}'
```

The export endpoint runs `WAL_CHECKPOINT(TRUNCATE)` before streaming, producing a complete self-contained SQLite file with no accompanying WAL. The volume does not need to be unmounted to export — SQLite's read consistency guarantees a coherent snapshot regardless of active writes.

The admin panel at `/admin` includes a per-volume **file explorer** for any mounted volume: browse with breadcrumbs, upload, download, create folders, and delete. The file endpoints operate through the live FUSE mountpoint, so they work identically for plain and encrypted volumes (the mount session already holds the key).

### Backup sync pattern

```
loop:
    poll GET /volumes/<id>/stat
    if mtime > last_known_mtime:
        GET /volumes/<id>/export  →  write to temp file  →  rename into place
        last_known_mtime = mtime
    sleep(interval)
```

The rename into place is atomic; a failed export leaves the previous replica intact.

---

## Security Notes

**Chunk data** is encrypted at the storage boundary (ChaCha20-Poly1305, Argon2id key derivation). The SQLite file is opaque without the PIN.

**Node metadata** (paths, timestamps, node IDs, directory structure) is stored in plaintext in the SQLite schema. An observer with access to the file can read the filesystem tree even without the PIN. For sensitive deployments, place the backing file on an encrypted volume (LUKS, encrypted home directory, etc.) or use the `pack` primitive to seal a subtree before transport.

The volume manager API is intended for trusted networks. PINs are transmitted in request bodies and never logged or persisted; the derived key is held only for the duration of the mount session.

---

## License

Apache 2.0. See [LICENSE](LICENSE).