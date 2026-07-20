# ./manager/test_supervisor.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
Lifecycle tests for manager.supervisor.MountSupervisor using a fake FUSE
backend (no root, no FUSE, no aloelite package).

Run standalone:  python3 -m manager.test_supervisor
Or under pytest: the test_* functions create their own temp dirs.
"""

from __future__ import annotations

import tempfile
import threading
import time

from manager.store import FilesystemRecord, JsonVolumeStore, VolumeRecord
from manager.supervisor import MountSupervisor
from manager import errors as merr


class FakeBackend:
    """Simulates a per-mount FUSE thread + readiness probe + unmount command.

    behavior:
      ok           become ready, run until stop_event, exit cleanly
      never_ready  run until stop_event but never report ready (-> timeout)
      exit_early   exit immediately, no error, never ready (-> MountFailed)
      badkey       raise an exc named BadKey (-> BadPin)
      encreq       raise an exc named EncryptionRequired (-> EncryptionMismatch)
      ignore_stop  become ready, then sleep ignoring stop (-> won't join)
    """

    def __init__(self, behavior: str = "ok") -> None:
        self.behavior = behavior
        self._live: set[str] = set()
        self._lock = threading.Lock()
        self.unmount_calls: list[str] = []

    def runner(self, record, pin, mountpoint, stop_event, sqlite_path=None):
        b = self.behavior
        if b == "badkey":
            raise type("BadKey", (Exception,), {})("wrong pin")
        if b == "encreq":
            raise type("EncryptionRequired", (Exception,), {})("enc mismatch")
        if b == "exit_early":
            return
        if b != "never_ready":
            with self._lock:
                self._live.add(mountpoint)
        if b == "ignore_stop":
            time.sleep(1.0)  # deliberately ignore stop_event
        else:
            while not stop_event.is_set():
                time.sleep(0.01)
        with self._lock:
            self._live.discard(mountpoint)

    def ready(self, mountpoint):
        with self._lock:
            return mountpoint in self._live

    def unmount_cmd(self, mountpoint):
        self.unmount_calls.append(mountpoint)


def _mk(tmp, behavior="ok", **kw):
    store = JsonVolumeStore(f"{tmp}/volumes.json")
    fb = FakeBackend(behavior)
    sup = MountSupervisor(
        store,
        aloelite_root=tmp,
        mnt_dir=tmp,
        fuse_runner=fb.runner,
        ready_probe=fb.ready,
        unmount_cmd=fb.unmount_cmd,
        ready_timeout=0.5,
        join_timeout=0.3,
        poll_interval=0.02,
        **kw,
    )
    store.put_fs(
        FilesystemRecord(
            id="fs1",
            display_name="vol1",
            sqlite_path=f"{tmp}/v1.sqlite",
            created_at=time.time(),
        )
    )
    rec = VolumeRecord(
        id="v1",
        name="vol1",
        fs_id="fs1",
        encrypted=False,
        created_at=time.time(),
        mounted=False,
        mountpoint=None,
    )
    return store, fb, sup, rec


def test_mount_success(tmp_path):
    store, fb, sup, rec = _mk(tmp_path, "ok")
    mp = sup.mount(rec, None)
    assert mp == f"{tmp_path}/v1"
    assert sup.is_active(mp) is True
    sup.unmount(rec)
    assert sup.is_active(mp) is False
    print("  ok: mount becomes ready, unmount tears down")


def test_already_mounted(tmp_path):
    store, fb, sup, rec = _mk(tmp_path, "ok")
    sup.mount(rec, None)
    try:
        sup.mount(rec, None)
        assert False, "expected AlreadyMounted"
    except merr.AlreadyMounted:
        pass
    sup.unmount(rec)
    print("  ok: double mount raises AlreadyMounted")


def test_bad_pin(tmp_path):
    store, fb, sup, rec = _mk(tmp_path, "badkey")
    try:
        sup.mount(rec, b"nope")
        assert False, "expected BadPin"
    except merr.BadPin:
        pass
    assert sup._threads == {}, "reservation must be rolled back"
    print("  ok: BadKey -> BadPin, reservation rolled back")


def test_encryption_mismatch(tmp_path):
    store, fb, sup, rec = _mk(tmp_path, "encreq")
    try:
        sup.mount(rec, None)
        assert False, "expected EncryptionMismatch"
    except merr.EncryptionMismatch:
        pass
    print("  ok: EncryptionRequired -> EncryptionMismatch")


def test_exit_before_ready(tmp_path):
    store, fb, sup, rec = _mk(tmp_path, "exit_early")
    try:
        sup.mount(rec, None)
        assert False, "expected MountFailed"
    except merr.MountFailed:
        pass
    assert sup._threads == {}
    print("  ok: early clean exit -> MountFailed")


def test_mount_timeout(tmp_path):
    store, fb, sup, rec = _mk(tmp_path, "never_ready")
    t0 = time.monotonic()
    try:
        sup.mount(rec, None)
        assert False, "expected MountTimeout"
    except merr.MountTimeout:
        pass
    assert time.monotonic() - t0 >= 0.5, "should wait the full ready_timeout"
    assert sup._threads == {}, "reservation cleaned up after timeout"
    assert fb.unmount_calls, "unmount command issued during rollback"
    print("  ok: never-ready -> MountTimeout, cleaned up")


def test_unmount_not_mounted(tmp_path):
    store, fb, sup, rec = _mk(tmp_path, "ok")
    try:
        sup.unmount(rec)
        assert False, "expected NotMounted"
    except merr.NotMounted:
        pass
    print("  ok: unmount when not mounted raises NotMounted")


def test_pending_unmount_on_join_failure(tmp_path):
    store, fb, sup, rec = _mk(tmp_path, "ignore_stop")
    mp = sup.mount(rec, None)  # becomes ready, then ignores stop
    sup.unmount(rec)  # join times out (0.3s < 1.0s sleep)
    assert mp in store.list_pending_unmounts(), "should record a pending unmount"
    assert sup._threads == {}, "thread entry cleared even when join fails"
    print("  ok: non-joining thread -> pending unmount recorded")


def test_auto_mount_all(tmp_path):
    store, fb, sup, rec = _mk(tmp_path, "ok")
    rec.auto_mount = True
    store.put(rec)
    mounted = sup.auto_mount_all(log=lambda *a: None)
    assert mounted == [f"{tmp_path}/v1"]
    assert store.get("v1").mounted is True
    sup.unmount(store.get("v1"))
    print("  ok: auto_mount_all mounts flagged volumes")


def test_auto_mount_missing_pin_env_skips(tmp_path):
    store, fb, sup, rec = _mk(tmp_path, "ok")
    rec.auto_mount = True
    rec.encrypted = True
    rec.pin_env = "ALOE_TEST_PIN_DOES_NOT_EXIST"
    store.put(rec)
    logged = []
    mounted = sup.auto_mount_all(log=lambda msg, *a: logged.append(msg % a))
    assert mounted == [] and logged, "failure logged, startup not aborted"
    assert store.get("v1").mounted is False
    print("  ok: auto-mount failure is logged and skipped")


def test_recover_stale_direct(tmp_path):
    from manager.preflight import recover_stale_mounts

    store, fb, sup, rec = _mk(tmp_path, "ok")
    rec.mounted = True
    rec.frontend = "direct"
    store.put(rec)
    results = recover_stale_mounts(store)
    assert any("stale direct" in r.name for r in results)
    got = store.get("v1")
    assert got.mounted is False and got.frontend is None
    assert fb.unmount_calls == []  # no fusermount for a direct record
    print("  ok: stale direct session cleared at startup")


def test_shutdown_stops_all(tmp_path):
    store, fb, sup, rec = _mk(tmp_path, "ok")
    sup.mount(rec, None)
    rec.mounted = True
    rec.mountpoint = sup.mountpoint_for(rec)
    store.put(rec)
    sup.shutdown()
    assert sup._threads == {}
    assert store.get("v1").mounted is False, "shutdown clears mounted flag"
    print("  ok: shutdown stops mounts and clears flags")


def main():
    tests = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    print(f"running {len(tests)} supervisor tests\n")
    for fn in tests:
        with tempfile.TemporaryDirectory() as tmp:
            print(f"{fn.__name__}:")
            fn(tmp)
    print("\nall supervisor tests passed ✓")


if __name__ == "__main__":
    main()
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
