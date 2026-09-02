# ./tests/test_pack_vectors.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
Python runner for conformance/vectors/pack-v1.json and pack-v2.json.

The pack blob is a cross-implementation byte contract (aloelite/pack.py): a
subtree packed on one platform must unpack on every other. pack-v2.json pins
the writer — encode is byte-exact, decode refuses what the gate says to and
tolerates what the format says to — and pack-v1.json pins that v1 blobs, which
no shipping writer produces any more, still read (D-8: v1 is readable
forever). The Rust runner (rust/aloelite-conformance/tests/pack_vectors.rs)
reads the same files. The walk order that feeds the codec is pinned by the
scenarios.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aloelite import errors, pack

_VECTORS = Path(__file__).resolve().parent.parent / "conformance" / "vectors"
_V1 = json.loads((_VECTORS / "pack-v1.json").read_text())
_V2 = json.loads((_VECTORS / "pack-v2.json").read_text())


def _cases(doc: dict, section: str, tag: str) -> list:
    return [pytest.param(c, id=f"{tag}:{c['name']}") for c in doc.get(section, [])]


_READ = _cases(_V1, "read", "v1")
_ENCODE = _cases(_V2, "encode", "v2")
_DECODE = _cases(_V2, "decode", "v2")


def _nodes(entries: list[dict]) -> list[dict]:
    """The JSON view -> codec input: *_hex become bytes."""
    out = []
    for e in entries:
        n = {k: e[k] for k in ("p", "t", "n", "c", "m", "u", "g", "o", "x") if k in e}
        if "xa_hex" in e:
            n["xa"] = {k: bytes.fromhex(v) for k, v in e["xa_hex"].items()}
        if "rk" in e:
            n["rk"] = e["rk"]
        if "d_hex" in e:
            n["d"] = bytes.fromhex(e["d_hex"])
        out.append(n)
    return out


def _view(nodes: list[dict]) -> list[dict]:
    """Decoded nodes -> the JSON view, so both sides compare as plain data."""
    out = []
    for e in nodes:
        v = {
            k: e[k]
            for k in ("p", "t", "n", "c", "m", "u", "g", "o")
            if e.get(k) is not None
        }
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


def test_constants_match_the_format():
    assert _V2["pack_fmt"] == pack.PACK_FMT == _V1["pack_fmt"]
    assert _V2["pack_ver"] == pack.PACK_VER
    assert _V1["pack_ver"] == 1


@pytest.mark.parametrize("case", _READ)
def test_v1_blobs_still_read(case):
    assert _view(pack.decode(bytes.fromhex(case["blob"]))) == case["nodes"]


@pytest.mark.parametrize("case", _ENCODE)
def test_encode_is_byte_exact(case):
    assert pack.encode(_nodes(case["nodes"])).hex() == case["blob"]


@pytest.mark.parametrize("case", _ENCODE)
def test_encoded_blobs_decode_back_to_their_nodes(case):
    assert _view(pack.decode(bytes.fromhex(case["blob"]))) == case["nodes"]


@pytest.mark.parametrize("case", _DECODE)
def test_decode_refuses_and_tolerates_as_the_reference_does(case):
    blob = bytes.fromhex(case["blob"])
    if "error" in case:
        with pytest.raises(errors.FsError) as caught:
            pack.decode(blob)
        assert caught.value.code == case["error"]
    else:
        assert _view(pack.decode(blob)) == case["nodes"]


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
