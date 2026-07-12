# ./aloelite/__init__.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
aloelite fs: Python reference implementation of the SQLite-backed Mount API.

This is the oracle: the implementation the conformance suite is generated from
and the other three (Rust, JS/WASM, Kotlin) are tested against. It drives the
shared SQL templates (sql-templates.yaml) rather than hand-written SQL, so it
stays a true reference for the template-driven implementations.

Layering, bottom to top:
    schema.sql                      (the SQL floor)
    db.Db / Db.txn                  (connection, templates, transaction boundary)
    resolve.resolve / resolve_parent (path -> id, the most-reused logic)
    [function layer]                (flat Mount API / next session)
    types / errors / models         (vocabulary, used throughout)
"""

from . import errors, operations
from .db import Db, Templates
from .descriptor import Descriptor
from .path import AloelitePath
from .models import (
    Anomaly,
    ContentPruneReport,
    DirEntry,
    LockInfo,
    MountInfo,
    NodeInfo,
    PruneReport,
    VolumeInfo,
)
from .resolve import Parent, Resolved, resolve, resolve_parent, split_path
from .types import (
    EdgeId,
    FdId,
    LockId,
    LockMode,
    MountId,
    MountState,
    NodeId,
    NodeType,
    Path,
    Timestamp,
    VolumeId,
    Whence,
    WriteMode,
)

__all__ = [
    # scaffolding
    "Db",
    "Templates",
    # resolve
    "resolve",
    "resolve_parent",
    "split_path",
    "Resolved",
    "Parent",
    # models
    "VolumeInfo",
    "NodeInfo",
    "DirEntry",
    "MountInfo",
    "LockInfo",
    "Anomaly",
    "PruneReport",
    "ContentPruneReport",
    # types
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
    # errors module
    "errors",
    "operations",
    "Descriptor",
    "AloelitePath",
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
