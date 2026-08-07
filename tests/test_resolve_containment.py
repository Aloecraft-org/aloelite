# ./tests/test_resolve_containment.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
Path resolution cannot climb above the node it starts from.

This is a SECURITY boundary, not a curiosity. Subtree mounts already exist —
ops.mount(volume, at=...) anchors at any mount_point, which is the primitive
behind admin-sees-home-directories and behind any multitenant policy layered on
top. All of that rests on one property: a path handed to a mount can only ever
name something at or below that mount's mount_point.

Today the property holds by construction rather than by enforcement. resolve.py
never interprets '.' or '..' — split_path() drops empty segments and passes
everything else through as an ordinary name — so '..' is looked up as a child
literally named '..' and simply misses. There is no upward edge to follow
because there is no code that would follow one.

That is a fine implementation, but "safe because nobody wrote the unsafe code
yet" is exactly the kind of guarantee that evaporates silently. The POSIX work
in a later era is going to put '..' handling on the table, and the natural way
to add it — normalize the path before resolving — turns every one of these
NotFounds into a real escape. So these tests pin the behavior rather than the
implementation: they assert containment, and they will fail loudly the day
somebody teaches the resolver to navigate.

Run:  pytest tests/test_resolve_containment.py
"""

from __future__ import annotations

import pytest

from aloelite import errors
from aloelite.aloelite import Aloelite
from aloelite.resolve import split_path

# Every spelling of "climb out" we can think of. None of these may resolve.
ESCAPES = [
    "..",
    "../secret.txt",
    "../../secret.txt",
    "../../../../../../../../secret.txt",
    "/../secret.txt",
    "/../../secret.txt",
    "sub/../../secret.txt",
    "./../secret.txt",
    "././../secret.txt",
    "..//secret.txt",
    "inner/../../secret.txt",
]

# The escapes that actually exercise resolve_parent's walk. A bare '..' has no
# parent walk at all — resolve_parent short-circuits to (mount_point, '..') and
# creating it makes an ordinary child named '..' INSIDE the subtree, which is
# containment working, not an escape. That case is covered on its own by
# test_dotdot_is_an_ordinary_name_not_navigation.
ESCAPES_WITH_PARENT = [p for p in ESCAPES if len(split_path(p)) > 1]


@pytest.fixture
def fs():
    """A volume with a secret at the top and a subtree to mount underneath."""
    with Aloelite(":memory:") as handle:
        vol = handle.create_volume("t")
        with handle.mount(vol.id) as m:
            m.put("/secret.txt", b"above the subtree mount")
            m.mkdir("/home")
            m.mkdir("/home/alice")
            m.put("/home/alice/inner.txt", b"alice's own file")
            m.mkdir("/home/bob")
            m.put("/home/bob/bob.txt", b"bob's own file")
        yield handle, vol


@pytest.fixture
def alice(fs):
    """A subtree mount anchored at /home/alice — the tenant's view."""
    handle, vol = fs
    with handle.mount(vol.id, at="/home/alice") as m:
        yield m


def test_split_path_never_interprets_dot_or_dotdot():
    """The unit-level statement of the whole property."""
    assert split_path("../a") == ["..", "a"]
    assert split_path("./a") == [".", "a"]
    assert split_path("a/../b") == ["a", "..", "b"]
    # only empties collapse — '.' and '..' survive as ordinary names
    assert split_path("//a///b//") == ["a", "b"]
    assert split_path("") == [] and split_path("/") == []


def test_subtree_mount_sees_only_its_subtree(alice):
    assert [e.name for e in alice.list("/")] == ["inner.txt"]
    assert alice.read_all("/inner.txt") == b"alice's own file"


@pytest.mark.parametrize("path", ESCAPES)
def test_dotdot_cannot_escape_a_subtree_mount(alice, path):
    """The core assertion: no spelling of '..' reaches above the mount point."""
    with pytest.raises(errors.NotFound):
        alice.stat(path)


@pytest.mark.parametrize("path", ESCAPES_WITH_PARENT)
def test_dotdot_cannot_escape_on_the_write_paths(alice, path):
    """Containment is not a read-only property.

    resolve_parent() is a separate walk from resolve(), and it is the one the
    create/move/rename operations use. An escape there would let a tenant write
    outside its subtree, which is strictly worse than reading outside it.
    """
    with pytest.raises(errors.NotFound):
        alice.put(path, b"written outside the subtree")
    with pytest.raises(errors.NotFound):
        alice.mkdir(path)


def test_sibling_tenants_are_mutually_unreachable(fs):
    """alice must not be able to name bob's subtree at all."""
    handle, vol = fs
    with handle.mount(vol.id, at="/home/alice") as a:
        for path in ("../bob/bob.txt", "../bob", "../../home/bob/bob.txt"):
            with pytest.raises(errors.NotFound):
                a.stat(path)


def test_dotdot_is_an_ordinary_name_not_navigation(fs):
    """The flip side: '..' is creatable, and creating it changes nothing.

    A container literally named '..' must not become a way up. This is what
    distinguishes "we treat '..' as a name" from "we happen to reject '..'" —
    and it is why a future normalization pass would be a behavior change, not a
    refinement.
    """
    handle, vol = fs
    with handle.mount(vol.id, at="/home/alice") as a:
        a.mkdir("..")  # a real child named '..' — created INSIDE alice
        a.put("../trap.txt", b"still inside alice")
        # it resolved to the child, not to /home
        assert sorted(e.name for e in a.list("/")) == ["..", "inner.txt"]
        assert a.read_all("../trap.txt") == b"still inside alice"
        # and it still cannot reach the secret above
        with pytest.raises(errors.NotFound):
            a.stat("../../secret.txt")
    # nothing leaked into the parent container
    with handle.mount(vol.id, at="/home") as h:
        assert sorted(e.name for e in h.list("/")) == ["alice", "bob"]


def test_containment_holds_at_the_volume_root_too(fs):
    """A full-volume mount cannot climb above the volume root either."""
    handle, vol = fs
    with handle.mount(vol.id) as m:
        assert m.read_all("/secret.txt") == b"above the subtree mount"
        for path in ("..", "../secret.txt", "../../secret.txt"):
            with pytest.raises(errors.NotFound):
                m.stat(path)


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
