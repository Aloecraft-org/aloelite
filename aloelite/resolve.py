# ./aloelite/resolve.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
Path resolution — the first thing every implementation writes, and the thing
the whole flat layer is built on.

resolve() folds the `resolution.resolve_segment` template over path segments,
starting at the mount's mount point. resolve_parent() is the same fold stopped
one segment short, returning (container_id, final_name) for the create / move /
rename operations that need "the parent container, keep the final name".

Path semantics are decided HERE, once, so all four implementations can mirror
exactly one set of rules:
  * paths are mount-relative; '' and '/' both denote the mount point itself
  * leading/trailing slashes are ignored; empty segments (//) collapse
  * resolution sees only VISIBLE nodes (NODE-5) — resolve_segment returns the
    greatest-uuid7 winner — so hidden same-name siblings are unreachable by
    path. That is the contract; *_by_id variants exist for those.
  * a miss at any segment raises NotFound
  * a non-final segment that resolves to an entry (not a container) raises
    NotAContainer — you cannot descend through a file
"""

from __future__ import annotations

from typing import NamedTuple

from .db import Db
from .errors import NotAContainer, NotFound
from .types import NodeId, NodeType, Path

_SEGMENT = "resolution.resolve_segment"


class Resolved(NamedTuple):
    node: NodeId
    type: NodeType


def split_path(path: Path | str) -> list[str]:
    """Normalize a mount-relative path into clean segments.

    '' and '/' -> []. Collapses repeated/trailing slashes. No '.'/'..' handling
    in this iteration (the namespace has no notion of them yet); if they appear
    they are treated as ordinary names and will simply NotFound.
    """
    return [seg for seg in str(path).split("/") if seg]


def _step(db: Db, container: NodeId, name: str) -> Resolved:
    row = db.one(_SEGMENT, {"container": container, "name": name})
    if row is None:
        raise NotFound(f"no visible child {name!r}", container=container, name=name)
    return Resolved(NodeId(row["node_id"]), NodeType(row["type"]))


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

    current = mount_point
    last = len(segments) - 1
    for i, name in enumerate(segments):
        found = _step(db, current, name)
        if i != last and found.type is not NodeType.CONTAINER:
            raise NotAContainer(
                f"path segment {name!r} is not a container", node=found.node
            )
        current = found.node
    return found  # type: ignore[possibly-undefined]  # segments non-empty => bound


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
    container = mount_point
    for name in head:
        found = _step(db, container, name)
        if found.type is not NodeType.CONTAINER:
            raise NotAContainer(
                f"path segment {name!r} is not a container", node=found.node
            )
        container = found.node
    return Parent(container, final)


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
