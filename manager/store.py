# ./manager/store.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
manager.store — volume metadata persistence.

All volume metadata is accessed exclusively through the VolumeStore interface;
no other component reads or writes metadata directly. The initial backend is a
single JSON file (volumes.json). Only one manager process touches it and all
writes are serialized through this interface, so a process-wide lock plus atomic
file replacement is sufficient for the current scope.

The abstraction exists so the backing store can be swapped for SQLite later
without touching any other component.

Layout of the JSON file (schema v2):

    {
      "version": 2,
      "filesystems": { "<fs_id>": { ...FilesystemRecord fields... }, ... },
      "volumes": { "<id>": { ...VolumeRecord fields... }, ... },
      "pending_unmounts": [ "<mountpoint>", ... ]
    }

A FilesystemRecord is one backing .sqlite file; a VolumeRecord references its
parent via fs_id and no longer carries a path of its own. The file's on-disk
name never changes after creation (<fs_id>.sqlite) — display_name is the
UI-settable, export-time name, so a rename is a store update with no
rename-while-open hazard.

MIGRATION (v1 -> v2, at load): every v1 volume carried its own sqlite_path
(1:1 file:volume). Each such record gets a synthesized FilesystemRecord
wrapping that path, display_name taken from the volume name, and the volume
is rewritten with fs_id. Distinct v1 volumes sharing a path (never produced
by the manager, but tolerated) collapse onto one FilesystemRecord. The
migrated state is flushed immediately so the file on disk becomes v2.

`pending_unmounts` is a side list of mountpoints whose FUSE thread failed to
join cleanly on shutdown/unmount. Preflight drains it on the next start by
attempting a defensive `fusermount3 -uz` on each. It is kept out of
VolumeRecord deliberately: a pending unmount is not tied to a live volume row
(the volume may already be deleted) and the record schema stays exactly as the
spec defines it.
"""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
import threading
import uuid
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

_SCHEMA_VERSION = 2


@dataclass
class FilesystemRecord:
    """One backing .sqlite file. The unit of portability (import/export)."""

    id: str
    display_name: str  # UI-settable; used as the export filename
    sqlite_path: str  # manager-internal path; fixed for the record's life
    created_at: float

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "FilesystemRecord":
        fields = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in fields})


@dataclass
class VolumeRecord:
    """One row of volume metadata. Belongs to a FilesystemRecord via fs_id."""

    id: str
    name: str
    fs_id: str  # parent FilesystemRecord
    encrypted: bool
    created_at: float
    mounted: bool
    mountpoint: str | None  # manager-internal path, e.g. /mnt/<id>
    # which frontend currently serves the volume: "fuse" (FUSE mountpoint),
    # "direct" (held Mount session, browser/API access only), or None.
    frontend: str | None = None
    # auto-mount at startup (opt-in). The PIN is never stored; it is re-read
    # from the named env var or file at each auto-mount.
    auto_mount: bool = False
    mount_name: str | None = None
    pin_env: str | None = None
    pin_file: str | None = None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "VolumeRecord":
        # Tolerate unknown keys defensively (forward-compat with later fields).
        fields = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in fields})


@runtime_checkable
class VolumeStore(Protocol):
    """The metadata interface. Implementations must be safe for concurrent
    access from the API thread, the supervisor thread, and mount threads."""

    def get(self, volume_id: str) -> VolumeRecord | None: ...
    def put(self, record: VolumeRecord) -> None: ...
    def delete(self, volume_id: str) -> None: ...
    def list(self) -> list[VolumeRecord]: ...

    # filesystem (backing file) records
    def get_fs(self, fs_id: str) -> FilesystemRecord | None: ...
    def put_fs(self, record: FilesystemRecord) -> None: ...
    def delete_fs(self, fs_id: str) -> None: ...
    def list_fs(self) -> list[FilesystemRecord]: ...
    def volumes_of(self, fs_id: str) -> list[VolumeRecord]: ...
    def sqlite_path_of(self, record: VolumeRecord) -> str: ...

    # pending-unmount bookkeeping (recovery aid; see module docstring)
    def add_pending_unmount(self, mountpoint: str) -> None: ...
    def list_pending_unmounts(self) -> list[str]: ...
    def clear_pending_unmount(self, mountpoint: str) -> None: ...

    def close(self) -> None: ...


class JsonVolumeStore:
    """JSON-file-backed VolumeStore.

    The full state is held in memory and rewritten atomically (temp file +
    os.replace) on every mutation, so a crash mid-write can never leave a
    partial file: the reader either sees the old complete file or the new
    complete one. A re-entrant lock serializes all access.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._filesystems: dict[str, FilesystemRecord] = {}
        self._volumes: dict[str, VolumeRecord] = {}
        self._pending: list[str] = []
        self._load()

    # -- persistence --------------------------------------------------------
    def _load(self) -> None:
        with self._lock:
            if not os.path.exists(self._path):
                # First run: materialize an empty store so later reads/writes
                # (and the preflight "store writable" check) have a file.
                self._flush_locked()
                return
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._pending = list(data.get("pending_unmounts", []))
            self._filesystems = {
                fid: FilesystemRecord.from_dict(rec)
                for fid, rec in data.get("filesystems", {}).items()
            }
            if data.get("version", 1) < 2:
                self._migrate_v1_locked(data.get("volumes", {}))
                self._flush_locked()  # persist as v2 immediately
            else:
                self._volumes = {
                    vid: VolumeRecord.from_dict(rec)
                    for vid, rec in data.get("volumes", {}).items()
                }

    def _migrate_v1_locked(self, raw_volumes: dict) -> None:
        """v1 -> v2: synthesize a FilesystemRecord per distinct sqlite_path and
        re-key each volume onto it. Caller holds the lock."""
        by_path: dict[str, str] = {}  # sqlite_path -> fs_id
        for vid, rec in raw_volumes.items():
            path = rec.get("sqlite_path")
            fs_id = by_path.get(path)
            if fs_id is None:
                fs_id = uuid.uuid4().hex
                by_path[path] = fs_id
                self._filesystems[fs_id] = FilesystemRecord(
                    id=fs_id,
                    display_name=rec.get("name") or fs_id,
                    sqlite_path=path,
                    created_at=rec.get("created_at", 0.0),
                )
            rec = dict(rec)
            rec["fs_id"] = fs_id
            self._volumes[vid] = VolumeRecord.from_dict(rec)

    def _flush_locked(self) -> None:
        """Atomically write current state. Caller must hold the lock."""
        payload = {
            "version": _SCHEMA_VERSION,
            "filesystems": {
                fid: rec.to_dict() for fid, rec in self._filesystems.items()
            },
            "volumes": {vid: rec.to_dict() for vid, rec in self._volumes.items()},
            "pending_unmounts": list(self._pending),
        }
        directory = os.path.dirname(self._path) or "."
        # Same-directory temp file guarantees os.replace is an atomic rename
        # (no cross-filesystem copy).
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".volumes.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            # Don't leave a stray temp file behind on failure.
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- VolumeStore interface ---------------------------------------------
    def get(self, volume_id: str) -> VolumeRecord | None:
        with self._lock:
            rec = self._volumes.get(volume_id)
            # Hand back a copy so a caller mutating the result can't corrupt
            # in-memory state without going through put().
            return dataclasses.replace(rec) if rec is not None else None

    def put(self, record: VolumeRecord) -> None:
        with self._lock:
            self._volumes[record.id] = dataclasses.replace(record)
            self._flush_locked()

    def delete(self, volume_id: str) -> None:
        with self._lock:
            if volume_id in self._volumes:
                del self._volumes[volume_id]
                self._flush_locked()

    def list(self) -> list[VolumeRecord]:
        with self._lock:
            return [dataclasses.replace(r) for r in self._volumes.values()]

    # -- filesystem records ---------------------------------------------------
    def get_fs(self, fs_id: str) -> FilesystemRecord | None:
        with self._lock:
            rec = self._filesystems.get(fs_id)
            return dataclasses.replace(rec) if rec is not None else None

    def put_fs(self, record: FilesystemRecord) -> None:
        with self._lock:
            self._filesystems[record.id] = dataclasses.replace(record)
            self._flush_locked()

    def delete_fs(self, fs_id: str) -> None:
        """Remove a filesystem record. Refuses while volumes still reference it
        (delete or re-home them first) — a dangling fs_id is unrepresentable."""
        with self._lock:
            if any(v.fs_id == fs_id for v in self._volumes.values()):
                raise ValueError(f"filesystem {fs_id} still has volumes")
            if fs_id in self._filesystems:
                del self._filesystems[fs_id]
                self._flush_locked()

    def list_fs(self) -> list[FilesystemRecord]:
        with self._lock:
            return [dataclasses.replace(r) for r in self._filesystems.values()]

    def volumes_of(self, fs_id: str) -> list[VolumeRecord]:
        with self._lock:
            return [
                dataclasses.replace(v)
                for v in self._volumes.values()
                if v.fs_id == fs_id
            ]

    def sqlite_path_of(self, record: VolumeRecord) -> str:
        """Resolve a volume's backing file via its parent. KeyError on a
        dangling fs_id (corruption tripwire, not a soft None)."""
        with self._lock:
            return self._filesystems[record.fs_id].sqlite_path

    # -- pending unmounts ---------------------------------------------------
    def add_pending_unmount(self, mountpoint: str) -> None:
        with self._lock:
            if mountpoint not in self._pending:
                self._pending.append(mountpoint)
                self._flush_locked()

    def list_pending_unmounts(self) -> list[str]:
        with self._lock:
            return list(self._pending)

    def clear_pending_unmount(self, mountpoint: str) -> None:
        with self._lock:
            if mountpoint in self._pending:
                self._pending.remove(mountpoint)
                self._flush_locked()

    def close(self) -> None:
        # State is already durable after every mutation; nothing buffered.
        # Present for interface symmetry and a future SQLite backend.
        with self._lock:
            self._flush_locked()


__all__ = ["FilesystemRecord", "VolumeRecord", "VolumeStore", "JsonVolumeStore"]
# Copyright Michael Godfrey 2026 | aloecraft.org <michael@aloecraft.org>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
