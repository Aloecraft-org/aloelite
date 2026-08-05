# ./manager/direct.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
manager.direct — held Mount sessions: the FUSE-less ("direct") frontend.

One DirectSession per unlocked volume: an Aloelite handle (owning the sqlite
connection, cipher installed at unlock) plus one engine Mount PER CLIENT —
a mount is a row, not a connection, so each attached browser/device holds
its own mount row with its own token, TTL, and lock attribution, all over
the single shared connection. The registry is the direct-mode counterpart
of MountSupervisor's thread table — same AlreadyMounted/NotMounted
semantics, same BadKey/EncryptionRequired translation — but with no thread,
no mountpoint, no readiness probe: the "mount" here is the engine session
itself.

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

import hmac
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from .errors import (
    AlreadyMounted,
    BadPin,
    EncryptionMismatch,
    MountFailed,
    NotAuthorized,
    NotMounted,
)
from .store import VolumeRecord

FRONTEND_DIRECT = "direct"
FRONTEND_FUSE = "fuse"

# Sentinel distinguishing "caller does no client auth" (session() without a
# token: AUTH off) from "client presented no token" (None: refuse). A missing
# cookie must never fall through to the shared primary mount.
_NO_AUTH: Any = object()


@dataclass
class DirectSession:
    fs: Any  # Aloelite (owns the connection; cipher installed at unlock)
    mount: Any  # primary Mount (the unlock's engine session; token-less access)
    session: dict | None  # snapshot of db.active_session (None for plain volumes)
    # token-mode clients: token hex -> Mount. One ENGINE MOUNT PER CLIENT --
    # a mount is a row, not a connection, so every attached browser/device
    # gets its own mount row (own token, own expires_at, own locks) over the
    # single shared sqlite connection. list_mounts is thereby the audit view
    # of who is attached. The primary mount above is clients[primary_token].
    clients: dict[str, Any] = field(default_factory=dict)
    primary_token: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)


def _token_hex(mount: Any) -> str:
    """Cookie value for a client mount: the engine's mount token T when the
    volume is encrypted (its designed user-held role, ENCRYPTION.md), else a
    manager-minted random id (a plain volume has no crypto session)."""
    t = getattr(mount, "token", None)
    return (t or os.urandom(16)).hex()


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
    def unlock(self, record: VolumeRecord, pin: bytes | None, sqlite_path: str) -> str:
        """Open the engine session for a volume; returns the first client's
        token (hex, cookie-ready). `sqlite_path` is resolved by the caller
        (store.sqlite_path_of) so the registry needs no store. BadKey ->
        BadPin and EncryptionRequired -> EncryptionMismatch, matching the
        supervisor."""
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
        token = _token_hex(mount)
        entry = DirectSession(
            fs=fs,
            mount=mount,
            session=dict(snapshot) if snapshot else None,
            clients={token: mount},
            primary_token=token,
        )
        with self._lock:
            if record.id in self._sessions:  # lost a race to another unlock
                mount.unmount()
                fs.close()
                raise AlreadyMounted(f"volume {record.id} already unlocked")
            self._sessions[record.id] = entry
        return token

    def attach(self, record: VolumeRecord, pin: bytes | None) -> str:
        """A NEW engine mount for an additional client on an already-unlocked
        volume; returns its token (hex). Encrypted volumes require the PIN
        again -- each device proves it may hold the key; a wrong PIN leaves
        the existing session untouched (ops.mount validates before touching
        connection state, and the failed txn rolls its mount row back)."""
        with self._lock:
            entry = self._sessions.get(record.id)
        if entry is None:
            raise NotMounted(f"volume {record.id} is not unlocked")
        with entry.lock:  # engine connection: serialize with in-flight ops
            with self._lock:
                if self._sessions.get(record.id) is not entry:
                    raise NotMounted(f"volume {record.id} is not unlocked")
            try:
                mount = entry.fs.mount(record.name, pin=pin)
            except BaseException as e:
                name = type(e).__name__
                if name == "BadKey":
                    raise BadPin("wrong PIN for encrypted volume") from e
                if name == "EncryptionRequired":
                    raise EncryptionMismatch(str(e) or "PIN/encryption mismatch") from e
                raise MountFailed(f"{name}: {e}") from e
            token = _token_hex(mount)
            entry.clients[token] = mount
        return token

    def detach(self, record: VolumeRecord, token: str) -> bool:
        """Unmount ONE client's engine mount. Returns True when it was the
        last client -- the caller should then treat the volume as locked
        (the underlying handle is closed here)."""
        with self._lock:
            entry = self._sessions.get(record.id)
        if entry is None:
            raise NotMounted(f"volume {record.id} is not unlocked")
        with entry.lock:
            match = self._client_key(entry, token)
            if match is None:
                raise NotAuthorized("no session for this client")
            mount = entry.clients.pop(match)
            try:
                mount.unmount()
            except Exception:
                pass  # already invalid/expired; the row is reapable by prune
            if entry.clients:
                return False
            with self._lock:
                if self._sessions.get(record.id) is entry:
                    del self._sessions[record.id]
            entry.fs.close()
            return True

    def lock(self, record: VolumeRecord) -> None:
        """Tear the WHOLE session down -- every client's mount. Order matters:
        drop the entry first so no new session() can win the lock, then
        unmount (clears cipher + triple), then close the connection. The
        snapshot dies with the entry (relock-without-PIN is a deliberate
        non-feature for now)."""
        with self._lock:
            entry = self._sessions.pop(record.id, None)
        if entry is None:
            raise NotMounted(f"volume {record.id} is not unlocked")
        with entry.lock:  # let any in-flight op finish
            try:
                for m in entry.clients.values():
                    try:
                        m.unmount()
                    except Exception:
                        pass  # expired/invalid rows are prune's problem
            finally:
                entry.fs.close()

    # -- access ---------------------------------------------------------------
    def is_unlocked(self, volume_id: str) -> bool:
        with self._lock:
            return volume_id in self._sessions

    @staticmethod
    def _client_key(entry: DirectSession, token: str | None) -> str | None:
        """Constant-time token lookup (a dict hit would leak timing)."""
        if not token:
            return None
        for key in entry.clients:
            if hmac.compare_digest(key, token):
                return key
        return None

    @contextmanager
    def session(self, volume_id: str, token: str | None = _NO_AUTH) -> Iterator[Any]:
        """Serialized access to a volume's Mount. With `token` OMITTED, yields
        the primary mount (AUTH off -- today's behavior). With `token` PASSED
        -- including None, an absent cookie -- yields that client's own mount
        or raises NotAuthorized, so locks and the mounts listing attribute to
        the client and a cookieless request can never fall through to the
        shared mount. Raises NotMounted if the volume is not unlocked (or was
        locked while waiting)."""
        with self._lock:
            entry = self._sessions.get(volume_id)
        if entry is None:
            raise NotMounted(f"volume {volume_id} is not unlocked")
        with entry.lock:
            # re-check: lock() may have torn it down while we waited
            with self._lock:
                if self._sessions.get(volume_id) is not entry:
                    raise NotMounted(f"volume {volume_id} is not unlocked")
            if token is _NO_AUTH:
                yield entry.mount
                return
            key = self._client_key(entry, token)
            if key is None:
                raise NotAuthorized("no session for this client")
            yield entry.clients[key]

    def shutdown(self) -> None:
        with self._lock:
            entries = list(self._sessions.items())
            self._sessions.clear()
        for _vid, entry in entries:
            with entry.lock:
                try:
                    for m in entry.clients.values():
                        try:
                            m.unmount()
                        except Exception:
                            pass
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
