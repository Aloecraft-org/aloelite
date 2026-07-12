# ./aloelite/aloelite.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
Aloelite — the ergonomic, Pythonic wrapper.

This is the ONLY layer that is allowed object state and sugar. It sits on top of
the flat function layer (operations.py) and adds nothing to the contract — the
other three implementations will each grow their own idiomatic wrapper over the
same operations. Two objects, each owning a resource as a context manager:

  Aloelite  — owns the file / connection (the transient physical attachment).
              `with Aloelite(path) as fs:` opens it; exit closes the connection.
              Note a mount is a ROW, not this connection: the connection is
              disposable, the mount id outlives it.

  Mount     — a handle bound to one mount id. `with fs.mount(vol) as m:` opens a
              session; exit unmounts it. Every method forwards to operations.*
              with the mount id already bound, so callers write m.list("/")
              instead of operations.list(db, mount_id, "/").

The streaming descriptor returned by m.open_read/open_write is itself a context
manager (its own concern: the lock lifecycle), so it composes:
    with fs.mount(vol) as m:
        with m.open_write("/f") as w:
            w.write(b"...")
"""

from __future__ import annotations

import builtins
from pathlib import Path as _FsPath
from typing import Iterator

from . import operations as ops
from .db import Db
from .descriptor import Descriptor
from .models import (
    ContentPruneReport,
    DirEntry,
    MountInfo,
    NodeInfo,
    PruneReport,
    VolumeInfo,
)
from .types import MountId, NodeId, VolumeId, WriteMode

# Default spec locations, resolved relative to this package. Override per call.
_PKG = _FsPath(__file__).resolve().parent
_DEFAULT_TEMPLATES = _PKG / "../config/sql-templates.yaml"
_DEFAULT_SCHEMA = _PKG / "../sql/schema.sql"


class Aloelite:
    """A handle to an Aloelite filesystem file (owns the connection)."""

    def __init__(
        self,
        path: str | _FsPath = ":memory:",
        *,
        templates_path: str | _FsPath = _DEFAULT_TEMPLATES,
        schema_path: str | _FsPath | None = _DEFAULT_SCHEMA,
        ensure_schema: bool = True,
    ) -> None:
        # The schema is idempotent (CREATE ... IF NOT EXISTS), so applying it on
        # open is safe for both new and existing files.
        self._db = Db.open(
            path,
            templates_path,
            schema_path=schema_path if ensure_schema else None,
        )

    # -- connection lifecycle (this object's context manager) ----------------
    def __enter__(self) -> "Aloelite":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._db.close()

    @property
    def db(self) -> Db:
        """Escape hatch to the connection wrapper (for advanced/raw use)."""
        return self._db

    # -- volumes -------------------------------------------------------------
    def create_volume(
        self,
        name: str | None = None,
        chunk_size: int = 1048576,
        pin: bytes | None = None,
        *,
        enc_mode: str = "convergent",
    ) -> VolumeInfo:
        return ops.create_volume(self._db, name, chunk_size, pin, enc_mode=enc_mode)

    def list_volumes(self) -> builtins.list[VolumeInfo]:
        return ops.list_volumes(self._db)

    # -- mounts --------------------------------------------------------------
    def mount(
        self,
        volume: VolumeId,
        at: str = "/",
        ttl_ms: int | None = None,
        pin: bytes | None = None,
    ) -> "Mount":
        mid = ops.mount(self._db, volume, at, ttl_ms, pin)
        sess = self._db.active_session
        token = sess["token"] if sess and sess.get("mount_id") == mid else None
        return Mount(self._db, mid, token=token)

    def attach(self, mount: MountId) -> "Mount":
        """Re-attach to an existing mount row (e.g. one created elsewhere and
        resumed on this connection). The mount is validated lazily, per op."""
        return Mount(self._db, mount)

    def list_mounts(
        self,
        volume: VolumeId | None = None,
        *,
        include_unmounted: bool = False,
    ) -> builtins.list[MountInfo]:
        """Durable mounts on this filesystem (records, not live handles).
        Re-attach to one with attach(info.id)."""
        return ops.list_mounts(
            self._db, volume, include_unmounted=include_unmounted
        )

    # -- maintenance ---------------------------------------------------------
    def prune(self, volume: VolumeId | None = None) -> PruneReport:
        return ops.prune(self._db, volume)

    def prune_content(self, volume: VolumeId | None = None) -> ContentPruneReport:
        return ops.prune_content(self._db, volume)

    def health_check(self) -> builtins.list:
        return ops.health_check(self._db)


class Mount:
    """A bound mount/session handle. Context manager: exit unmounts."""

    def __init__(
        self, db: Db, mount_id: MountId, *, token: bytes | None = None
    ) -> None:
        self._db = db
        self.id = mount_id
        # The per-mount token (encrypted volumes only); None when unencrypted.
        # Runtime-only handle that, with N_m, stands in for the PIN this session.
        self.token = token

    # -- the mount's context manager (session lifecycle) ---------------------
    def __enter__(self) -> "Mount":
        return self

    def __exit__(self, *exc: object) -> None:
        self.unmount()

    def unmount(self) -> None:
        ops.unmount(self._db, self.id)

    def info(self) -> MountInfo:
        return ops.mount_info(self._db, self.id)

    def renew(self, ttl_ms: int | None = None) -> MountInfo:
        return ops.renew_mount(self._db, self.id, ttl_ms)

    # -- ergonomic path surface -----------------------------------------------
    def path(self, path: str = "/") -> "AloelitePath":
        """A pathlib-style handle bound to this mount (see aloelite.path)."""
        from .path import AloelitePath

        return AloelitePath(self, path)

    def __truediv__(self, other) -> "AloelitePath":
        """`mount / "docs" / "a.txt"` builds an AloelitePath from the mount root."""
        return self.path("/") / other

    # -- read ----------------------------------------------------------------
    def stat(self, path: str) -> NodeInfo:
        return ops.stat(self._db, self.id, path)

    def stat_by_id(self, node: NodeId) -> NodeInfo:
        return ops.stat_by_id(self._db, self.id, node)

    def exists(self, path: str) -> bool:
        return ops.exists(self._db, self.id, path)

    def list(self, path: str = "/") -> builtins.list[DirEntry]:
        return ops.list(self._db, self.id, path)

    def read_all(self, path: str) -> bytes:
        return ops.read_all(self._db, self.id, path)

    def path_of(self, node: NodeId) -> str:
        return ops.path_of(self._db, self.id, node)

    # -- structural ----------------------------------------------------------
    def create_container(self, path: str) -> NodeId:
        return ops.create_container(self._db, self.id, path)

    def create_entry(self, path: str, data: bytes | None = None) -> NodeId:
        return ops.create_entry(self._db, self.id, path, data)

    def write_all(self, path: str, data: bytes) -> None:
        ops.write_all(self._db, self.id, path, data)
        
    def append(self, path: str, data: bytes) -> int:
        return ops.append(self._db, self.id, path, data)

    def rename(self, path: str, name: str) -> None:
        ops.rename(self._db, self.id, path, name)

    def set_metadata(self, path: str, metadata: dict[str, str]) -> None:
        ops.set_metadata(self._db, self.id, path, metadata)

    def set_retention(self, path: str, keep: int | None) -> None:
        ops.set_retention(self._db, self.id, path, keep)

    def move(self, src: str, dst: str) -> None:
        ops.move(self._db, self.id, src, dst)

    def remove(self, path: str) -> None:
        ops.remove(self._db, self.id, path)

    def remove_recursive(self, path: str) -> None:
        ops.remove_recursive(self._db, self.id, path)

    def copy(self, src: str, dst: str) -> NodeId:
        return ops.copy(self._db, self.id, src, dst)

    def pack(self, path: str) -> NodeId:
        return ops.pack(self._db, self.id, path)

    def unpack(self, path: str) -> None:
        ops.unpack(self._db, self.id, path)

    # -- streaming -----------------------------------------------------------
    def open_read(self, path: str) -> Descriptor:
        return ops.open_read(self._db, self.id, path)

    def open_write(self, path: str, mode: WriteMode = WriteMode.TRUNCATE) -> Descriptor:
        return ops.open_write(self._db, self.id, path, mode)


__all__ = ["Aloelite", "Mount"]
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
