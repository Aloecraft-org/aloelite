# ./tests/test_integration.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
End-to-end integration test: the real Flask API + real MountSupervisor, with
only the FUSE backend faked. The checkpoint/export path runs against a real
WAL-mode SQLite file, so the backup machinery is exercised for real.

Run standalone:  python3 -m manager.test_integration
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time

from manager.api import create_app
from manager.store import JsonVolumeStore, VolumeRecord
from manager.supervisor import MountSupervisor
from manager.test_supervisor import FakeBackend


def _seed_backing_db(path: str, rows: int = 500) -> None:
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t(x)")
    con.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])
    con.commit()
    con.close()


def test_full_request_cycle(tmp_path):
    sp = os.path.join(tmp_path, "abc.sqlite")
    _seed_backing_db(sp)

    store = JsonVolumeStore(os.path.join(tmp_path, "volumes.json"))
    store.put(
        VolumeRecord(
            id="abc",
            name="myphotos",
            sqlite_path=sp,
            encrypted=False,
            created_at=time.time(),
            mounted=False,
            mountpoint=None,
        )
    )

    fb = FakeBackend("ok")
    sup = MountSupervisor(
        store,
        aloelite_root=tmp_path,
        mnt_dir=tmp_path,
        fuse_runner=fb.runner,
        ready_probe=fb.ready,
        unmount_cmd=fb.unmount_cmd,
        ready_timeout=0.5,
        join_timeout=0.3,
        poll_interval=0.02,
    )
    app = create_app(
        store, sup, aloelite_root=tmp_path, host_mnt_prefix="/mnt/aloelite"
    )
    c = app.test_client()

    # mount
    r = c.post("/volumes/abc/mount", json={})
    assert r.status_code == 200
    assert r.get_json()["host_path"] == "/mnt/aloelite/abc"
    assert store.get("abc").mounted is True

    # status reflects readiness
    assert c.get("/volumes/abc/mount").get_json()["ready"] is True
    assert c.get("/volumes").get_json()[0]["mounted"] is True
    assert c.get("/volumes/abc/stat").get_json()["size_bytes"] > 0

    # checkpoint leaves no WAL frames
    assert c.post("/volumes/abc/checkpoint").get_json()["wal_frames_remaining"] == 0

    # export streams an exact-length, self-contained, reusable snapshot
    r = c.get("/volumes/abc/export")
    data = r.get_data()
    assert r.status_code == 200
    assert r.headers["Content-Length"] == str(len(data))
    assert data[:16] == b"SQLite format 3\x00"
    snap = os.path.join(tmp_path, "snap.sqlite")
    with open(snap, "wb") as fh:
        fh.write(data)
    assert sqlite3.connect(snap).execute("SELECT count(*) FROM t").fetchone()[0] == 500

    # unmount, idempotency, remount
    assert c.delete("/volumes/abc/mount").status_code == 204
    assert store.get("abc").mounted is False
    assert c.delete("/volumes/abc/mount").status_code == 404
    assert c.post("/volumes/abc/mount", json={}).status_code == 200

    sup.shutdown()
    assert store.get("abc").mounted is False
    print("  ok: full mount/status/stat/checkpoint/export/unmount/remount cycle")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        print("test_full_request_cycle:")
        test_full_request_cycle(tmp)
    print("\nintegration test passed ✓")


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
