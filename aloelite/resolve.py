# ./aloelite/resolve.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
Path resolution — the first thing every implementation writes, and the thing
the whole flat layer is built on.

resolve() walks a whole path in ONE query via the `resolution.resolve_path`
recursive CTE, starting at the mount's mount point. resolve_parent() is the same
walk stopped one segment short, returning (container_id, final_name) for the
create / move / rename operations that need "the parent container, keep the
final name".

This used to fold `resolution.resolve_segment` per segment: a ten-deep path cost
ten round trips. Free on a local file, but path resolution is woven through every
path-addressed operation, so over a network connection to a remote backend the
per-segment fold is the difference between usable and unusable. The CTE resolves
any depth in one round trip and speeds up local operation too.

Path semantics are decided HERE, once, so all four implementations can mirror
exactly one set of rules:
  * paths are mount-relative; '' and '/' both denote the mount point itself
  * leading/trailing slashes are ignored; empty segments (//) collapse
  * resolution sees only VISIBLE nodes (NODE-5) — the greatest-uuid7 sibling
    wins — so hidden same-name siblings are unreachable by path. That is the
    contract; *_by_id variants exist for those.
  * a miss at any segment raises NotFound
  * a non-final segment that resolves to an entry (not a container) raises
    NotAContainer — you cannot descend through a file
  * '.' and '..' are ORDINARY NAMES, not navigation. They are never interpreted,
    so a path can never climb above the mount point it started at — which is
    what confines a subtree mount to its subtree. See
    tests/test_resolve_containment.py; that containment is a security boundary
    and must survive any future POSIX work.
"""

from __future__ import annotations

from typing import NamedTuple

from .db import Db
from .errors import NotAContainer, NotFound
from .types import NodeId, NodeType, Path

_WALK = "resolution.resolve_path"


class Resolved(NamedTuple):
    node: NodeId
    type: NodeType


def split_path(path: Path | str) -> list[str]:
    """Normalize a mount-relative path into clean segments.

    '' and '/' -> []. Collapses repeated/trailing slashes. No '.'/'..' handling:
    they are treated as ordinary names and will simply NotFound. That is not an
    omission to fix later — it is what keeps resolution anchored at the mount
    point (see the module docstring).
    """
    return [seg for seg in str(path).split("/") if seg]


def _walk(db: Db, root: NodeId, segments: list[str]) -> Resolved:
    """Resolve non-empty `segments` beneath `root` in one query.

    Raises the same errors, at the same segment, as the per-segment fold did:
    the CTE records where it stopped so the last row carries the diagnosis.
    """
    rows = db.all(_WALK, {"root": root, "path": "/".join(segments)})
    last = rows[-1]  # the walk always consumes at least one segment
    if last["node_id"] is None:
        raise NotFound(
            f"no visible child {last['seg']!r}",
            container=NodeId(last["parent_id"]),
            name=last["seg"],
        )
    if last["idx"] < len(segments):
        # stopped early: only a non-container can halt the walk mid-path
        raise NotAContainer(
            f"path segment {last['seg']!r} is not a container",
            node=NodeId(last["node_id"]),
        )
    return Resolved(NodeId(last["node_id"]), NodeType(last["node_type"]))


def resolve(db: Db, mount_point: NodeId, path: Path | str) -> Resolved:
    """Resolve a full mount-relative path to its node.

    `mount_point` is the resolved anchor for this mount (caller supplies it,
    typically MountInfo.mount_point). Returns the node and its type so callers
    can enforce container/entry expectations without a second lookup.
    """
    segments = split_path(path)
    if not segments:
        # '' or '/' is the mount point itself; report its type for symmetry.
        row = db.one("resolution.get_node", {"node": mount_point})
        if row is None:
            raise NotFound("mount point does not exist", node=mount_point)
        return Resolved(NodeId(mount_point), NodeType(row["type"]))
    return _walk(db, mount_point, segments)


class Parent(NamedTuple):
    container: NodeId
    name: str


def resolve_parent(db: Db, mount_point: NodeId, path: Path | str) -> Parent:
    """Resolve a path to (its parent container, its final name).

    The substrate for create_container / create_entry / move-target / rename.
    Resolving the parent walks all-but-last segments and requires the parent to
    be a container; the final name is NOT looked up (it may not exist yet).
    """
    segments = split_path(path)
    if not segments:
        raise NotFound("cannot take the parent of the mount point root")

    *head, final = segments
    if not head:
        return Parent(mount_point, final)  # parent is the mount point itself

    found = _walk(db, mount_point, head)
    if found.type is not NodeType.CONTAINER:
        # the deepest head segment resolved to an entry; _walk only raises
        # NotAContainer for segments it had to descend THROUGH
        raise NotAContainer(
            f"path segment {head[-1]!r} is not a container", node=found.node
        )
    return Parent(found.node, final)


__all__ = ["Resolved", "Parent", "split_path", "resolve", "resolve_parent"]
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
