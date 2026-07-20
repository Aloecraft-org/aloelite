# ./tests/test_direct.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""Lean tests for DirectSessionRegistry: real engine, tmp files, no FUSE."""

from __future__ import annotations

import threading

import pytest

from aloelite.aloelite import Aloelite
from manager import errors as merr
from manager.direct import DirectSessionRegistry
from manager.store import VolumeRecord


def _rec(tmp_path, vid="v1", name="vol", **over):
    base = dict(
        id=vid,
        name=name,
        fs_id=f"fs-{vid}",
        encrypted=False,
        created_at=0.0,
        mounted=False,
        mountpoint=None,
    )
    base.update(over)
    return VolumeRecord(**base)


def _sp(tmp_path, stem="v1") -> str:
    return str(tmp_path / f"{stem}.sqlite")


def test_unlock_operate_lock(tmp_path):
    reg = DirectSessionRegistry()
    rec = _rec(tmp_path)
    reg.unlock(rec, None, _sp(tmp_path))  # create=True bootstraps the volume
    assert reg.is_unlocked("v1")
    with reg.session("v1") as m:
        m.put("/f", b"hello")
        assert m.read_all("/f") == b"hello"
    with pytest.raises(merr.AlreadyMounted):
        reg.unlock(rec, None, _sp(tmp_path))
    reg.lock(rec)
    assert not reg.is_unlocked("v1")
    with pytest.raises(merr.NotMounted):
        with reg.session("v1"):
            pass
    with pytest.raises(merr.NotMounted):
        reg.lock(rec)


def test_encrypted_snapshot_and_bad_pin(tmp_path):
    rec = _rec(tmp_path, encrypted=True)
    sp = _sp(tmp_path)
    with Aloelite(sp) as fs:
        fs.create_volume(rec.name, pin=b"1234")
    reg = DirectSessionRegistry()
    with pytest.raises(merr.BadPin):
        reg.unlock(rec, b"wrong", sp)
    assert not reg.is_unlocked("v1")
    reg.unlock(rec, b"1234", sp)
    assert reg._sessions["v1"].session is not None  # triple parked in registry
    with reg.session("v1") as m:
        m.put("/s", b"secret")
        assert m.read_all("/s") == b"secret"
    with pytest.raises(merr.EncryptionMismatch):
        reg.unlock(_rec(tmp_path, vid="v1x", name=rec.name), None, sp)
    reg.lock(rec)


def test_cross_thread_serialized_ops(tmp_path):
    reg = DirectSessionRegistry()
    reg.unlock(_rec(tmp_path), None, _sp(tmp_path))
    errs: list[BaseException] = []

    def worker(i):
        try:
            for j in range(10):
                with reg.session("v1") as m:
                    m.put(f"/t{i}-{j}", bytes([i, j]))
        except BaseException as e:  # noqa: BLE001
            errs.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errs == []
    with reg.session("v1") as m:
        assert len([e for e in m.list("/") if e.visible]) == 40
    reg.shutdown()
    assert not reg.is_unlocked("v1")


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
