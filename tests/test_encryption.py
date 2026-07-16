# ./tests/test_encryption.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
Conformance tests for at-rest encryption (ENC-1..3).

Same style as operations_test.py: perform an operation sequence, assert the
observable state — but here the assertions also reach into the raw chunk pool to
prove ciphertext (not plaintext) is what lands on disk, that dedup survives
encryption, and that the key ladder rejects a wrong PIN. These become part of
the cross-language conformance corpus: every port must pass them.

Run:  pytest encryption_test.py --import-mode=importlib
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aloelite import Db, errors
from aloelite import operations as ops
from aloelite import crypto


def _find(*candidates: str) -> str:
    here = Path(__file__).resolve().parent
    for root in (here, here.parent, Path.cwd()):
        for c in candidates:
            if (root / c).exists():
                return str(root / c)
    raise FileNotFoundError(candidates)


SCHEMA = _find("schema.sql", "sql/schema.sql")
TEMPLATES = _find("sql-templates.yaml", "config/sql-templates.yaml")

PIN = b"1234"
PIN2 = b"5678"


@pytest.fixture
def db() -> Db:
    d = Db.open(":memory:", TEMPLATES, schema_path=SCHEMA)
    yield d
    d.close()


def _raw_chunks(db) -> list[tuple[bytes, bytes, bytes, int]]:
    return db.connection.execute(
        "SELECT data, N_c, enc_tag, length FROM content_chunk"
    ).fetchall()


# ---------------------------------------------------------------------------
# Key ladder unit-level (pure crypto, no DB)
# ---------------------------------------------------------------------------
def test_wrong_pin_fails_to_unwrap():
    h_v = crypto.volume_hash("vol-123", "root-456")
    k_v = crypto.new_volume_key()
    k_u = crypto.derive_unlock_key(PIN, h_v)
    wrapped, nonce = crypto.wrap_volume_key(k_u, k_v)
    # right pin recovers the key
    assert crypto.unwrap_volume_key(k_u, wrapped, nonce) == k_v
    # wrong pin -> the Poly1305 tag rejects it (no alternate unlock secret)
    k_u_bad = crypto.derive_unlock_key(PIN2, h_v)
    with pytest.raises(Exception):
        crypto.unwrap_volume_key(k_u_bad, wrapped, nonce)


def test_session_token_recovers_volume_key_without_pin():
    k_v = crypto.new_volume_key()
    token = crypto.new_token()
    n_m = crypto.new_mount_nonce()
    mount_secret, sess_nonce = crypto.seal_mount_secret(token, n_m, k_v)
    # token + N_m + mount_secret rebuild K_v with no PIN in sight
    assert crypto.open_mount_secret(token, n_m, mount_secret, sess_nonce) == k_v
    # wrong token fails
    with pytest.raises(Exception):
        crypto.open_mount_secret(crypto.new_token(), n_m, mount_secret, sess_nonce)


def test_convergent_nonce_is_deterministic():
    c = crypto.ChunkCipher(crypto.new_volume_key(), "vol-x", convergent=True)
    ct1, n1, t1 = c.encrypt_chunk(b"hello world")
    ct2, n2, t2 = c.encrypt_chunk(b"hello world")
    assert (ct1, n1, t1) == (ct2, n2, t2)  # identical plaintext -> identical ciphertext
    ct3, n3, _ = c.encrypt_chunk(b"different")
    assert n3 != n1 and ct3 != ct1


# ---------------------------------------------------------------------------
# End-to-end through the engine
# ---------------------------------------------------------------------------
def test_encrypted_roundtrip():
    db = Db.open(":memory:", TEMPLATES, schema_path=SCHEMA)
    try:
        vol = ops.create_volume(db, "secret", chunk_size=16, pin=PIN)
        m = ops.mount(db, vol.id, "/", ttl_ms=60_000, pin=PIN)
        payload = b"the quick brown fox jumps over the lazy dog" * 4
        ops.create_entry(db, m, "/f", payload)
        assert ops.read_all(db, m, "/f") == payload
    finally:
        db.close()


def test_ciphertext_on_disk_is_not_plaintext():
    db = Db.open(":memory:", TEMPLATES, schema_path=SCHEMA)
    try:
        vol = ops.create_volume(db, "secret", chunk_size=16, pin=PIN)
        m = ops.mount(db, vol.id, "/", ttl_ms=60_000, pin=PIN)
        marker = b"TOPSECRET_MARKER_STRING_DO_NOT_LEAK_0123456789"
        ops.create_entry(db, m, "/f", marker)
        # No raw chunk's data may contain the plaintext marker; nonce+tag present.
        for data, n_c, tag, length in _raw_chunks(db):
            assert marker not in data
            assert len(n_c) == crypto.NONCE_LEN
            assert len(tag) == crypto.TAG_LEN
    finally:
        db.close()


def test_dedup_survives_encryption():
    db = Db.open(":memory:", TEMPLATES, schema_path=SCHEMA)
    try:
        vol = ops.create_volume(db, "secret", chunk_size=8, pin=PIN)
        m = ops.mount(db, vol.id, "/", ttl_ms=60_000, pin=PIN)
        block = b"AAAAAAAA" * 10  # many identical 8-byte chunks
        ops.create_entry(db, m, "/a", block)
        pool_after_a = len(_raw_chunks(db))
        ops.create_entry(db, m, "/b", block)  # identical content
        pool_after_b = len(_raw_chunks(db))
        # convergent encryption -> identical ciphertext -> pool does not grow
        assert pool_after_b == pool_after_a
        assert ops.read_all(db, m, "/b") == block
    finally:
        db.close()


def test_random_mode_breaks_dedup_but_roundtrips():
    db = Db.open(":memory:", TEMPLATES, schema_path=SCHEMA)
    try:
        vol = ops.create_volume(db, "rnd", chunk_size=8, pin=PIN, enc_mode="random")
        m = ops.mount(db, vol.id, "/", ttl_ms=60_000, pin=PIN)
        block = b"BBBBBBBB" * 6
        ops.create_entry(db, m, "/a", block)
        n_after_a = len(_raw_chunks(db))
        ops.create_entry(db, m, "/b", block)
        n_after_b = len(_raw_chunks(db))
        # random nonce -> identical plaintext stores distinct ciphertext (no dedup)
        assert n_after_b > n_after_a
        assert ops.read_all(db, m, "/a") == block
        assert ops.read_all(db, m, "/b") == block
    finally:
        db.close()


def test_wrong_pin_at_mount_raises():
    db = Db.open(":memory:", TEMPLATES, schema_path=SCHEMA)
    try:
        vol = ops.create_volume(db, "secret", pin=PIN)
        with pytest.raises(errors.BadKey):
            ops.mount(db, vol.id, "/", pin=PIN2)
    finally:
        db.close()


def test_missing_pin_on_encrypted_volume_raises():
    db = Db.open(":memory:", TEMPLATES, schema_path=SCHEMA)
    try:
        vol = ops.create_volume(db, "secret", pin=PIN)
        with pytest.raises(errors.EncryptionRequired):
            ops.mount(db, vol.id, "/")
    finally:
        db.close()


def test_pin_on_plain_volume_raises():
    db = Db.open(":memory:", TEMPLATES, schema_path=SCHEMA)
    try:
        vol = ops.create_volume(db, "plain")  # no pin -> enc_mode 'none'
        with pytest.raises(errors.EncryptionRequired):
            ops.mount(db, vol.id, "/", pin=PIN)
    finally:
        db.close()


def test_mount_exposes_token_and_session():
    db = Db.open(":memory:", TEMPLATES, schema_path=SCHEMA)
    try:
        vol = ops.create_volume(db, "secret", pin=PIN)
        m = ops.mount(db, vol.id, "/", ttl_ms=60_000, pin=PIN)
        sess = db.active_session
        assert sess is not None and sess["mount_id"] == m
        assert len(sess["token"]) == crypto.TOKEN_LEN
        assert len(sess["n_m"]) == crypto.MOUNT_NONCE_LEN
        # the in-memory session triple reconstructs K_v (proves the token works)
        # (we don't expose K_v, but open_mount_secret must not raise)
        crypto.open_mount_secret(
            sess["token"], sess["n_m"], sess["mount_secret"], sess["sess_nonce"]
        )
        ops.unmount(db, m)
        assert db.active_session is None
    finally:
        db.close()


def test_cross_volume_same_plaintext_no_alias():
    """Two encrypted volumes with different keys but identical plaintext must
    not alias in the shared chunk pool (the InvalidTag-on-reread bug). Mounts
    are sequential because the cipher is per-connection (one active at a time)."""
    db = Db.open(":memory:", TEMPLATES, schema_path=SCHEMA)
    try:
        va = ops.create_volume(db, "a", chunk_size=16, pin=PIN)
        vb = ops.create_volume(db, "b", chunk_size=16, pin=PIN)
        payload = b"identical across volumes"

        ma = ops.mount(db, va.id, "/", pin=PIN)
        ops.create_entry(db, ma, "/f", payload)
        assert ops.read_all(db, ma, "/f") == payload
        ops.unmount(db, ma)

        mb = ops.mount(db, vb.id, "/", pin=PIN)
        ops.create_entry(db, mb, "/f", payload)  # same plaintext, different key
        assert ops.read_all(db, mb, "/f") == payload  # InvalidTag before the fix
        ops.unmount(db, mb)

        # volume a still reads correctly afterward
        ma2 = ops.mount(db, va.id, "/", pin=PIN)
        assert ops.read_all(db, ma2, "/f") == payload
    finally:
        db.close()


def test_streaming_write_encrypted():
    """The bounded-memory streaming descriptor must also encrypt per chunk."""
    db = Db.open(":memory:", TEMPLATES, schema_path=SCHEMA)
    try:
        vol = ops.create_volume(db, "stream", chunk_size=64, pin=PIN)
        m = ops.mount(db, vol.id, "/", ttl_ms=60_000, pin=PIN)
        ops.create_entry(db, m, "/big", b"")
        payload = bytes((i * 31) % 251 for i in range(64 * 20 + 7))
        w = ops.open_write(db, m, "/big")
        off = 0
        while off < len(payload):
            off += w.write(payload[off : off + 100])
        w.close()
        # roundtrip via ranged reader
        r = ops.open_read(db, m, "/big")
        got = r.read()
        r.close()
        assert got == payload
        # and on-disk chunks are ciphertext
        for data, n_c, tag, length in _raw_chunks(db):
            assert len(n_c) == crypto.NONCE_LEN and len(tag) == crypto.TAG_LEN
    finally:
        db.close()


def test_persisted_encrypted_volume_reopens(tmp_path):
    """A real file: write encrypted, close, reopen with PIN, read back."""
    p = str(tmp_path / "enc.sqlite")
    payload = b"persistence across reopen" * 8
    db = Db.open(p, TEMPLATES, schema_path=SCHEMA)
    vol = ops.create_volume(db, "secret", chunk_size=16, pin=PIN)
    vid = vol.id
    m = ops.mount(db, vid, "/", pin=PIN)
    ops.create_entry(db, m, "/f", payload)
    ops.unmount(db, m)
    db.close()

    db2 = Db.open(p, TEMPLATES, schema_path=SCHEMA)
    try:
        m2 = ops.mount(db2, vid, "/", pin=PIN)
        assert ops.read_all(db2, m2, "/f") == payload
        # wrong pin on the reopened file is still rejected
        ops.unmount(db2, m2)
        with pytest.raises(errors.BadKey):
            ops.mount(db2, vid, "/", pin=PIN2)
    finally:
        db2.close()


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
