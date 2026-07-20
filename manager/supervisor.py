# ./manager/supervisor.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
manager.supervisor — mount supervisor and per-mount FUSE thread lifecycle.

One daemon thread per active FUSE mount, each running its own trio.run() over
aloelite.fuse.fuse_main(...). A threading.Event is the stop signal; readiness is
detected by polling st_dev (a live FUSE mount differs from its parent dir).

Mount/PIN errors are surfaced synchronously to the API: fuse_main performs its
Aloelite mount *before* the blocking pyfuse3.main loop, so a wrong PIN makes the
thread exit fast with the error captured; the readiness poll notices the early
exit and translates BadKey -> BadPin, EncryptionRequired -> EncryptionMismatch.
This avoids a second (Argon2id-costly) validation mount.

The FUSE runner, readiness probe, and unmount command are injectable (with real
defaults) so the whole lifecycle is testable without root, FUSE, or aloelite.
"""

from __future__ import annotations

import logging
import os
import threading
import time

from .errors import (
    AlreadyMounted,
    BadPin,
    EncryptionMismatch,
    MountFailed,
    MountTimeout,
    NotMounted,
)
from .preflight import _is_fuse_active, _lazy_unmount
from .store import VolumeRecord, VolumeStore

_LOG = logging.getLogger("manager.supervisor")


def _default_fuse_runner(
    record: VolumeRecord,
    pin: bytes | None,
    mountpoint: str,
    stop_event: threading.Event,
    sqlite_path: str,
    *,
    allow_other: bool,
) -> None:
    """Run one mount's FUSE loop to completion (real path). Imports lazily so
    tests that inject a fake runner never need the aloelite package."""
    import functools
    import trio
    from aloelite.fuse import fuse_main

    trio.run(
        functools.partial(
            fuse_main,
            sqlite_path,
            record.name,
            mountpoint,
            pin,
            stop_event=stop_event,
            allow_other=allow_other,
            create=True,  # the manager created the volume row; tolerate races
        )
    )


class MountSupervisor:
    def __init__(
        self,
        store: VolumeStore,
        *,
        aloelite_root: str = "/aloelite-root",
        mnt_dir: str = "/mnt",
        allow_other: bool = True,
        ready_timeout: float = 2.0,
        join_timeout: float = 5.0,
        poll_interval: float = 0.1,
        fuse_runner=None,
        ready_probe=None,
        unmount_cmd=None,
    ) -> None:
        self.store = store
        self.aloelite_root = aloelite_root
        self.mnt_dir = mnt_dir
        self.allow_other = allow_other
        self.ready_timeout = ready_timeout
        self.join_timeout = join_timeout
        self.poll_interval = poll_interval
        self._ready_probe = ready_probe or _is_fuse_active
        self._unmount_cmd = unmount_cmd or _lazy_unmount
        # Default runner binds allow_other; a custom runner takes the 4 core args.
        # Runner contract: (record, pin, mountpoint, stop_event, sqlite_path).
        if fuse_runner is not None:
            self._fuse_runner = fuse_runner
        else:
            self._fuse_runner = lambda rec, pin, mp, ev, sp: _default_fuse_runner(
                rec, pin, mp, ev, sp, allow_other=self.allow_other
            )
        self._lock = threading.RLock()
        # mountpoint -> {"thread", "stop", "done", "result"}
        self._threads: dict[str, dict] = {}

    # -- helpers ------------------------------------------------------------
    def mountpoint_for(self, record: VolumeRecord) -> str:
        return f"{str(self.mnt_dir).rstrip('/')}/{record.id}"

    def _resolve_mountpoint(self, record: VolumeRecord) -> str:
        return record.mountpoint or self.mountpoint_for(record)

    @staticmethod
    def _rmdir_quiet(mountpoint: str) -> None:
        try:
            os.rmdir(mountpoint)
        except OSError:
            pass

    def _checkpoint_quiet(self, sqlite_path: str) -> None:
        """PRAGMA wal_checkpoint(TRUNCATE) on unmount (spec unmount step 4).
        Mirrors api._wal_checkpoint_truncate; kept inline to avoid a cross-import
        with the Flask layer."""
        try:
            import sqlite3

            con = sqlite3.connect(sqlite_path, timeout=5.0)
            try:
                con.execute("PRAGMA busy_timeout=5000")
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                con.close()
        except Exception as e:  # never let cleanup hygiene fail an unmount
            _LOG.warning("checkpoint on unmount failed for %s: %s", sqlite_path, e)

    @staticmethod
    def _translate(err: BaseException | None) -> Exception:
        if err is None:
            return MountFailed("FUSE thread exited before becoming ready")
        name = type(err).__name__
        if name == "BadKey":
            return BadPin("wrong PIN for encrypted volume")
        if name == "EncryptionRequired":
            return EncryptionMismatch(str(err) or "PIN/encryption mismatch")
        return MountFailed(f"{name}: {err}")

    # -- thread body --------------------------------------------------------
    def _thread_main(
        self, record, pin, mountpoint, stop_event, result_box, done_event, sqlite_path
    ) -> None:
        try:
            self._fuse_runner(record, pin, mountpoint, stop_event, sqlite_path)
        except BaseException as e:  # noqa: BLE001 — capture to report to caller
            result_box["error"] = e
        finally:
            done_event.set()

    def _await_ready(self, mountpoint, done_event, result_box) -> None:
        deadline = time.monotonic() + self.ready_timeout
        while time.monotonic() < deadline:
            if done_event.is_set():
                # Thread exited before readiness => failure (bad pin, init error…)
                raise self._translate(result_box.get("error"))
            if self._ready_probe(mountpoint):
                return
            time.sleep(self.poll_interval)
        raise MountTimeout(f"mount {mountpoint} not ready within {self.ready_timeout}s")

    # -- public API ---------------------------------------------------------
    def mount(self, record: VolumeRecord, pin: bytes | None, mp_path=None) -> str:
        sqlite_path = self.store.sqlite_path_of(record)
        name = mp_path or record.id
        mountpoint = f"{str(self.mnt_dir).rstrip('/')}/{name}"
        with self._lock:
            if mountpoint in self._threads:
                raise AlreadyMounted(f"volume {record.id} already mounted")
            os.makedirs(mountpoint, exist_ok=True)
            os.chmod(mountpoint, 0o777)
            stop_event = threading.Event()
            done_event = threading.Event()
            result_box: dict = {}
            t = threading.Thread(
                target=self._thread_main,
                args=(
                    record,
                    pin,
                    mountpoint,
                    stop_event,
                    result_box,
                    done_event,
                    sqlite_path,
                ),
                name=f"fuse-{record.id}",
                daemon=True,
            )
            self._threads[mountpoint] = {
                "thread": t,
                "stop": stop_event,
                "done": done_event,
                "result": result_box,
            }
            t.start()

        try:
            self._await_ready(mountpoint, done_event, result_box)
        except BaseException:
            # Roll back the reservation: stop, unmount, join, forget, rmdir.
            stop_event.set()
            self._unmount_cmd(mountpoint)
            t.join(self.join_timeout)
            with self._lock:
                self._threads.pop(mountpoint, None)
            self._rmdir_quiet(mountpoint)
            raise
        return mountpoint

    def unmount(self, record: VolumeRecord) -> None:
        mountpoint = self._resolve_mountpoint(record)
        with self._lock:
            entry = self._threads.get(mountpoint)
            if entry is None:
                raise NotMounted(f"volume {record.id} is not mounted")
        stop_event = entry["stop"]
        thread = entry["thread"]

        stop_event.set()
        self._unmount_cmd(mountpoint)  # fusermount3 -uz (lazy; safe if busy)
        thread.join(self.join_timeout)
        alive = thread.is_alive()
        with self._lock:
            self._threads.pop(mountpoint, None)

        if alive:
            # The session will finish detaching once consumers close their fds;
            # record it so the next preflight clears any kernel-side residue.
            self.store.add_pending_unmount(mountpoint)
            _LOG.warning(
                "unmount: thread for %s did not join in %.1fs; "
                "recorded pending unmount",
                mountpoint,
                self.join_timeout,
            )

        self._rmdir_quiet(mountpoint)
        self._checkpoint_quiet(self.store.sqlite_path_of(record))

    def _read_auto_pin(self, rec: VolumeRecord) -> bytes | None:
        if rec.pin_env:
            val = os.environ.get(rec.pin_env)
            if val is None:
                raise KeyError(f"env var {rec.pin_env!r} is not set")
            return val.encode()
        if rec.pin_file:
            with open(os.path.expanduser(rec.pin_file), "rb") as fh:
                return fh.read().rstrip(b"\n")
        return None

    def auto_mount_all(self, log=_LOG.warning) -> list[str]:
        """Mount every record flagged auto_mount. Failures are logged and
        skipped (one bad volume must not block the rest). Returns the
        mountpoints that came up."""
        mounted = []
        for rec in self.store.list():
            if not rec.auto_mount or rec.mounted:
                continue
            try:
                pin = self._read_auto_pin(rec)
                mp = self.mount(rec, pin, mp_path=rec.mount_name)
            except Exception as e:  # noqa: BLE001 — startup must not die
                log("auto-mount %s (%s) failed: %s", rec.id, rec.name, e)
                continue
            rec.mounted = True
            rec.mountpoint = mp
            self.store.put(rec)
            mounted.append(mp)
        return mounted

    def is_active(self, mountpoint: str | None) -> bool:
        if not mountpoint:
            return False
        return bool(self._ready_probe(mountpoint))

    def shutdown(self) -> None:
        with self._lock:
            entries = list(self._threads.items())
        # Signal + unmount all first, then join, so teardown overlaps.
        for mountpoint, entry in entries:
            entry["stop"].set()
            self._unmount_cmd(mountpoint)
        for mountpoint, entry in entries:
            entry["thread"].join(self.join_timeout)
            if entry["thread"].is_alive():
                self.store.add_pending_unmount(mountpoint)
                _LOG.warning("shutdown: thread for %s did not join", mountpoint)
            self._rmdir_quiet(mountpoint)
        with self._lock:
            self._threads.clear()
        # Spec shutdown step 3: all records mounted=False.
        for rec in self.store.list():
            if rec.mounted:
                rec.mounted = False
                rec.mountpoint = None
                self.store.put(rec)


__all__ = ["MountSupervisor"]
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
