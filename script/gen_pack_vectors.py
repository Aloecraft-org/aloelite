#!/usr/bin/env python3
"""Regenerate conformance/vectors/pack-v1.json from the reference implementation.

Run from the repo root:  python script/gen_pack_vectors.py

The pack blob (aloelite/pack.py) is the one cross-implementation BYTE contract
besides the chunk format: a subtree packed on one platform must unpack on
every other, and identical trees must pack to identical bytes. This file pins
both directions at the codec level, below the database walk (whose canonical
order the scenarios pin), so the timestamps a real pack takes from its nodes
can be fixed here.

Every `encode` blob is produced by aloelite.pack.encode; every `decode` blob is
raw msgpack built here to exercise the gate and the tolerance rules. Payload
bytes appear as `d_hex` in the JSON view of a node. Regenerating should be a
no-op diff; a non-empty diff means the pack format moved — see
conformance/vectors/README.md and doc/DECISIONS.md D-8.
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import msgpack  # noqa: E402

from aloelite import pack  # noqa: E402

OUT = (
    pathlib.Path(__file__).resolve().parent.parent / "conformance/vectors/pack-v1.json"
)

# Fixed era-2 nanosecond stamps: 2023-11-14T22:13:20Z and one second later.
C0 = 1_700_000_000_000_000_000
M0 = 1_700_000_001_000_000_000


def node(p: int, t: str, n: str, c: int = C0, m: int = M0, x=None, d=None) -> dict:
    """A node entry with its keys in the emission order pack.py documents."""
    e: dict = {"p": p, "t": t, "n": n, "c": c, "m": m}
    if x:
        e["x"] = dict(sorted(x.items()))
    if d is not None:
        e["d"] = d
    return e


def view(nodes: list[dict]) -> list[dict]:
    """The JSON view of a node list: bytes as hex under d_hex, None dropped."""
    out = []
    for e in nodes:
        v = {k: e[k] for k in ("p", "t", "n", "c", "m") if e.get(k) is not None}
        if e.get("x"):
            v["x"] = e["x"]
        if e.get("d") is not None:
            v["d_hex"] = e["d"].hex()
        out.append(v)
    return out


def mp(obj) -> bytes:
    return msgpack.packb(obj, use_bin_type=True)


def reference_tree() -> list[dict]:
    """Every node type, metadata, an empty payload, and a symlink target:
    the tree conformance/scenarios/coherence.yaml restores end to end."""
    return [
        node(-1, "container", "d"),
        node(0, "entry", "f", C0 + 1, M0 + 1, x={"status": "draft", "k": "v"}, d=b"hi"),
        node(0, "container", "sub", C0 + 2, M0 + 2),
        node(2, "entry", "empty", C0 + 3, M0 + 3, d=b""),
        node(0, "symlink", "link", C0 + 4, M0 + 4, d=b"../target"),
        node(0, "fifo", "pipe", C0 + 5, M0 + 5, d=b""),
        node(0, "socket", "sock", C0 + 6, M0 + 6, d=b""),
    ]


def marker_boundaries() -> list[dict]:
    """Names, payloads, maps and the node list itself at the MsgPack marker
    boundaries: fixstr/str8 at 32, bin8/bin16/bin32 at 256 and 65536,
    fixmap/map16 at 16 keys, fixarray/array16 at 16 entries."""
    pattern = bytes(range(256))
    nodes = [
        node(-1, "container", "d"),
        node(0, "entry", "a" * 31, d=b"31"),
        node(0, "entry", "b" * 32, d=b"32"),
        node(0, "entry", "bin8", d=pattern[:255]),
        node(0, "entry", "bin16", d=pattern),
        node(0, "entry", "bin32", d=pattern * 256),
        node(0, "container", "map16", x={f"k{i:02d}": f"v{i}" for i in range(16)}),
    ]
    while len(nodes) < 17:  # 17 entries: the list itself needs array16
        nodes.append(node(0, "container", f"filler{len(nodes)}"))
    return nodes


ENCODE = [
    ("container-alone", [node(-1, "container", "d")]),
    ("reference-tree", reference_tree()),
    (
        "unicode-names-and-metadata",
        [
            node(-1, "container", "répertoire"),
            node(
                0,
                "entry",
                "日本語.txt",
                x={"ключ": "значение", "emoji": "🙂"},
                d="ünïcödé".encode(),
            ),
        ],
    ),
    ("marker-boundaries", marker_boundaries()),
    (
        "integer-markers",
        [
            node(-1, "container", "d", c=5, m=127),
            node(0, "entry", "f", c=2**31, m=2**32, d=b"x"),
            node(0, "entry", "g", c=2**63 - 1, m=255, d=b"y"),
        ],
    ),
]

FMT, VER = pack.PACK_FMT, pack.PACK_VER
DECODE = [
    (
        "newer-version",
        mp({"fmt": FMT, "ver": VER + 1, "nodes": []}),
        {"error": "unsupported"},
    ),
    ("versionless", mp({"fmt": FMT, "nodes": []}), {"error": "corrupt"}),
    ("wrong-fmt", mp({"fmt": "nope", "ver": VER, "nodes": []}), {"error": "corrupt"}),
    ("ver-as-string", mp({"fmt": FMT, "ver": "1", "nodes": []}), {"error": "corrupt"}),
    ("ver-as-bool", mp({"fmt": FMT, "ver": True, "nodes": []}), {"error": "corrupt"}),
    ("ver-zero", mp({"fmt": FMT, "ver": 0, "nodes": []}), {"error": "corrupt"}),
    ("top-level-array", mp([FMT, VER, []]), {"error": "corrupt"}),
    ("no-node-list", mp({"fmt": FMT, "ver": VER}), {"error": "corrupt"}),
    (
        "node-without-type",
        mp({"fmt": FMT, "ver": VER, "nodes": [{"p": -1, "n": "d"}]}),
        {"error": "corrupt"},
    ),
    (
        "node-with-string-parent",
        mp({"fmt": FMT, "ver": VER, "nodes": [{"p": "0", "t": "entry", "n": "f"}]}),
        {"error": "corrupt"},
    ),
    ("truncated", pack.encode(reference_tree())[:-7], {"error": "corrupt"}),
    ("garbage", b"not msgpack at all", {"error": "corrupt"}),
    ("empty", b"", {"error": "corrupt"}),
    (
        "optional-fields-absent",
        mp({"fmt": FMT, "ver": VER, "nodes": [{"p": -1, "t": "entry", "n": "bare"}]}),
        {"nodes": [{"p": -1, "t": "entry", "n": "bare"}]},
    ),
    (
        "null-timestamps",
        mp(
            {
                "fmt": FMT,
                "ver": VER,
                "nodes": [{"p": -1, "t": "container", "n": "d", "c": None, "m": None}],
            }
        ),
        {"nodes": [{"p": -1, "t": "container", "n": "d"}]},
    ),
    (
        "unknown-keys-ignored",
        mp(
            {
                "fmt": FMT,
                "ver": VER,
                "nodes": [
                    {"p": -1, "t": "container", "n": "d", "c": C0, "m": M0, "zz": 9}
                ],
            }
        ),
        {"nodes": [{"p": -1, "t": "container", "n": "d", "c": C0, "m": M0}]},
    ),
]


def main() -> None:
    doc = {
        "format": "aloelite-pack-vectors",
        "version": 1,
        "note": "generated by script/gen_pack_vectors.py; see README.md",
        "pack_fmt": FMT,
        "pack_ver": VER,
        "encode": [
            {"name": name, "nodes": view(nodes), "blob": pack.encode(nodes).hex()}
            for name, nodes in ENCODE
        ],
        "decode": [
            {"name": name, "blob": blob.hex(), **expect}
            for name, blob, expect in DECODE
        ],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {OUT.relative_to(pathlib.Path.cwd())}")


if __name__ == "__main__":
    main()
