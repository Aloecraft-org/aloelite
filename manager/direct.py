# ./manager/direct.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
manager.direct — held Mount sessions: the FUSE-less ("direct") frontend.

One DirectSession per unlocked volume: an Aloelite handle (owning the sqlite
connection, cipher installed at unlock) plus its live Mount. The registry is
the direct-mode counterpart of MountSupervisor's thread table — same
AlreadyMounted/NotMounted semantics, same BadKey/EncryptionRequired
translation — but with no thread, no mountpoint, no readiness probe: the
"mount" here is the engine session itself.

Threading: Flask serves threaded, the engine owns ONE sqlite connection per
session and adds no thread safety, so every operation on a session must run
under that session's lock. Use `with registry.session(volume_id) as m:` —
it acquires the lock and yields the Mount. One connection serves exactly one
volume (registry invariant), so per-connection cipher/session state never
crosses volumes.

The unlock also snapshots db.active_session (the T / N_m / mount_secret
triple minted by ops.mount) into the entry. Nothing consumes it yet: it is
parked here in its designed role — the manager holds the token, not the PIN —
so the future resume_session/token path (JWT) reads it from the registry
instead of re-deriving anything. Discarded on lock().
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from .errors import AlreadyMounted, BadPin, EncryptionMismatch, MountFailed, NotMounted
from .store import VolumeRecord

FRONTEND_DIRECT = "direct"
FRONTEND_FUSE = "fuse"


@dataclass
class DirectSession:
    fs: Any  # Aloelite (owns the connection; cipher installed at unlock)
    mount: Any  # Mount (live engine session)
    session: dict | None  # snapshot of db.active_session (None for plain volumes)
    lock: threading.Lock = field(default_factory=threading.Lock)


class DirectSessionRegistry:
    """volume_id -> DirectSession. All access serialized per session."""

    def __init__(self, *, fs_factory=None) -> None:
        # fs_factory(sqlite_path) -> Aloelite-like; injectable for tests.
        self._fs_factory = fs_factory or self._default_factory
        self._lock = threading.RLock()
        self._sessions: dict[str, DirectSession] = {}

    @staticmethod
    def _default_factory(sqlite_path: str):
        from aloelite.aloelite import Aloelite  # lazy (mirrors api.py)

        # Ops run on whichever Flask worker thread holds the session lock.
        return Aloelite(sqlite_path, check_same_thread=False)

    # -- lifecycle -----------------------------------------------------------
    def unlock(
        self, record: VolumeRecord, pin: bytes | None, sqlite_path: str
    ) -> None:
        """Open the engine session for a volume. `sqlite_path` is resolved by
        the caller (store.sqlite_path_of) so the registry needs no store.
        BadKey -> BadPin and EncryptionRequired -> EncryptionMismatch,
        matching the supervisor."""
        with self._lock:
            if record.id in self._sessions:
                raise AlreadyMounted(f"volume {record.id} already unlocked")
        fs = self._fs_factory(sqlite_path)
        try:
            mount = fs.mount(record.name, pin=pin, create=True)
        except BaseException as e:
            fs.close()
            name = type(e).__name__
            if name == "BadKey":
                raise BadPin("wrong PIN for encrypted volume") from e
            if name == "EncryptionRequired":
                raise EncryptionMismatch(str(e) or "PIN/encryption mismatch") from e
            raise MountFailed(f"{name}: {e}") from e
        snapshot = getattr(fs.db, "active_session", None)
        entry = DirectSession(fs=fs, mount=mount, session=dict(snapshot) if snapshot else None)
        with self._lock:
            if record.id in self._sessions:  # lost a race to another unlock
                mount.unmount()
                fs.close()
                raise AlreadyMounted(f"volume {record.id} already unlocked")
            self._sessions[record.id] = entry

    def lock(self, record: VolumeRecord) -> None:
        """Tear the session down. Order matters: drop the entry first so no new
        session() can win the lock, then unmount (clears cipher + triple), then
        close the connection. The snapshot dies with the entry (relock-without-
        PIN is a deliberate non-feature for now)."""
        with self._lock:
            entry = self._sessions.pop(record.id, None)
        if entry is None:
            raise NotMounted(f"volume {record.id} is not unlocked")
        with entry.lock:  # let any in-flight op finish
            try:
                entry.mount.unmount()
            finally:
                entry.fs.close()

    # -- access ---------------------------------------------------------------
    def is_unlocked(self, volume_id: str) -> bool:
        with self._lock:
            return volume_id in self._sessions

    @contextmanager
    def session(self, volume_id: str) -> Iterator[Any]:
        """Serialized access to a volume's Mount. Raises NotMounted if the
        volume is not unlocked (or was locked while waiting)."""
        with self._lock:
            entry = self._sessions.get(volume_id)
        if entry is None:
            raise NotMounted(f"volume {volume_id} is not unlocked")
        with entry.lock:
            # re-check: lock() may have torn it down while we waited
            with self._lock:
                if self._sessions.get(volume_id) is not entry:
                    raise NotMounted(f"volume {volume_id} is not unlocked")
            yield entry.mount

    def shutdown(self) -> None:
        with self._lock:
            entries = list(self._sessions.items())
            self._sessions.clear()
        for _vid, entry in entries:
            with entry.lock:
                try:
                    entry.mount.unmount()
                finally:
                    entry.fs.close()


__all__ = [
    "DirectSession",
    "DirectSessionRegistry",
    "FRONTEND_DIRECT",
    "FRONTEND_FUSE",
]
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
