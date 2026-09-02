# ./tests/test_pack_vectors.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
Python runner for conformance/vectors/pack-v1.json.

The pack blob is a cross-implementation byte contract (aloelite/pack.py): a
subtree packed on one platform must unpack on every other. These vectors pin
the codec below the database walk — encode is byte-exact, decode is tolerant
where the format says so and refuses what the gate says to refuse — and the
Rust runner (rust/aloelite-conformance/tests/pack_vectors.rs) reads the same
file. The walk order that feeds the codec is pinned by the scenarios.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aloelite import errors, pack

_VECTORS = (
    Path(__file__).resolve().parent.parent / "conformance" / "vectors" / "pack-v1.json"
)
_DOC = json.loads(_VECTORS.read_text())
_ENCODE = [pytest.param(c, id=c["name"]) for c in _DOC["encode"]]
_DECODE = [pytest.param(c, id=c["name"]) for c in _DOC["decode"]]


def _nodes(entries: list[dict]) -> list[dict]:
    """The JSON view -> codec input: d_hex becomes bytes under d."""
    out = []
    for e in entries:
        n = {k: e[k] for k in ("p", "t", "n", "c", "m", "x") if k in e}
        if "d_hex" in e:
            n["d"] = bytes.fromhex(e["d_hex"])
        out.append(n)
    return out


def _view(nodes: list[dict]) -> list[dict]:
    """Decoded nodes -> the JSON view, so both sides compare as plain data."""
    out = []
    for e in nodes:
        v = {k: e[k] for k in ("p", "t", "n", "c", "m") if e.get(k) is not None}
        if e.get("x"):
            v["x"] = e["x"]
        if e.get("d") is not None:
            v["d_hex"] = e["d"].hex()
        out.append(v)
    return out


def test_constants_match_the_format():
    assert _DOC["pack_fmt"] == pack.PACK_FMT
    assert _DOC["pack_ver"] == pack.PACK_VER


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
