# ./tests/test_era2_ops.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
Era-2 operations at the ops/wrapper layer — hardlinks (D-5), special node
types (D-3), ownership, and xattrs — no kernel required, so these run in any
CI environment. The kernel-facing equivalents live in
tests/test_posix_surface.py and self-skip without /dev/fuse.
"""

from __future__ import annotations

import pytest

from aloelite import errors
from aloelite.aloelite import Aloelite
from aloelite.types import NodeType


@pytest.fixture
def m():
    fs = Aloelite()
    vol = fs.create_volume("v", enc_mode="none").id
    mount = fs.mount(vol)
    yield mount
    fs.close()


# -- hardlinks (D-5) --------------------------------------------------------
def test_hardlink_shares_the_node(m):
    m.create_entry("/a", b"original")
    m.hardlink("/a", "/b")
    assert m.stat("/a").id == m.stat("/b").id
    assert m.stat("/a").nlink == 2
    m.write_all("/b", b"through b")
    assert m.read_all("/a") == b"through b"


def test_unlink_one_placement_keeps_the_other(m):
    m.create_entry("/a", b"x")
    m.hardlink("/a", "/b")
    m.remove("/a")
    assert not m.exists("/a")
    assert m.read_all("/b") == b"x"
    assert m.stat("/b").nlink == 1


def test_hardlink_across_containers_with_new_name(m):
    m.create_container("/d")
    m.create_entry("/a", b"x")
    m.hardlink("/a", "/d/renamed")
    assert m.read_all("/d/renamed") == b"x"
    # listing shows the placement's effective name, not the node's
    assert {e.name for e in m.list("/d")} == {"renamed"}


def test_hardlink_refuses_containers_and_collisions(m):
    m.create_container("/d")
    m.create_entry("/a", b"x")
    with pytest.raises(errors.NotAnEntry):
        m.hardlink("/d", "/dlink")
    with pytest.raises(errors.AlreadyExists):
        m.hardlink("/a", "/d")  # name already taken


def test_rename_one_placement_leaves_the_other(m):
    m.create_container("/d")
    m.create_entry("/a", b"x")
    m.hardlink("/a", "/d/b")
    m.rename("/d/b", "c")
    assert m.read_all("/d/c") == b"x"
    assert m.read_all("/a") == b"x"  # the other placement kept its name


def test_prune_spares_a_node_with_a_live_placement(m):
    m.create_entry("/a", b"x")
    m.hardlink("/a", "/b")
    m.remove("/a")
    # the node has an archived edge AND a live one: not detached, not prunable
    fs_db = m._db
    detached = fs_db.connection.execute(
        "SELECT count(*) FROM detached_node"
    ).fetchone()[0]
    assert detached == 0


# -- special types (D-3) ----------------------------------------------------
def test_symlink_fifo_socket_types(m):
    m.create_special("/link", NodeType.SYMLINK, b"/a/b")
    m.create_special("/fifo", NodeType.FIFO)
    m.create_special("/sock", NodeType.SOCKET)
    assert m.stat("/link").type is NodeType.SYMLINK
    assert m.read_all("/link") == b"/a/b"  # the target rides in content
    assert m.stat("/fifo").type is NodeType.FIFO
    assert m.stat("/sock").type is NodeType.SOCKET


def test_create_special_refuses_ordinary_types(m):
    with pytest.raises(errors.Unsupported):
        m.create_special("/e", NodeType.ENTRY)


def test_copy_preserves_special_types(m):
    m.create_container("/src")
    m.create_special("/src/link", NodeType.SYMLINK, b"target")
    m.copy("/src", "/dst")
    assert m.stat("/dst/link").type is NodeType.SYMLINK
    assert m.read_all("/dst/link") == b"target"


# -- ownership (era-2 columns) ----------------------------------------------
def test_set_owner_and_ctime(m):
    m.create_entry("/f", b"x")
    before = m.stat("/f")
    m.set_owner("/f", uid=1000, gid=1000, mode=0o640)
    st = m.stat("/f")
    assert (st.uid, st.gid, st.mode) == (1000, 1000, 0o640)
    assert st.modified_at == before.modified_at  # chmod is not a content change
    assert st.ctime >= before.ctime
    m.set_owner("/f", mode=0o600)  # partial update leaves uid/gid alone
    st = m.stat("/f")
    assert (st.uid, st.gid, st.mode) == (1000, 1000, 0o600)


# -- mount policy (D-4) ------------------------------------------------------
@pytest.fixture
def fs():
    handle = Aloelite()
    yield handle
    handle.close()


def _vol(fs):
    return fs.create_volume("v", enc_mode="none").id


def test_ro_mount_refuses_every_mutation(fs):
    vol = _vol(fs)
    rw = fs.mount(vol)
    rw.create_container("/d")
    rw.create_entry("/d/f", b"x")
    ro = fs.mount(vol, access="ro")  # ro never conflicts with rw
    assert ro.read_all("/d/f") == b"x"  # reads flow
    assert [e.name for e in ro.list("/d")] == ["f"]
    for attempt in [
        lambda: ro.create_entry("/g", b"y"),
        lambda: ro.write_all("/d/f", b"y"),
        lambda: ro.remove("/d/f"),
        lambda: ro.rename("/d/f", "g"),
        lambda: ro.hardlink("/d/f", "/g"),
        lambda: ro.set_owner("/d/f", mode=0o600),
        lambda: ro.set_xattr("/d/f", "user.a", b"v"),
        lambda: ro.open_write("/d/f"),
    ]:
        with pytest.raises(errors.ReadOnlyMount):
            attempt()


def test_one_rw_mount_per_subtree_is_the_default(fs):
    vol = _vol(fs)
    fs.mount(vol)  # rw at the root
    with pytest.raises(errors.MountConflict):
        fs.mount(vol)  # same point
    m = fs.mount(vol, allow_overlap=True)  # explicit opt-in stacks
    m.unmount()


def test_rw_conflict_spans_ancestors_and_descendants(fs):
    vol = _vol(fs)
    boot = fs.mount(vol)
    boot.create_container("/tenants")
    boot.create_container("/tenants/alice")
    boot.unmount()

    tenant = fs.mount(vol, at="/tenants/alice")
    with pytest.raises(errors.MountConflict):
        fs.mount(vol)  # root would contain the tenant's rw mount
    with pytest.raises(errors.MountConflict):
        fs.mount(vol, at="/tenants/alice")  # same subtree
    # the admin-over-tenants deployment is the documented opt-in
    admin = fs.mount(vol, allow_overlap=True, principal="admin")
    assert admin.read_all is not None
    tenant.create_entry("/f", b"tenant writes fine")


def test_disjoint_subtrees_do_not_conflict(fs):
    vol = _vol(fs)
    boot = fs.mount(vol)
    boot.create_container("/a")
    boot.create_container("/b")
    boot.unmount()  # unmounted rows never conflict
    ma = fs.mount(vol, at="/a")
    mb = fs.mount(vol, at="/b")  # sibling subtree: no conflict
    ma.create_entry("/f", b"1")
    mb.create_entry("/f", b"2")


# -- xattrs ------------------------------------------------------------------
def test_xattr_roundtrip(m):
    m.create_entry("/f", b"x")
    m.set_xattr("/f", "user.a", b"\x00binary\xff")
    m.set_xattr("/f", "user.b", b"2")
    assert m.get_xattr("/f", "user.a") == b"\x00binary\xff"
    assert m.list_xattrs("/f") == ["user.a", "user.b"]
    m.set_xattr("/f", "user.a", b"replaced")
    assert m.get_xattr("/f", "user.a") == b"replaced"
    assert m.remove_xattr("/f", "user.a") is True
    assert m.remove_xattr("/f", "user.a") is False
    assert m.get_xattr("/f", "user.a") is None


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
