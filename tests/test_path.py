# ./tests/test_path.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
Tests for the AloelitePath pathlib-style surface. Exercised through the Aloelite
wrapper (the layer AloelitePath binds to), against an in-memory volume.

Run:  pytest tests/test_path.py
"""

from __future__ import annotations

import pytest

from aloelite import errors
from aloelite.aloelite import Aloelite


@pytest.fixture
def m():
    with Aloelite(":memory:") as fs:
        vol = fs.create_volume("t")
        with fs.mount(vol.id) as mnt:
            yield mnt


def test_path_algebra(m):
    p = m.path("/") / "docs" / "a.txt"
    assert str(p) == "/docs/a.txt"
    assert p.name == "a.txt" and p.suffix == ".txt" and p.stem == "a"
    assert str(p.parent) == "/docs" and str(p.parent.parent) == "/"
    assert p.parts == ("docs", "a.txt")
    assert (m / "docs" / "a.txt") == p  # Mount / str builds the same path
    assert str(m.path("//docs///a.txt")) == "/docs/a.txt"  # normalization


def test_write_read_roundtrip(m):
    p = m / "notes.txt"
    p.write_text("héllo")  # creates the entry
    assert p.read_text() == "héllo"
    p.write_bytes(b"raw")  # atomic replace of an existing entry
    assert p.read_bytes() == b"raw"
    assert p.exists() and p.is_file() and not p.is_dir()


def test_mkdir_parents_and_iterdir(m):
    d = m / "a" / "b" / "c"
    d.mkdir(parents=True)
    assert d.is_dir() and (m / "a").is_dir()
    with pytest.raises(FileExistsError):
        d.mkdir()
    d.mkdir(exist_ok=True)  # no-op
    (d / "x").write_bytes(b"1")
    (d / "y").write_bytes(b"2")
    assert {c.name for c in d.iterdir()} == {"x", "y"}


def test_open_streaming_modes(m):
    p = m / "big"
    with p.open("wb") as w:  # creates + truncate-writes
        w.write(b"abc")
        w.write(b"def")
    with p.open("ab") as w:  # append
        w.write(b"!")
    with p.open("rb") as r:  # ranged read
        assert r.read() == b"abcdef!"
    with pytest.raises(ValueError):
        p.open("r+")


def test_append_bytes(m):
    p = m / "log"
    p.write_bytes(b"a")
    assert p.append_bytes(b"bc") == 3
    assert p.read_bytes() == b"abc"


def test_glob_and_rglob(m):
    d = m / "d"
    d.mkdir()
    (d / "sub").mkdir()
    (d / "a.txt").write_bytes(b"")
    (d / "b.log").write_bytes(b"")
    (d / "sub" / "c.txt").write_bytes(b"")
    assert {p.name for p in d.glob("*.txt")} == {"a.txt"}
    assert {p.name for p in d.rglob("*.txt")} == {"a.txt", "c.txt"}
    assert {str(p) for p in d.glob("sub/*.txt")} == {"/d/sub/c.txt"}


def test_rename_copy_remove(m):
    p = m / "f"
    p.write_bytes(b"x")
    q = p.rename("/g")  # returns the new path
    assert q.exists() and not p.exists()
    c = q.copy("/h")
    assert c.read_bytes() == b"x" and q.exists()
    c.unlink()
    assert not c.exists()
    d = m / "dir"
    d.mkdir()
    (d / "f").write_bytes(b"1")
    with pytest.raises(errors.NotEmpty):
        d.rmdir()
    d.rmtree()
    assert not d.exists()


def test_mount_by_name_and_create():
    with Aloelite(":memory:") as fs:
        with pytest.raises(errors.NotFound):
            fs.mount("nope")  # double miss, create=False
        with fs.mount("vault", create=True) as m:  # bootstraps + mounts
            m.put("/f", b"x")
        assert fs.resolve_volume_name("vault") is not None
        with fs.mount("vault") as m:  # resolves by name now
            assert m.read_all("/f") == b"x"
        with pytest.raises(errors.FsError):
            fs.create_volume("vault", ensure_unique=True)
        # duplicate names: latest id wins
        v2 = fs.create_volume("vault")  # permissive path still allowed
        assert fs.resolve_volume_name("vault") == v2.id
        # id-fallback: an explicit id still mounts even with names around
        with fs.mount(v2.id) as m:
            assert m.list() == []


def test_mkdir(m):
    m.mkdir("/a")
    with pytest.raises(errors.ContainerExists):
        m.mkdir("/a")
    assert m.mkdir("/a", exist_ok=True)  # returns existing id, no dup
    assert [e.name for e in m.list("/")].count("a") == 1
    with pytest.raises(errors.NotFound):
        m.mkdir("/x/y/z")  # strict default: no parents
    m.mkdir("/x/y/z", parents=True, exist_ok=True)  # mkdir -p
    assert (m / "x" / "y" / "z").is_dir()
    m.put("/f", b"")
    with pytest.raises(errors.ContainerExists):
        m.mkdir("/f", exist_ok=True)  # entry in the way: still raises


def test_direntry_path(m):
    m.mkdir("/folder")
    m.put("/folder/abc.txt", b"123")
    (root,) = [e for e in m.list("/") if e.name == "folder"]
    assert root.current_directory == "/" and root.path == "/folder"
    (child,) = m.list("/folder")
    assert child.current_directory == "/folder"
    assert child.path == "/folder/abc.txt"
    (deep,) = m.list("folder//")  # normalization
    assert deep.path == "/folder/abc.txt"


def test_put(m):
    p = m / "f"
    m.put("/f", b"one")  # create
    assert p.read_bytes() == b"one"
    m.put("/f", b"two")  # replace
    assert p.read_bytes() == b"two"
    m.put("/f", b"!", append=True)  # append existing
    assert p.read_bytes() == b"two!"
    m.put("/g", b"new", append=True)  # append to missing => create
    assert (m / "g").read_bytes() == b"new"


def test_metadata_property(m):
    p = m / "f"
    p.write_bytes(b"")
    assert p.metadata == {}
    p.set_metadata({"author": "mg"})
    assert p.metadata == {"author": "mg"}


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
