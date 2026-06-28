# ./aloelite/crypto.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
At-rest encryption: the key ladder and the chunk-cipher seam.

This module owns ALL cryptographic decisions. The rest of the engine touches
encryption only through `db.cipher` — an object with two methods,
`encrypt_chunk` / `decrypt_chunk` — so operations.py and descriptor.py stay
byte-for-byte oblivious to whether a volume is encrypted. The default cipher is
the identity (no-op), so an unencrypted volume runs the exact same code path and
the whole conformance suite is unaffected.

KEY LADDER (see ENCRYPTION_SPEC.md). One user secret, layered so the volume key
is the fixed point that survives PIN rotation:

    H_v   = SHA256(volume_id || root_node_id)         # derived, never stored
    K_u   = Argon2id(PIN, salt=H_v)                    # unlock secret; mount-only
    K_v   = random(32)                                 # volume key; immutable
    S_vk  = AEAD(K_u, N_wrap, K_v)                     # wrapped_key on disk; the
                                                       #   Poly1305 tag rejects a
                                                       #   wrong PIN with no
                                                       #   alternate unlock secret

Per mount, a token stands in for the PIN so K_u is needed only once:

    T            = random(16)                          # token; user-held, runtime
    N_m          = random(16)                          # mount nonce; stored on the
                                                       #   mount row, dies w/ mount
    session_kek  = HKDF(T, salt=N_m, info="session")
    mount_secret = AEAD(session_kek, N_sess, K_v)      # memory-only; T+N_m+this
                                                       #   recover K_v without PIN

Chunks are encrypted under a domain-separated subkey, with a convergent nonce so
identical plaintext re-encrypts identically and the content-addressed dedup pool
keeps working:

    K_chunk = HKDF(K_v, info="aloelite-chunk" || volume_id)
    N_c     = SHA256(b"aloelite-nc" || len(pt) || pt)[:12]   # convergent
    (xC, tag) = ChaCha20Poly1305(K_chunk).encrypt(N_c, pt)

chunk_hash stays computed over PLAINTEXT, so equal plaintext dedups; convergent
N_c makes the ciphertext equal too, so the pool actually collapses duplicates.
"""

from __future__ import annotations

import hashlib
import os
from typing import Protocol

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# ChaCha20-Poly1305 (IETF) uses a 12-byte nonce and a 16-byte tag.
NONCE_LEN = 12
TAG_LEN = 16
TOKEN_LEN = 16
MOUNT_NONCE_LEN = 16

# Argon2id work factors. These are part of the format contract: every port must
# stretch the PIN with these exact parameters or it derives a different K_u.
ARGON2_TIME_COST = 3
ARGON2_MEMORY_COST = 65536  # KiB
ARGON2_PARALLELISM = 4
ARGON2_KEY_LEN = 32


# ---------------------------------------------------------------------------
# The seam: every chunk crosses into/out of SQLite through one of these.
# ---------------------------------------------------------------------------
class Cipher(Protocol):
    # Whether identical plaintext addresses to the same pool row (content
    # dedup). True for identity and convergent; False for random, where the
    # per-chunk random nonce is folded into the address so equal plaintext
    # stores distinctly (no dedup, no equality leakage).
    dedup: bool

    def encrypt_chunk(self, plaintext: bytes) -> tuple[bytes, bytes, bytes]:
        """Return (ciphertext, nonce, tag). Length-preserving ciphertext so the
        stored `length` (plaintext length) still governs offset math."""
        ...

    def decrypt_chunk(self, ciphertext: bytes, nonce: bytes, tag: bytes) -> bytes: ...


class IdentityCipher:
    """No-op cipher for an unencrypted ('none') volume. Stores empty nonce/tag
    blobs (the columns are NOT NULL but an empty BLOB satisfies that)."""

    dedup = True

    def encrypt_chunk(self, plaintext: bytes) -> tuple[bytes, bytes, bytes]:
        return plaintext, b"", b""

    def decrypt_chunk(self, ciphertext: bytes, nonce: bytes, tag: bytes) -> bytes:
        return ciphertext


class ChunkCipher:
    """ChaCha20-Poly1305 over a domain-separated per-volume subkey, with a
    convergent nonce (enc_mode='convergent') or a random nonce
    (enc_mode='random', dedup sacrificed for zero equality leakage)."""

    def __init__(self, volume_key: bytes, volume_id: str, *, convergent: bool = True):
        self._k = _hkdf(volume_key, info=b"aloelite-chunk:" + volume_id.encode())
        self._aead = ChaCha20Poly1305(self._k)
        self._convergent = convergent
        self.dedup = convergent

    def encrypt_chunk(self, plaintext: bytes) -> tuple[bytes, bytes, bytes]:
        if self._convergent:
            h = hashlib.sha256()
            h.update(b"aloelite-nc")
            h.update(len(plaintext).to_bytes(8, "big"))
            h.update(plaintext)
            nonce = h.digest()[:NONCE_LEN]
        else:
            nonce = os.urandom(NONCE_LEN)
        sealed = self._aead.encrypt(nonce, plaintext, None)
        ciphertext, tag = sealed[:-TAG_LEN], sealed[-TAG_LEN:]
        return ciphertext, nonce, tag

    def decrypt_chunk(self, ciphertext: bytes, nonce: bytes, tag: bytes) -> bytes:
        return self._aead.decrypt(nonce, ciphertext + tag, None)


# ---------------------------------------------------------------------------
# Key-ladder primitives (pure; no DB). Ports reproduce these exactly.
# ---------------------------------------------------------------------------
def volume_hash(volume_id: str, root_node_id: str) -> bytes:
    """H_v = SHA256(volume_id || root_node_id). The Argon2id salt; derived on
    mount from two immutable fields, never stored."""
    return hashlib.sha256(volume_id.encode() + root_node_id.encode()).digest()


def derive_unlock_key(pin: bytes, h_v: bytes) -> bytes:
    """K_u = Argon2id(PIN, salt=H_v) with the fixed format work factors."""
    kdf = Argon2id(
        salt=h_v,
        length=ARGON2_KEY_LEN,
        iterations=ARGON2_TIME_COST,
        lanes=ARGON2_PARALLELISM,
        memory_cost=ARGON2_MEMORY_COST,
    )
    return kdf.derive(pin)


def _aead_wrap(key: bytes, plaintext: bytes) -> tuple[bytes, bytes]:
    """Seal `plaintext` under `key`. Returns (wrapped = ct||tag, nonce)."""
    nonce = os.urandom(NONCE_LEN)
    sealed = ChaCha20Poly1305(key).encrypt(nonce, plaintext, None)
    return sealed, nonce


def _aead_unwrap(key: bytes, wrapped: bytes, nonce: bytes) -> bytes:
    """Open a seal. Raises cryptography.exceptions.InvalidTag on a wrong key —
    this is what rejects a wrong PIN (no alternate unlock secret is reachable)."""
    return ChaCha20Poly1305(key).decrypt(nonce, wrapped, None)


def wrap_volume_key(unlock_key: bytes, volume_key: bytes) -> tuple[bytes, bytes]:
    """S_vk = AEAD(K_u, N_wrap, K_v) -> (wrapped_key, wrap_nonce)."""
    return _aead_wrap(unlock_key, volume_key)


def unwrap_volume_key(
    unlock_key: bytes, wrapped_key: bytes, wrap_nonce: bytes
) -> bytes:
    """K_v = open(K_u, S_vk). InvalidTag => wrong PIN."""
    return _aead_unwrap(unlock_key, wrapped_key, wrap_nonce)


def new_volume_key() -> bytes:
    return os.urandom(32)


# -- session / token layer ---------------------------------------------------
def new_token() -> bytes:
    return os.urandom(TOKEN_LEN)


def new_mount_nonce() -> bytes:
    return os.urandom(MOUNT_NONCE_LEN)


def session_kek(token: bytes, mount_nonce: bytes) -> bytes:
    """HKDF(T, salt=N_m, info='session'). HKDF (not Argon2) because T is already
    high-entropy — there is nothing to stretch."""
    return _hkdf(token, salt=mount_nonce, info=b"aloelite-session")


def seal_mount_secret(
    token: bytes, mount_nonce: bytes, volume_key: bytes
) -> tuple[bytes, bytes]:
    """mount_secret = AEAD(session_kek(T, N_m), N_sess, K_v). Memory-only; T +
    N_m + mount_secret reconstruct K_v without the PIN."""
    return _aead_wrap(session_kek(token, mount_nonce), volume_key)


def open_mount_secret(
    token: bytes, mount_nonce: bytes, mount_secret: bytes, sess_nonce: bytes
) -> bytes:
    """K_v from the session triple. InvalidTag => wrong token."""
    return _aead_unwrap(session_kek(token, mount_nonce), mount_secret, sess_nonce)


def _hkdf(key: bytes, *, salt: bytes | None = None, info: bytes = b"") -> bytes:
    return HKDF(algorithm=SHA256(), length=32, salt=salt, info=info).derive(key)


__all__ = [
    "Cipher",
    "IdentityCipher",
    "ChunkCipher",
    "volume_hash",
    "derive_unlock_key",
    "wrap_volume_key",
    "unwrap_volume_key",
    "new_volume_key",
    "new_token",
    "new_mount_nonce",
    "session_kek",
    "seal_mount_secret",
    "open_mount_secret",
    "NONCE_LEN",
    "TAG_LEN",
    "TOKEN_LEN",
    "MOUNT_NONCE_LEN",
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
