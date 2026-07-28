# ./tests/test_format_vectors.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
Python runner for conformance/vectors/format-v1.json.

The vectors pin the byte-level format: content addressing, chunking, and the
ENC-2 key ladder. crypto.py already says the Argon2id work factors are "part of
the format contract: every port must stretch the PIN with these exact
parameters" — this is what turns that comment into a failing test, here and in
every other implementation that reads the same file.

Each test re-derives from the vector's declared inputs and compares to its
declared outputs, which is exactly what a Rust or Kotlin runner does. Nothing
here reads the implementation's own constants except the one test that asserts
the constants themselves match.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aloelite import crypto
from aloelite.db import chunk_hash, split_chunks

_VECTORS = (
    Path(__file__).resolve().parent.parent
    / "conformance"
    / "vectors"
    / "format-v1.json"
)


@pytest.fixture(scope="module")
def vectors() -> dict:
    return json.loads(_VECTORS.read_text())


def _b(hexstr: str) -> bytes:
    return bytes.fromhex(hexstr)


def test_constants_match_the_format(vectors):
    """A port that stretches the PIN with different work factors derives a
    different K_u and cannot open the volume at all."""
    c = vectors["constants"]
    assert c["nonce_len"] == crypto.NONCE_LEN
    assert c["tag_len"] == crypto.TAG_LEN
    assert c["token_len"] == crypto.TOKEN_LEN
    assert c["mount_nonce_len"] == crypto.MOUNT_NONCE_LEN
    assert c["argon2_time_cost"] == crypto.ARGON2_TIME_COST
    assert c["argon2_memory_cost_kib"] == crypto.ARGON2_MEMORY_COST
    assert c["argon2_parallelism"] == crypto.ARGON2_PARALLELISM
    assert c["argon2_key_len"] == crypto.ARGON2_KEY_LEN


def test_chunk_addresses(vectors):
    """CV-2: the address folds the byte length in, so a short final chunk can
    never collide with a full chunk that shares its leading bytes."""
    for v in vectors["chunk_address"]:
        assert chunk_hash(_b(v["bytes"])) == v["chunk_hash"], v["name"]


def test_chunk_addresses_are_length_sensitive(vectors):
    """The property the length-fold exists to provide, asserted directly rather
    than assumed from the vectors."""
    assert chunk_hash(b"ab") != chunk_hash(b"ab\x00")
    seen = {v["chunk_hash"] for v in vectors["chunk_address"]}
    assert len(seen) == len(vectors["chunk_address"]), "vector payloads collided"


def test_chunk_splitting(vectors):
    """CV-1: uniform chunks with a possibly-short final one; empty stages none."""
    for v in vectors["chunk_split"]:
        got = [len(c) for c in split_chunks(b"z" * v["size"], v["chunk_size"])]
        assert got == v["lengths"], f"size={v['size']} cs={v['chunk_size']}"
        assert sum(got) == v["size"]


def test_volume_hash(vectors):
    for v in vectors["volume_hash"]:
        assert crypto.volume_hash(v["volume_id"], v["root_node_id"]).hex() == v["h_v"]


@pytest.mark.parametrize("index", [0, 1])
def test_unlock_key(vectors, index):
    """K_u = Argon2id(PIN, salt=H_v). Deliberately expensive, so kept to two."""
    v = vectors["unlock_key"][index]
    got = crypto.derive_unlock_key(v["pin_utf8"].encode(), _b(v["h_v"]))
    assert got.hex() == v["k_u"]


def test_chunk_subkey(vectors):
    """K_chunk = HKDF(K_v, info='aloelite-chunk:' || volume_id). Reaching the
    subkey directly is deliberate: if encrypt_chunk diverges in a port, this
    vector says whether the fault is the derivation or the AEAD."""
    for v in vectors["chunk_key"]:
        cipher = crypto.ChunkCipher(_b(v["volume_key"]), v["volume_id"])
        assert cipher._k.hex() == v["k_chunk"]


def test_convergent_encryption_is_byte_identical(vectors):
    """The dedup-critical vector. Convergent mode must produce the same nonce,
    ciphertext and tag everywhere, or the same plaintext lands at two pool
    addresses and cross-implementation dedup silently stops working."""
    inputs = vectors["inputs"]
    cipher = crypto.ChunkCipher(_b(inputs["volume_key"]), inputs["volume_id"])
    for v in vectors["encrypt_chunk_convergent"]:
        plaintext = _b(v["plaintext"])
        ct, nonce, tag = cipher.encrypt_chunk(plaintext)
        assert nonce.hex() == v["n_c"], f"{v['name']}: convergent nonce drifted"
        assert ct.hex() == v["ciphertext"], f"{v['name']}: ciphertext drifted"
        assert tag.hex() == v["tag"], f"{v['name']}: tag drifted"
        assert chunk_hash(ct) == v["chunk_hash"], f"{v['name']}: pool address drifted"
        assert cipher.decrypt_chunk(ct, nonce, tag) == plaintext


def test_convergent_encryption_is_stable_across_cipher_instances(vectors):
    """Dedup depends on determinism across sessions, not just within one."""
    inputs = vectors["inputs"]
    args = (_b(inputs["volume_key"]), inputs["volume_id"])
    first = crypto.ChunkCipher(*args).encrypt_chunk(b"same bytes")
    second = crypto.ChunkCipher(*args).encrypt_chunk(b"same bytes")
    assert first == second


def test_unwrap_volume_key(vectors):
    """The deterministic direction of the seal — wrap picks a random nonce, so
    the vector fixes a wrapped blob and asserts what opens out of it."""
    for v in vectors["unwrap_volume_key"]:
        got = crypto.unwrap_volume_key(
            _b(v["k_u"]), _b(v["wrapped_key"]), _b(v["wrap_nonce"])
        )
        assert got.hex() == v["volume_key"]


def test_unwrap_rejects_a_wrong_key(vectors):
    """This is the mechanism that rejects a wrong PIN: there is no alternate
    unlock path, so a bad K_u fails the AEAD tag."""
    from cryptography.exceptions import InvalidTag

    v = vectors["unwrap_volume_key"][0]
    wrong = bytes(32)
    with pytest.raises(InvalidTag):
        crypto.unwrap_volume_key(wrong, _b(v["wrapped_key"]), _b(v["wrap_nonce"]))


def test_session_kek(vectors):
    for v in vectors["session_kek"]:
        got = crypto.session_kek(_b(v["token"]), _b(v["mount_nonce"]))
        assert got.hex() == v["kek"]


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
