# ./aloelite/models.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
Record models (Pydantic v2).

The plain-data return shapes from the `records` section of mount-api.yaml. These
are what the flat function layer returns; across the future FFI boundary they
project to each language's idiomatic struct. They are deliberately dumb: no
behavior, no DB access, just typed, validated data.

Construction convention: every model has a `from_row` classmethod that takes a
sqlite3.Row (or any mapping) and applies the small DB->model conventions in one
place — SQLite has no bool (0/1 ints) and emits enum tokens as plain strings, so
those coercions live here rather than scattered through the function layer.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Optional

from pydantic import BaseModel, ConfigDict

from .types import (
    EdgeId,
    LockId,
    MountId,
    NodeId,
    NodeType,
    Timestamp,
    VolumeId,
    MountState,
)


def _b(value: Any) -> bool:
    """SQLite stores booleans as 0/1 integers; normalize to bool."""
    return bool(value)


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class VolumeInfo(_Record):
    id: VolumeId
    name: str | None
    root: NodeId | None
    api_version: int
    created_at: Timestamp

    @classmethod
    def from_row(cls, r: Mapping[str, Any]) -> "VolumeInfo":
        return cls(
            id=VolumeId(r["volume_id"]),
            name=r["name"],
            root=NodeId(r["root_node_id"]) if r["root_node_id"] is not None else None,
            api_version=r["api_version"],
            created_at=Timestamp(r["created_at"]),
        )


class NodeInfo(_Record):
    id: NodeId
    type: NodeType
    name: str
    created_at: Timestamp
    # own content/metadata change, NOT placement (a move does not bump it)
    modified_at: Timestamp
    volume: VolumeId | None
    size: Optional[int]
    # NODE-6: shallow {string:string} annotation map; {} when unset
    metadata: dict[str, str]

    @classmethod
    def from_row(cls, r: Mapping[str, Any]) -> "NodeInfo":
        # get_node already coalesces modified_at -> created_at in SQL, but guard
        # here too for rows that come from elsewhere.
        modified = r["modified_at"] if r["modified_at"] is not None else r["created_at"]
        # get_node returns metadata as a JSON string via json(); NULL => empty map.
        raw_meta = r["metadata"]
        metadata = json.loads(raw_meta) if raw_meta is not None else {}
        return cls(
            id=NodeId(r["node_id"]),
            type=NodeType(r["type"]),
            name=r["name"],
            created_at=Timestamp(r["created_at"]),
            modified_at=Timestamp(modified),
            volume=VolumeId(r["volume_id"]) if r["volume_id"] is not None else None,
            size=r["size"],
            metadata=metadata,
        )


class DirEntry(_Record):
    node: NodeId
    name: str
    type: NodeType
    visible: bool
    edge: EdgeId

    @classmethod
    def from_row(cls, r: Mapping[str, Any]) -> "DirEntry":
        return cls(
            node=NodeId(r["node_id"]),
            name=r["name"],
            type=NodeType(r["type"]),
            visible=_b(r["visible"]),
            edge=EdgeId(r["edge_id"]),
        )


class MountInfo(_Record):
    id: MountId
    volume: VolumeId
    mount_point: NodeId
    # the mount point's volume-absolute path, recomputed on read (may be None
    # until path_of has been resolved by the caller)
    mount_path: str | None
    state: MountState
    expires_at: Timestamp | None
    created_at: Timestamp

    @classmethod
    def from_row(
        cls, r: Mapping[str, Any], mount_path: str | None = None
    ) -> "MountInfo":
        return cls(
            id=MountId(r["mount_id"]),
            volume=VolumeId(r["volume_id"]),
            mount_point=NodeId(r["mount_point"]),
            mount_path=mount_path,
            state=MountState(r["state"]),
            expires_at=Timestamp(r["expires_at"])
            if r["expires_at"] is not None
            else None,
            created_at=Timestamp(r["created_at"]),
        )


class LockInfo(_Record):
    id: LockId
    mount: MountId
    node: NodeId
    expires_at: Timestamp | None

    @classmethod
    def from_row(cls, r: Mapping[str, Any]) -> "LockInfo":
        return cls(
            id=LockId(r["lock_id"]),
            mount=MountId(r["mount_id"]),
            node=NodeId(r["node_id"]),
            expires_at=Timestamp(r["expires_at"])
            if r["expires_at"] is not None
            else None,
        )


class Anomaly(_Record):
    kind: str
    # Deliberately a bare str, NOT a typed Id: health_anomaly emits the id of
    # whatever kind of thing the anomaly is about (a node, edge, or volume id
    # depending on `kind`), so it is heterogeneous by design.
    id: str

    @classmethod
    def from_row(cls, r: Mapping[str, Any]) -> "Anomaly":
        return cls(kind=r["kind"], id=r["id"])


class PruneReport(_Record):
    nodes_pruned: int
    locks_pruned: int


class ContentPruneReport(_Record):
    # CV-7: result of prune_content — superseded/aborted manifest versions
    # dropped, and pool chunks reclaimed because no retained version referenced
    # them.
    versions_pruned: int
    chunks_pruned: int


__all__ = [
    "VolumeInfo",
    "NodeInfo",
    "DirEntry",
    "MountInfo",
    "LockInfo",
    "Anomaly",
    "PruneReport",
    "ContentPruneReport",
]
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
