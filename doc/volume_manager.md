# Aloelite Volume Manager — Consolidated Specification

## Purpose

The Aloelite Volume Manager is a privileged container that manages multiple Aloelite
volumes and exposes each as a FUSE-mounted subdirectory accessible to other containers
or pods. It acts as a lightweight volume provisioner, mount orchestrator, and future
replication engine, all running inside a single Python process.

This specification supersedes `manager.md` and `manager_draft2.md`.

---

## Source Dependencies

The manager is built on exactly two files from the Aloelite project:

- **`aloelite.py`** — volume lifecycle, mount sessions, all filesystem operations,
  streaming descriptors, encryption
- **`fuse.py`** — complete FUSE implementation over a single Aloelite mount

No other Aloelite source files are required.

---

## Namespace Definitions

Three distinct namespaces are in play. The spec uses these terms consistently:

| Term | Meaning |
|---|---|
| **Manager-internal path** | A path inside the manager container's filesystem |
| **Host path** | A path on the container host, visible to the Docker/Podman daemon |
| **Consumer path** | A path inside a consumer container, chosen by the consumer |

Key mappings at runtime:

```
Host: /mnt/aloelite/<volume-id>        ← propagated from manager via rshared
Manager-internal: /mnt/<volume-id>     ← FUSE mountpoint inside the manager
Backing store: /aloelite-root/<id>.sqlite  ← persistent SQLite file (named volume)
Consumer: /data  (or anything)         ← bind-mounted from host path above
```

---

## Container Requirements

The manager container must be started with:

```bash
docker run --privileged \
  -v /aloelite-root:/aloelite-root \
  -v /mnt/aloelite:/mnt:rshared \
  --device /dev/fuse \
  aloelite-manager
```

- `/aloelite-root` is a named or host volume holding the backing SQLite files.
  It persists across container restarts.
- `/mnt/aloelite:/mnt:rshared` bridges the manager's mount namespace to the host.
  FUSE mounts created inside the container under `/mnt/<id>` propagate to the host
  at `/mnt/aloelite/<id>`, from where consumers bind-mount them.
- `/dev/fuse` grants the container permission to perform FUSE mounts.
- `--privileged` (or at minimum `CAP_SYS_ADMIN`) is required for FUSE and mount
  namespace operations.

A consumer container mounts a volume as:

```bash
docker run \
  -v /mnt/aloelite/<volume-id>:/data \
  my-consumer
```

The consumer sees a plain directory. It has no knowledge of FUSE or Aloelite.

---

## Architecture

### Single Process, Multiple Volumes

The manager runs as one Python process containing:

- **API server** (Flask or FastAPI) in the main thread
- **Mount supervisor** in a dedicated thread
- **One thread per active FUSE mount**, each running its own `trio.run()` loop

`pyfuse3` with Trio is used for FUSE. Each mount thread is isolated — it owns its
event loop and its FUSE session. Inter-mount coordination is limited to the mount
supervisor, which uses thread-safe primitives (locks, events) to track state.

This satisfies the requirement of no new process per volume.

### Storage Concerns

Two independent storage concerns:

1. **Backing SQLite files** — Aloelite `.sqlite` files stored under
   `/aloelite-root/`. Persistent. Managed by `aloelite.py`.
2. **Volume metadata** — managed by a `VolumeStore` abstraction (see below).
   Not stored in an Aloelite volume.

---

## VolumeStore Abstraction

All volume metadata is accessed exclusively through a `VolumeStore` interface.
No other component reads or writes metadata directly.

```python
class VolumeStore:
    def get(self, volume_id: str) -> VolumeRecord | None: ...
    def put(self, record: VolumeRecord) -> None: ...
    def delete(self, volume_id: str) -> None: ...
    def list(self) -> list[VolumeRecord]: ...
```

```python
@dataclass
class VolumeRecord:
    id: str
    name: str
    sqlite_path: str       # manager-internal path to backing file
    encrypted: bool
    created_at: float
    mounted: bool
    mountpoint: str | None  # manager-internal path, e.g. /mnt/<id>
```

### Initial Implementation: JSON

The initial `VolumeStore` implementation writes to a single JSON file
(`/aloelite-root/volumes.json`). Since only one manager process touches this
file and writes are serialized through the store interface, this is safe for
the current scope.

The abstraction exists so the backing store can be replaced with SQLite later
without changing any other component.

---

## API

Nine endpoints. All request/response bodies are JSON.

### `POST /volumes`
Create a new volume. Does not mount it.

Request:
```json
{ "name": "myphotos", "encrypted": true, "pin": "secret" }
```
`pin` is required if `encrypted` is true, omitted otherwise.

Response `201`:
```json
{ "id": "...", "name": "myphotos", "encrypted": true, "mounted": false }
```

### `DELETE /volumes/<id>`
Delete a volume. If currently mounted, unmounts it first, then removes the
backing SQLite file and the volume record.

Response `204` on success.

### `GET /volumes`
List all volumes with current mount status.

Response `200`:
```json
[
  { "id": "...", "name": "myphotos", "encrypted": true, "mounted": true,
    "mountpoint": "/mnt/abc123" }
]
```

### `POST /volumes/<id>/mount`
Mount an existing volume. Returns when the mount is confirmed ready.

Request:
```json
{ "pin": "secret" }
```
`pin` required only for encrypted volumes.

Response `200`:
```json
{ "id": "...", "mountpoint": "/mnt/abc123", "host_path": "/mnt/aloelite/abc123" }
```

`host_path` is the path consumers use for bind mounts.

Response `409` if already mounted. Response `503` if mount readiness check times out.

### `DELETE /volumes/<id>/mount`
Unmount a volume. Does not delete the volume or its backing file.

Response `204` on success. Response `404` if not currently mounted.

### `GET /volumes/<id>/mount`
Get current mount status for a single volume.

Response `200`:
```json
{ "id": "...", "mounted": true, "mountpoint": "/mnt/abc123", "ready": true }
```

### `GET /volumes/<id>/stat`
Returns lightweight metadata about the backing SQLite file. Intended as a
cheap poll target for backup clients to detect whether the volume has changed
since their last export.

The volume does not need to be mounted.

Response `200`:
```json
{
  "id": "...",
  "name": "myphotos",
  "size_bytes": 4194304,
  "mtime": 1719580412.337,
  "mounted": true
}
```

`mtime` is `os.stat(sqlite_path).st_mtime` on the backing file. For mounted
volumes, writes flow through FUSE and are committed to SQLite, so `mtime`
advances after any write activity. A backup client can poll this endpoint and
skip the export if `mtime` has not changed since the last snapshot.

### `GET /volumes/<id>/export`
Produces a clean, consistent snapshot of the backing SQLite file and streams
it to the caller as `application/octet-stream`.

The volume does not need to be mounted. If it is mounted, active writes
are not paused — SQLite's read consistency guarantees a coherent snapshot
regardless.

Procedure:
1. Run `PRAGMA wal_checkpoint(TRUNCATE)` on the backing file to collapse
   the WAL into the main database file and reset the WAL to zero bytes.
2. Stream the main `.sqlite` file to the response.

The caller receives a complete, self-contained SQLite database with no
accompanying WAL. It can be used directly as a replica or stored as a backup.
The caller is responsible for replacing its local copy atomically (write to a
temp file, rename into place).

Response: binary stream, `Content-Type: application/octet-stream`,
`Content-Length` set to file size after checkpoint.

### `POST /volumes/<id>/checkpoint`
Explicitly triggers `PRAGMA wal_checkpoint(TRUNCATE)` on the backing file
without streaming an export. Useful for tooling, testing, and scheduled
maintenance. The volume does not need to be mounted.

Response `200`:
```json
{ "id": "...", "wal_frames_checkpointed": 42, "wal_frames_remaining": 0 }
```

`wal_frames_remaining` should be `0` after a successful `TRUNCATE` checkpoint.
A non-zero value indicates active readers prevented full truncation; this is
not an error but should be logged.

---

## Mount Lifecycle

### Mounting

1. Validate the volume exists and is not already mounted.
2. Create the mountpoint directory at `/mnt/<id>` if it does not exist.
3. Spawn a new thread; the thread calls `trio.run(fuse_main, sqlite_path, volume_name, mountpoint, pin)`.
4. The API handler polls for mount readiness (see below).
5. On confirmed readiness, update `VolumeStore` (`mounted=True`, `mountpoint=...`).
6. Return the mount response including `host_path`.

### Mount Readiness Check

After spawning the FUSE thread, the API handler polls in a loop:

```python
import os, time

def wait_for_mount(mountpoint, timeout=2.0, interval=0.1):
    parent_dev = os.stat(os.path.dirname(mountpoint)).st_dev
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if os.stat(mountpoint).st_dev != parent_dev:
                return True  # FUSE is mounted; device ID differs from parent
        except OSError:
            pass
        time.sleep(interval)
    return False
```

If this returns `False`, the FUSE thread is signalled to stop, the mountpoint
is cleaned up, and the API returns `503`.

### Unmounting

1. Signal the FUSE thread to stop (via a thread event or Trio cancellation scope).
2. Call `fusermount3 -uz <mountpoint>` (lazy unmount, safe if consumers still have
   it open).
3. Join the thread with a timeout (5 seconds). If it does not exit, log and continue.
4. Run `PRAGMA wal_checkpoint(TRUNCATE)` on the backing SQLite file. This collapses
   any pending WAL frames into the main file, resets WAL size to zero, and leaves
   the backing file in the cleanest possible state for export or backup. Log
   `wal_frames_remaining` if non-zero.
5. Remove the mountpoint directory.
6. Update `VolumeStore` (`mounted=False`, `mountpoint=None`).

---

## Preflight Checks

Run at startup before the API server begins accepting requests. Any failure is
fatal — log the reason and exit.

| Check | How | Fatal? |
|---|---|---|
| `/dev/fuse` present | `os.path.exists("/dev/fuse")` | Yes |
| `CAP_SYS_ADMIN` available | Read `/proc/self/status`, check `CapEff` | Yes |
| `/aloelite-root` exists and is writable | `os.access("/aloelite-root", os.W_OK)` | Yes |
| `/mnt` has `rshared` propagation | Parse `/proc/self/mountinfo` for `shared:` peer group on `/mnt` | Yes |
| `fusermount3` binary present | `shutil.which("fusermount3")` | Yes |
| `allow_other` permitted | Check `/etc/fuse.conf` for `user_allow_other`, or attempt a test mount with `allow_other` | Yes |
| `VolumeStore` readable/writable | Attempt to read (or create) `volumes.json` | Yes |
| Stale mountpoints from prior run | For each `mounted=True` record in store, check if mount is actually active; if not, clear `mounted` flag and attempt `fusermount3 -uz` defensively | Warning, not fatal |

### `allow_other` Detail

Without `allow_other`, FUSE mounts owned by the manager process are not readable
by other UIDs — including consumer containers. This is the single most common
silent failure in this architecture. The preflight check must confirm it is
enabled before any mount is attempted.

### Stale Mountpoints on Restart

If the manager crashed, previously mounted volumes will have `mounted=True` in
the store but no active FUSE session. On startup, the manager attempts
`fusermount3 -uz` on each such mountpoint (to clear any kernel-side stale state),
then sets `mounted=False` in the store. Volumes must be explicitly remounted via
the API after a restart.

---

## PIN Handling

For encrypted volumes, the PIN is supplied in the POST body of
`POST /volumes` (at creation) and `POST /volumes/<id>/mount` (at mount time).

Constraints:
- The PIN is passed directly to `aloelite.py` for key derivation. It is held in
  memory only for the duration of that call.
- The manager never logs request bodies.
- The PIN is never written to the `VolumeStore` or any file.
- After key derivation, the Aloelite mount session holds the derived key internally.
  The manager holds no key material after the mount call returns.

Assumption: the manager API is on a trusted network with SSL termination. No
additional PIN transport security is required in this scope.

---

## Shutdown Sequence

Registered as handlers for both `SIGTERM` and `SIGINT`.

1. Stop accepting new API requests (close the listening socket or set a flag).
2. For each active mount, in any order:
   a. Signal the FUSE thread to stop.
   b. Call `fusermount3 -uz <mountpoint>`.
   c. Join the thread with a 5-second timeout.
3. Update `VolumeStore`: set all `mounted=False`.
4. Close the `VolumeStore`.
5. Exit 0.

If any FUSE thread fails to join within the timeout, log the mountpoint and
continue shutdown. Do not block indefinitely.

---

## Backup Synchronisation

The export + stat endpoints provide a simple, connection-tolerant backup sync
pattern that requires no persistent connection and no server-side state beyond
what already exists.

### Client Sync Loop

```
loop:
    poll GET /volumes/<id>/stat
    if mtime > last_known_mtime:
        GET /volumes/<id>/export  → write to temp file → rename into place
        last_known_mtime = mtime
    sleep(interval)
```

This works correctly on shaky connections: if the export request is interrupted,
the temp file is discarded and the client retries on the next poll. The rename
into place is atomic, so the local replica is never left in a partial state.
The client only fetches a new export when the backing file has actually changed.

### WAL Checkpoint Guarantee

`GET /volumes/<id>/export` always runs `PRAGMA wal_checkpoint(TRUNCATE)` before
streaming. This means:

- The exported file has no accompanying WAL — it is a complete, self-contained
  SQLite database.
- WAL does not accumulate unboundedly on the server. Each export (and each
  unmount) resets it.
- A backup client that polls and exports regularly provides implicit WAL
  maintenance as a side effect.

If no export or unmount has occurred for a long time and a mounted volume has
seen heavy write activity, WAL can grow. A scheduled `POST /volumes/<id>/checkpoint`
call (e.g. nightly) handles this case.

### What This Does Not Provide

- Incremental replication (each export is a full snapshot)
- Change notification (clients must poll)
- Point-in-time recovery within an export interval

These are explicitly deferred to a future WAL streaming implementation.

---

## Replication (Future)

The manager container is the natural place for incremental WAL replication, since
it is the only process with direct access to the backing SQLite files. A future
addition would:

- Watch SQLite WAL files per volume for new frames
- Batch frames into numbered, encrypted replication segments
- Upload segments + periodic snapshots to offsite storage
- Restore by replaying segments onto a snapshot baseline

Replication always operates on encrypted Aloelite data. Offsite storage need not
be fully trusted.

The export + checkpoint infrastructure added in this iteration provides the
snapshot baseline that a future WAL streaming implementation would build on.
No streaming replication logic should be included in the initial implementation.

---

## File Layout

```
aloelite-py/
  aloelite/
    __init__.py
    aloelite.py         # AloeLite and Mount classes; add checkpoint() here
    crypto.py
    db.py
    descriptor.py
    errors.py
    fuse.py             # FUSE implementation; modified for programmatic multi-mount
    models.py
    operations.py
    resolve.py
    types.py

  manager/
    __init__.py
    api.py              # Flask/FastAPI endpoints (nine routes)
    supervisor.py       # Mount supervisor thread, FUSE thread lifecycle
    store.py            # VolumeStore abstraction + JSON implementation
    preflight.py        # All preflight checks

  config/
    sql-templates.yaml

  sql/
    schema.sql

  tests/
    test_operations.py
    test_encryption.py
    manager/            # future

  pyproject.toml        # aloelite-fuse = "aloelite.fuse:main" entry point
  Dockerfile
  README.md
  .gitignore
```

The manager is a sibling package to `aloelite`, not nested inside it. `fuse.py`
is part of the `aloelite` package and imported programmatically by the manager's
supervisor; it is also exposed as the `aloelite-fuse` CLI entry point via
`pyproject.toml`.

Run during development without installing:
```bash
python3 -m aloelite.fuse photos.sqlite photos /mnt/photos
```

Or install in editable mode and use the entry point:
```bash
pip install -e .
aloelite-fuse photos.sqlite photos /mnt/photos
```

---

## What This Specification Defines

- What the manager is and what it is not
- The three namespaces in play and how they relate
- Container startup requirements (volumes, devices, propagation)
- The `VolumeStore` abstraction and its initial JSON implementation
- The complete nine-endpoint API
- The mount threading model (thread-per-mount, Trio per thread)
- Mount readiness signalling
- WAL checkpoint behaviour (on unmount, on export, on demand)
- Backup synchronisation pattern (stat poll + full export, connection-tolerant)
- PIN handling constraints
- The complete preflight checklist including `allow_other` and stale mount recovery
- The shutdown sequence
- The path toward future WAL streaming replication

This document is sufficient to drive implementation from a fresh context.