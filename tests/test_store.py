# ./tests/test_store.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
Standalone smoke test for manager.store.JsonVolumeStore.

Run from the repo root:   python3 -m manager.test_store
(no pytest, no dependencies — just asserts)
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time

from manager.store import JsonVolumeStore, VolumeRecord


def _rec(vid: str, **over) -> VolumeRecord:
    base = dict(
        id=vid,
        name=f"vol-{vid}",
        sqlite_path=f"/aloelite-root/{vid}.sqlite",
        encrypted=False,
        created_at=time.time(),
        mounted=False,
        mountpoint=None,
    )
    base.update(over)
    return VolumeRecord(**base)


def test_empty_store_materializes_file(tmp_path):
    path = os.path.join(tmp_path, "volumes.json")
    assert not os.path.exists(path)
    store = JsonVolumeStore(path)
    assert os.path.exists(path), "store should create the file on first init"
    assert store.list() == []
    with open(path) as fh:
        data = json.load(fh)
    assert data["version"] == 1
    assert data["volumes"] == {}
    assert data["pending_unmounts"] == []
    print("  ok: empty store materializes a valid file")


def test_crud_roundtrip(tmp_path):
    path = os.path.join(tmp_path, "volumes.json")
    store = JsonVolumeStore(path)

    store.put(_rec("a", name="alpha"))
    store.put(_rec("b", name="beta", encrypted=True))

    got = store.get("a")
    assert got is not None and got.name == "alpha"
    assert store.get("b").encrypted is True
    assert store.get("missing") is None

    ids = sorted(r.id for r in store.list())
    assert ids == ["a", "b"], ids

    # update in place
    store.put(_rec("a", name="alpha-renamed", mounted=True, mountpoint="/mnt/a"))
    a = store.get("a")
    assert a.name == "alpha-renamed" and a.mounted and a.mountpoint == "/mnt/a"
    assert len(store.list()) == 2, "update must not add a row"

    store.delete("a")
    assert store.get("a") is None
    assert sorted(r.id for r in store.list()) == ["b"]
    store.delete("nonexistent")  # must be a no-op, not raise
    print("  ok: CRUD round-trip")


def test_persistence_across_reopen(tmp_path):
    path = os.path.join(tmp_path, "volumes.json")
    s1 = JsonVolumeStore(path)
    s1.put(_rec("x", name="persist", encrypted=True, mounted=True, mountpoint="/mnt/x"))
    s1.add_pending_unmount("/mnt/stale")
    s1.close()

    s2 = JsonVolumeStore(path)
    x = s2.get("x")
    assert x is not None
    assert (
        x.name == "persist" and x.encrypted and x.mounted and x.mountpoint == "/mnt/x"
    )
    assert s2.list_pending_unmounts() == ["/mnt/stale"]
    print("  ok: state survives reopen")


def test_copy_isolation(tmp_path):
    path = os.path.join(tmp_path, "volumes.json")
    store = JsonVolumeStore(path)
    store.put(_rec("c", name="orig"))

    leaked = store.get("c")
    leaked.name = "mutated-outside"  # must not affect the store
    assert store.get("c").name == "orig", "get() must return an isolated copy"

    listed = store.list()[0]
    listed.mountpoint = "/tmp/hacked"
    assert store.get("c").mountpoint is None, "list() must return isolated copies"
    print("  ok: returned records are isolated copies")


def test_pending_unmounts(tmp_path):
    path = os.path.join(tmp_path, "volumes.json")
    store = JsonVolumeStore(path)
    store.add_pending_unmount("/mnt/a")
    store.add_pending_unmount("/mnt/b")
    store.add_pending_unmount("/mnt/a")  # dedup
    assert store.list_pending_unmounts() == ["/mnt/a", "/mnt/b"]
    store.clear_pending_unmount("/mnt/a")
    assert store.list_pending_unmounts() == ["/mnt/b"]
    store.clear_pending_unmount("/mnt/missing")  # no-op
    assert store.list_pending_unmounts() == ["/mnt/b"]
    print("  ok: pending-unmount add/dedup/clear")


def test_unknown_keys_tolerated(tmp_path):
    path = os.path.join(tmp_path, "volumes.json")
    # Simulate a file written by a future version with an extra field.
    payload = {
        "version": 1,
        "volumes": {
            "z": {
                "id": "z",
                "name": "future",
                "sqlite_path": "/x.sqlite",
                "encrypted": False,
                "created_at": 1.0,
                "mounted": False,
                "mountpoint": None,
                "some_future_field": "ignored",
            }
        },
        "pending_unmounts": [],
    }
    with open(path, "w") as fh:
        json.dump(payload, fh)
    store = JsonVolumeStore(path)
    assert store.get("z").name == "future"
    print("  ok: unknown keys tolerated on load")


def test_no_temp_files_left(tmp_path):
    path = os.path.join(tmp_path, "volumes.json")
    store = JsonVolumeStore(path)
    for i in range(20):
        store.put(_rec(str(i)))
    leftovers = [f for f in os.listdir(tmp_path) if f.startswith(".volumes.")]
    assert leftovers == [], f"temp files left behind: {leftovers}"
    print("  ok: no temp files left behind")


def test_concurrent_writes(tmp_path):
    """The lock should serialize concurrent putters without losing rows or
    corrupting the file."""
    path = os.path.join(tmp_path, "volumes.json")
    store = JsonVolumeStore(path)

    def worker(start):
        for i in range(start, start + 50):
            store.put(_rec(f"k{i}"))

    threads = [threading.Thread(target=worker, args=(b * 50,)) for b in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(store.list()) == 200, len(store.list())
    # File must still be valid JSON.
    reopened = JsonVolumeStore(path)
    assert len(reopened.list()) == 200
    print("  ok: concurrent writes serialize cleanly (200 rows)")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"running {len(tests)} tests\n")
    for fn in tests:
        with tempfile.TemporaryDirectory() as tmp:
            print(f"{fn.__name__}:")
            fn(tmp)
    print("\nall store tests passed ✓")


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
