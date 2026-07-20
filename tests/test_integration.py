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
from manager.store import FilesystemRecord, JsonVolumeStore, VolumeRecord
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
    store.put_fs(
        FilesystemRecord(
            id="fsabc", display_name="myphotos", sqlite_path=sp, created_at=time.time()
        )
    )
    store.put(
        VolumeRecord(
            id="abc",
            name="myphotos",
            fs_id="fsabc",
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
    assert store.get("abc").frontend == "fuse"
    assert c.delete("/volumes/abc/mount").status_code == 204

    # direct mode: unlock via the same endpoint, explorer works, lock again.
    # (The seeded file is a bare WAL db, not an aloelite fs, so give direct
    # mode a real one via the create endpoint.)
    r = c.post("/volumes", json={"name": "directvol"})
    assert r.status_code == 201
    dvid = r.get_json()["id"]
    r = c.post(f"/volumes/{dvid}/mount", json={"mode": "direct"})
    assert r.status_code == 200 and r.get_json()["frontend"] == "direct"
    assert store.get(dvid).mounted and store.get(dvid).mountpoint is None
    assert c.get(f"/volumes/{dvid}/mount").get_json()["ready"] is True
    assert c.post(f"/volumes/{dvid}/mount", json={"mode": "direct"}).status_code == 409
    # explorer over the Mount API
    assert c.post(f"/volumes/{dvid}/files/mkdir?path=/docs").status_code == 201
    import io
    r = c.post(
        f"/volumes/{dvid}/files/upload?path=/docs",
        data={"file": (io.BytesIO(b"direct bytes"), "a.txt")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 201
    names = {e["name"] for e in c.get(f"/volumes/{dvid}/files?path=/docs").get_json()}
    assert names == {"a.txt"}
    assert c.get(
        f"/volumes/{dvid}/files/download?path=/docs/a.txt"
    ).get_data() == b"direct bytes"
    assert c.delete(f"/volumes/{dvid}/mount").status_code == 204
    assert store.get(dvid).frontend is None

    # filesystems: nested listing, rename, export, import round-trip
    fss = c.get("/filesystems").get_json()
    mine = [x for x in fss if any(v["id"] == dvid for v in x["volumes"])]
    assert len(mine) == 1
    fid = mine[0]["id"]
    assert c.patch(
        f"/filesystems/{fid}", json={"display_name": "travel.fs"}
    ).status_code == 200
    r = c.get(f"/filesystems/{fid}/export")
    assert r.status_code == 200
    assert 'filename="travel.fs"' in r.headers["Content-Disposition"]
    blob = r.get_data()
    assert blob[:16] == b"SQLite format 3\x00"
    r = c.post(
        "/filesystems/import",
        data={"file": (io.BytesIO(blob), "roundtrip.fs")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 201
    imp = r.get_json()
    assert imp["display_name"] == "roundtrip.fs"
    assert [v["name"] for v in imp["volumes"]] == ["directvol"]
    assert imp["volumes"][0]["encrypted"] is False
    # imported volume unlocks and reads back the content
    ivid = imp["volumes"][0]["id"]
    assert c.post(f"/volumes/{ivid}/mount", json={"mode": "direct"}).status_code == 200
    assert c.get(
        f"/volumes/{ivid}/files/download?path=/docs/a.txt"
    ).get_data() == b"direct bytes"
    assert c.delete(f"/volumes/{ivid}/mount").status_code == 204
    # junk import is rejected and leaves nothing behind
    assert c.post(
        "/filesystems/import",
        data={"file": (io.BytesIO(b"not a database"), "junk.bin")},
        content_type="multipart/form-data",
    ).status_code == 400

    sup.shutdown()
    app.config["DIRECT_REGISTRY"].shutdown()
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
