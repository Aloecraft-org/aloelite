#!/usr/bin/env python3
"""Regenerate conformance/vectors/pack-v1.json and pack-v2.json.

Run from the repo root:  python script/gen_pack_vectors.py

The pack blob (aloelite/pack.py) is the one cross-implementation BYTE contract
besides the chunk format. Two files pin it at the codec level, below the
database walk (whose canonical order the scenarios pin):

  pack-v2.json  the WRITER's contract: `encode` cases are node lists with the
                exact blob aloelite.pack.encode produces, byte for byte, and
                `decode` cases are raw blobs with the error the gate must
                answer or the nodes a tolerant read must produce.
  pack-v1.json  the READER's compatibility contract: `read` cases are v1
                blobs, built here by a frozen v1 encoder that no shipping
                writer has any more, with the nodes every reader must recover
                from them. v1 is readable forever (D-8).

Payload bytes appear as `d_hex`, xattr values as `xa_hex`. Regenerating should
be a no-op diff; a non-empty diff means the pack format moved — see
conformance/vectors/README.md and doc/DECISIONS.md D-8.
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import msgpack  # noqa: E402

from aloelite import pack  # noqa: E402

VECTORS = pathlib.Path(__file__).resolve().parent.parent / "conformance/vectors"

# Fixed era-2 nanosecond stamps: 2023-11-14T22:13:20Z and one second later.
C0 = 1_700_000_000_000_000_000
M0 = 1_700_000_001_000_000_000
FMT = pack.PACK_FMT
VER = pack.PACK_VER


def node(p: int, t: str, n: str, c: int = C0, m: int = M0, **opt) -> dict:
    """A node entry with its keys in the emission order pack.py documents.
    Optional keys (u, g, o, x, xa, rk, d) are emitted only when given; zero is
    a value, None is absence."""
    e: dict = {"p": p, "t": t, "n": n, "c": c, "m": m}
    for key in ("u", "g", "o"):
        if opt.get(key) is not None:
            e[key] = opt[key]
    if opt.get("x"):
        e["x"] = dict(sorted(opt["x"].items()))
    if opt.get("xa"):
        e["xa"] = dict(sorted(opt["xa"].items()))
    if opt.get("rk") is not None:
        e["rk"] = opt["rk"]
    if opt.get("d") is not None:
        e["d"] = opt["d"]
    return e


def view(nodes: list[dict]) -> list[dict]:
    """The JSON view of a node list: bytes as hex, None dropped."""
    out = []
    for e in nodes:
        v = {}
        for k in ("p", "t", "n", "c", "m", "u", "g", "o"):
            if e.get(k) is not None:
                v[k] = e[k]
        if e.get("x"):
            v["x"] = e["x"]
        if e.get("xa"):
            v["xa_hex"] = {k: val.hex() for k, val in e["xa"].items()}
        if e.get("rk") is not None:
            v["rk"] = e["rk"]
        if e.get("d") is not None:
            v["d_hex"] = e["d"].hex()
        out.append(v)
    return out


def mp(obj) -> bytes:
    return msgpack.packb(obj, use_bin_type=True)


def encode_v1(nodes: list[dict]) -> bytes:
    """The frozen v1 writer: identical to pack.encode before v2 existed."""
    return mp({"fmt": FMT, "ver": 1, "nodes": nodes})


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


def reference_tree_v2() -> list[dict]:
    """The same tree carrying what v2 added: ownership on the container and
    the entry, xattrs on the entry, a retention policy on the entry."""
    nodes = reference_tree()
    nodes[0] = node(-1, "container", "d", o=0o750, xa={"user.owner": b"root"})
    nodes[1] = node(
        0,
        "entry",
        "f",
        C0 + 1,
        M0 + 1,
        u=1000,
        g=1001,
        o=0o640,
        x={"status": "draft", "k": "v"},
        xa={"user.b": b"bee", "user.a": b"\x00\xff"},
        rk=3,
        d=b"hi",
    )
    return nodes


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


def integer_markers() -> list[dict]:
    return [
        node(-1, "container", "d", c=5, m=127),
        node(0, "entry", "f", c=2**31, m=2**32, d=b"x"),
        node(0, "entry", "g", c=2**63 - 1, m=255, d=b"y"),
    ]


UNICODE = [
    node(-1, "container", "répertoire"),
    node(
        0,
        "entry",
        "日本語.txt",
        x={"ключ": "значение", "emoji": "🙂"},
        d="ünïcödé".encode(),
    ),
]

# v1 blobs every reader must still recover. The first five are the writer
# cases as they were pinned when v1 was the current format.
READ_V1 = [
    ("container-alone", [node(-1, "container", "d")]),
    ("reference-tree", reference_tree()),
    ("unicode-names-and-metadata", UNICODE),
    ("marker-boundaries", marker_boundaries()),
    ("integer-markers", integer_markers()),
]
TOLERANT_V1 = [
    (
        "optional-fields-absent",
        mp({"fmt": FMT, "ver": 1, "nodes": [{"p": -1, "t": "entry", "n": "bare"}]}),
        [{"p": -1, "t": "entry", "n": "bare"}],
    ),
    (
        "null-timestamps",
        mp(
            {
                "fmt": FMT,
                "ver": 1,
                "nodes": [{"p": -1, "t": "container", "n": "d", "c": None, "m": None}],
            }
        ),
        [{"p": -1, "t": "container", "n": "d"}],
    ),
    (
        "unknown-keys-ignored",
        mp(
            {
                "fmt": FMT,
                "ver": 1,
                "nodes": [
                    {"p": -1, "t": "container", "n": "d", "c": C0, "m": M0, "zz": 9}
                ],
            }
        ),
        [{"p": -1, "t": "container", "n": "d", "c": C0, "m": M0}],
    ),
]

ENCODE_V2 = [
    ("container-alone", [node(-1, "container", "d")]),
    ("reference-tree", reference_tree()),
    ("reference-tree-with-ownership-xattrs-retention", reference_tree_v2()),
    (
        "zero-is-a-value",
        [node(-1, "container", "d", u=0, g=0, o=0), node(0, "entry", "f", rk=0, d=b"")],
    ),
    ("unicode-names-and-metadata", UNICODE),
    ("marker-boundaries", marker_boundaries()),
    ("integer-markers", integer_markers()),
    (
        "mode-markers",
        [
            node(-1, "container", "d", o=0o777),
            node(0, "entry", "f", o=0o7777, u=65534, g=2**32 - 2, d=b"z"),
        ],
    ),
]

DECODE_V2 = [
    (
        "newer-version",
        mp({"fmt": FMT, "ver": VER + 1, "nodes": []}),
        {"error": "unsupported"},
    ),
    ("versionless", mp({"fmt": FMT, "nodes": []}), {"error": "corrupt"}),
    ("wrong-fmt", mp({"fmt": "nope", "ver": VER, "nodes": []}), {"error": "corrupt"}),
    ("ver-as-string", mp({"fmt": FMT, "ver": "2", "nodes": []}), {"error": "corrupt"}),
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
    (
        "payload-as-string",
        mp(
            {
                "fmt": FMT,
                "ver": VER,
                "nodes": [{"p": -1, "t": "entry", "n": "f", "d": "text"}],
            }
        ),
        {"error": "corrupt"},
    ),
    (
        "mode-as-string",
        mp(
            {
                "fmt": FMT,
                "ver": VER,
                "nodes": [{"p": -1, "t": "entry", "n": "f", "o": "644"}],
            }
        ),
        {"error": "corrupt"},
    ),
    (
        "xattr-value-as-string",
        mp(
            {
                "fmt": FMT,
                "ver": VER,
                "nodes": [{"p": -1, "t": "entry", "n": "f", "xa": {"user.a": "text"}}],
            }
        ),
        {"error": "corrupt"},
    ),
    ("truncated", pack.encode(reference_tree_v2())[:-7], {"error": "corrupt"}),
    ("garbage", b"not msgpack at all", {"error": "corrupt"}),
    ("empty", b"", {"error": "corrupt"}),
    (
        "v1-blob-reads-under-v2",
        encode_v1(reference_tree()),
        {"nodes": view(reference_tree())},
    ),
    (
        "optional-fields-absent",
        mp({"fmt": FMT, "ver": VER, "nodes": [{"p": -1, "t": "entry", "n": "bare"}]}),
        {"nodes": [{"p": -1, "t": "entry", "n": "bare"}]},
    ),
    (
        "null-optionals",
        mp(
            {
                "fmt": FMT,
                "ver": VER,
                "nodes": [
                    {
                        "p": -1,
                        "t": "container",
                        "n": "d",
                        "c": None,
                        "m": None,
                        "u": None,
                        "o": None,
                        "rk": None,
                    }
                ],
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


def write(path: pathlib.Path, doc: dict) -> None:
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {path.relative_to(pathlib.Path.cwd())}")


def main() -> None:
    VECTORS.mkdir(parents=True, exist_ok=True)
    write(
        VECTORS / "pack-v1.json",
        {
            "format": "aloelite-pack-vectors",
            "version": 1,
            "note": (
                "generated by script/gen_pack_vectors.py; see README.md. "
                "v1 blobs are read, never written, since format v2."
            ),
            "pack_fmt": FMT,
            "pack_ver": 1,
            "read": [
                {"name": name, "blob": encode_v1(nodes).hex(), "nodes": view(nodes)}
                for name, nodes in READ_V1
            ]
            + [
                {"name": name, "blob": blob.hex(), "nodes": nodes}
                for name, blob, nodes in TOLERANT_V1
            ],
        },
    )
    write(
        VECTORS / "pack-v2.json",
        {
            "format": "aloelite-pack-vectors",
            "version": 1,
            "note": "generated by script/gen_pack_vectors.py; see README.md",
            "pack_fmt": FMT,
            "pack_ver": VER,
            "encode": [
                {"name": name, "nodes": view(nodes), "blob": pack.encode(nodes).hex()}
                for name, nodes in ENCODE_V2
            ],
            "decode": [
                {"name": name, "blob": blob.hex(), **expect}
                for name, blob, expect in DECODE_V2
            ],
        },
    )


if __name__ == "__main__":
    main()
