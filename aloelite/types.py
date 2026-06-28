# ./aloelite/types.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
Vocabulary: opaque id scalars and closed enums.

The Mount API is "flat functions over ids and plain records". The ids are
strings underneath (uuid7), but they are modeled as *distinct* NewTypes so the
type checker refuses to let a MountId stand in for a NodeId. They carry no
behavior and serialize as plain strings across the (future) FFI boundary.

This module is the Python projection of the `scalars` and `enums` sections of
mount-api.yaml. It must stay in lockstep with that file.
"""

from __future__ import annotations

from enum import Enum
from typing import NewType

# ---------------------------------------------------------------------------
# Opaque id scalars
#
# NewType gives a zero-cost distinct type for static checking while remaining a
# plain `str` at runtime (so it binds directly as a SQL parameter and serializes
# trivially). Construct with e.g. NodeId(row["node_id"]).
# ---------------------------------------------------------------------------
NodeId = NewType("NodeId", str)
EdgeId = NewType("EdgeId", str)
VolumeId = NewType("VolumeId", str)
MountId = NewType("MountId", str)
LockId = NewType("LockId", str)
FdId = NewType("FdId", str)

# A mount-relative, slash-separated path. Distinct from a NodeId so the
# path-first surface and the id-addressed (*_by_id) surface can't be confused.
Path = NewType("Path", str)

# Unix epoch milliseconds. Distinct from a bare int to keep timestamp arithmetic
# self-documenting; still an int at runtime.
Timestamp = NewType("Timestamp", int)


# ---------------------------------------------------------------------------
# Closed enums
#
# Values are the exact lowercase tokens stored in the database (node.type,
# mount.state, ...), so `NodeType("container")` round-trips a DB value and
# `.value` produces the DB token. Do NOT rename values without a schema change.
# ---------------------------------------------------------------------------
class NodeType(str, Enum):
    CONTAINER = "container"
    ENTRY = "entry"


class MountState(str, Enum):
    # 'new'       = mount row exists, still being constructed / not yet doing I/O
    # 'active'    = mount is in use
    # 'unmounted' = terminal; mount is invalid and its locks become prunable
    # ('new' and 'active' both count as valid; only 'unmounted' is not.)
    NEW = "new"
    ACTIVE = "active"
    UNMOUNTED = "unmounted"


class Whence(str, Enum):
    """Seek origin for a streaming descriptor."""

    SET = "set"
    CUR = "cur"
    END = "end"


class WriteMode(str, Enum):
    """Start position for open_write; an exclusive lock is taken either way."""

    TRUNCATE = "truncate"
    APPEND = "append"


class LockMode(str, Enum):
    """Only EXCLUSIVE in this iteration (ACC-7); reserved for future modes."""

    EXCLUSIVE = "exclusive"


__all__ = [
    "NodeId",
    "EdgeId",
    "VolumeId",
    "MountId",
    "LockId",
    "FdId",
    "Path",
    "Timestamp",
    "NodeType",
    "MountState",
    "Whence",
    "WriteMode",
    "LockMode",
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
