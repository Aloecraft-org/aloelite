# ./tests/test_fuse_overlay.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
The Python daemon's dirty-extent overlay (`aloelite.fuse._OpenFile`), driven
directly against a volume, no kernel. The live-mount behaviour is in
test_fuse_mount.py; this pins what a flush commits, which a mount cannot see.
Mirrors the overlay unit tests in rust/aloelite-fuse/src/overlay.rs.
"""

from __future__ import annotations

import pytest

from aloelite import errors
from aloelite.aloelite import Aloelite

fuse = pytest.importorskip("aloelite.fuse", reason="pyfuse3 not installed")


@pytest.fixture
def m(tmp_path):
    with Aloelite(str(tmp_path / "o.fs")) as fs:
        vol = fs.create_volume("data")
        with fs.mount(vol.id) as mount:
            yield mount


def test_a_flush_of_scattered_extents_is_one_version(m):
    """A database checkpoint: many small writes far apart, then one fsync.
    Each extent used to land as its own write_range, so its own version."""
    m.put("/f", b"." * (64 << 10))
    before = m.stat("/f").version
    of = fuse._OpenFile(m, "/f")
    for i in range(200):
        of.write(i * 300, b"W")
    of.flush()
    assert m.stat("/f").version == before + 1
    got = m.read_all("/f")
    assert all(got[i * 300] == ord("W") for i in range(200))
    assert got.count(b"W") == 200


def test_a_refused_flush_keeps_the_dirty_bytes(m):
    """All or nothing: when the commit is refused, nothing lands and the
    extents stay buffered for the next attempt."""
    m.put("/f", b"0123456789")
    of = fuse._OpenFile(m, "/f")
    of.write(2, b"XY")
    of.write(8, b"Z")
    m.remove("/f")
    with pytest.raises(errors.NotFound):
        of.flush()
    assert of.extents and of.dirty


def test_create_takes_a_chunk_size_and_an_existing_volume_keeps_its_own(tmp_path):
    """--chunk-size reaches the volume --create makes; CV-1 fixes it there, so
    naming a different size for a volume that exists is refused."""
    with Aloelite(str(tmp_path / "c.fs")) as fs:
        vid = fuse._find_or_create_volume(fs, "db", create=True, chunk_size=65536)
        assert fs.db.chunk_size_of(vid) == 65536
        same = fuse._find_or_create_volume(fs, "db", create=True, chunk_size=65536)
        assert same == vid
        assert fuse._find_or_create_volume(fs, "db", create=True) == vid
        with pytest.raises(fuse.ChunkSizeFixed):
            fuse._find_or_create_volume(fs, "db", create=True, chunk_size=16384)
        plain = fuse._find_or_create_volume(fs, "plain", create=True)
        assert fs.db.chunk_size_of(plain) == 1 << 20


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
