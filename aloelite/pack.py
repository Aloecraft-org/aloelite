# ./aloelite/pack.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
The pack blob codec — a CROSS-IMPLEMENTATION CONTRACT (OP-6/OP-7, TX-2).

A packed subtree is one MsgPack map:

    { "fmt": "aloefs.pack", "ver": 1, "nodes": [ <node>, ... ] }

`nodes` is in TOP-DOWN canonical order (the `subtree` view: depth, then
edge_id, then node_id), one entry per PLACEMENT, parents before children so a
single forward pass restores it. Each node is a map whose keys a writer emits
in exactly this order, omitting the optional ones when absent:

    p   parent's index in `nodes`, or -1 for the root
    t   node type: container | entry | symlink | fifo | socket
    n   the EFFECTIVE name at this placement (coalesce(edge.name, node.name))
    c   created_at, ns
    m   modified_at, ns
    x   NODE-6 metadata, {string: string}, keys sorted; only when non-empty
    d   payload bytes; leaves only (a symlink's target, empty for fifo/socket)

Byte rules, so four implementations produce identical blobs: MsgPack with
strings as str and payloads as bin (msgpack-python's `use_bin_type=True`),
integers in their smallest encoding, no absent keys. Both encoders in the tree
today follow them by construction; conformance/vectors/pack-v1.json pins them.

VERSIONING. `ver` is a gate, not decoration: a blob written by a newer build
is refused (`unsupported`) rather than read with this build's field set, which
would silently drop whatever the new version added — the same posture db.py
takes toward a newer schema era. A malformed or absent version is `corrupt`.
Older versions stay readable: every field added after v1 is optional on read.
The v2 scope is doc/DECISIONS.md D-8.
"""

from __future__ import annotations

from typing import Any

import msgpack

from .errors import Corrupt, Unsupported

PACK_FMT = "aloefs.pack"
PACK_VER = 1

# The keys a v1 node entry may carry, in emission order.
NODE_KEYS = ("p", "t", "n", "c", "m", "x", "d")


def encode(nodes: list[dict[str, Any]]) -> bytes:
    """Serialize `nodes` (already in canonical order, keys in NODE_KEYS
    order) as a v1 pack blob."""
    return msgpack.packb(
        {"fmt": PACK_FMT, "ver": PACK_VER, "nodes": nodes}, use_bin_type=True
    )


def decode(blob: bytes, **context: object) -> list[dict[str, Any]]:
    """Validate a pack blob and return its node list.

    Raises Corrupt for anything that is not a well-formed pack of a known
    shape, and Unsupported for a pack written by a newer build. `context` is
    attached to the error (the node being unpacked, typically).
    """
    try:
        doc = msgpack.unpackb(blob, raw=False)
    except Exception:
        raise Corrupt("not an aloefs pack blob", **context) from None
    if not isinstance(doc, dict) or doc.get("fmt") != PACK_FMT:
        raise Corrupt("not an aloefs pack blob", **context)
    ver = doc.get("ver")
    if not isinstance(ver, int) or isinstance(ver, bool) or ver < 1:
        raise Corrupt(f"pack blob has no usable version ({ver!r})", **context)
    if ver > PACK_VER:
        raise Unsupported(
            f"pack was written by a newer aloelite (pack format v{ver}; "
            f"this build understands v{PACK_VER}). Upgrade aloelite to "
            f"unpack it.",
            **context,
        )
    nodes = doc.get("nodes")
    if not isinstance(nodes, list):
        raise Corrupt("pack blob has no node list", **context)
    for i, pn in enumerate(nodes):
        if not _well_formed(pn):
            raise Corrupt(f"pack node {i} is malformed", **context)
    return nodes


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _well_formed(pn: object) -> bool:
    """The v1 node shape: p/t/n required, c/m optional ints, x an optional
    map, d optional bytes. Unknown keys are ignored (forward-tolerant)."""
    if not isinstance(pn, dict):
        return False
    if not (_is_int(pn.get("p")) and isinstance(pn.get("t"), str)):
        return False
    if not isinstance(pn.get("n"), str):
        return False
    for key in ("c", "m"):
        if pn.get(key) is not None and not _is_int(pn.get(key)):
            return False
    if pn.get("x") is not None and not isinstance(pn.get("x"), dict):
        return False
    return pn.get("d") is None or isinstance(pn.get("d"), bytes)


__all__ = ["PACK_FMT", "PACK_VER", "NODE_KEYS", "encode", "decode"]
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
