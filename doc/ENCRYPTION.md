# Encryption Specification

<div align="center">

<img src="aloelite.png" style="height:96px; width:96px;"/>

**Aloelite SQLite Filesystem**
</div>

### Quick Links 

- [Overview](/README.md)
- [Getting Started](/doc/GETTING_STARTED.md)
- [Frequently Asked Questions](/doc/FAQ.md)
- [Troubleshooting](/doc/TROUBLESHOOTING.md)
- [Requirements Spec](/doc/REQUIREMENTS.md)
- **Encryption Spec (This Document)**

(see `doc/` for more)

## Lexicon

**Operations**

Provision:
P(K, H_v, S_v) = S_vk

Authorize:
A(K, S_vk) = (T, N_m)

Unlock:
U(T, N_m, S_vk) = S_v

Encrypt:
E(S_v, S_c, C) = xC

Decrypt:
D(S_v, S_c, xC) = C

**Stored Data**

Volume:
- H_v
- S_vk

Chunk:
- xC
- N_c

**Runtime Data**

User
- T

Client
- N_m

## At-Rest Format

**Goal:** At-rest dump reveals nothing without the key.

### Schema Additions
- `volume.enc_mode` TEXT NOT NULL DEFAULT 'convergent' (reserves {none, convergent, random})
- `volume.N_wrap` BLOB (stored wrap nonce for the wrapped volume key)
- `mount.N_m` BLOB NOT NULL (ephemeral mount nonce, auto-generated, 16 bytes)
- `content_chunk.N_c` BLOB NOT NULL (convergent salt, 16 bytes, derived from plaintext)
- `content_chunk.enc_tag` BLOB NOT NULL (ChaCha20-Poly1305 tag, 16 bytes)

## Key Hierarchy

### 1. PIN Derivation (At Mount)
```
H_v = SHA256(volume_id || root_node_id)  # derived, NOT stored
K_u = Argon2id(PIN, H_v, time=3, memory=65536, parallelism=4, len=32)
```
- `H_v` is deterministic so same PIN always yields same `K_u` for a volume
- `K_u` exists only during mount, then discarded

### 2. Volume Key Wrapping (At Provision)
```
K_v = random(32)  # volume key, generated once, immutable
S_vk = ChaCha20Poly1305.encrypt(
  key=K_u,
  nonce=N_wrap (stored),
  plaintext=K_v
)  # stored on volume row; tag forecloses wrong PINs
```

### 3. Session Mount (Per Mount, At Open)
```
T = random(16)  # token, user-held, runtime-only
N_m = random(16)  # mount nonce, host-held, stored on mount row, dies with mount
session_kek = HKDF(T, "session" || N_m)  # HKDF not Argon2 (T is already random)
mount_secret = ChaCha20Poly1305.encrypt(
  key=session_kek,
  nonce=N_session (random, stored with mount_secret),
  plaintext=K_u
)  # memory-only, not persisted; losing it requires a fresh mount
```

### 4. Per-Operation Key Recovery
```
session_kek = HKDF(T, "session" || N_m)
K_u = ChaCha20Poly1305.decrypt(session_kek, mount_secret)
K_v = ChaCha20Poly1305.decrypt(K_u, S_vk)
```

### 5. Chunk Encryption
```
K_chunk = HKDF(K_v, "aloelite-chunk" || volume_id)
N_c = SHA256(len || plaintext)[:16]  # convergent: same plaintext -> same N_c
(xC, tag) = ChaCha20Poly1305.encrypt(K_chunk, N_c, plaintext_chunk)
# Store: chunk_hash, N_c, enc_tag, data (xC || enc_tag or separate columns)
```

**Convergent salt choice:** identical plaintext encrypts identically, so dedup survives. Trade-off: an attacker with a dump can see repeated blocks and confirm known files. Reserve `enc_mode` for a future `random` nonce option if a workload needs zero equality leakage.

## Threat Model & Guarantees

| Attacker | Defense | Outcome |
|----------|---------|---------|
| Dump of volume file alone | Ciphertext everywhere | Completely blocked |
| Single memory snapshot during mount | N_m + mount_secret separated; K_v assembled only for chunk ops | High probability of missing all keys |
| Continuous in-context observer (process heap, repeated sampling) | Out of scope for userspace library | Recommend isolation: Worker/extension/server-side boundary |
| PIN forged | Poly1305 tag on wrapped K_u | Tag mismatch rejects unwrap |

## PIN Rotation vs Key Rotation

- **PIN change:** Re-derive K_u from new PIN, re-wrap S_vk under new K_u. Chunks untouched (still encrypted under K_v).
- **Volume key rotation (deferred):** K_v change requires re-encrypting all chunks. Not yet implemented; reserved for v2.

## Implementation Notes

1. **Crypto primitives:** ChaCha20-Poly1305 (preferred) or AES-GCM (both acceptable); Argon2id for PIN; HKDF for session/chunk key derivation. Both AES and ChaCha20 are quantum-safe for symmetric (not threatened in the way RSA is).

2. **Token scope:** `T` is opaque to the client, bound to a single mount. For stronger isolation (browser), deploy as server-side library (recaptcha-token pattern): host holds `N_m` + `mount_secret`, client holds only `T` across a process/origin boundary. Format unchanged; runtime policy only.

3. **Secret-sharing (deferred, v1+ optimization):** For Rust core, assemble `K_v = share_host ⊕ share_user` per-operation and zeroize immediately. Share neither with untrusted JS. Does NOT change on-disk bytes, only key lifetime in memory.

4. **Dedup and equality:** Convergent `N_c` (default, `enc_mode='convergent'`) keeps dedup; identical plaintext → identical ciphertext → observable equality in a dump. Alternative (future) `enc_mode='random'` sacrifices dedup for zero leakage if a threat model requires it. Choice is per-volume, reserved in schema.

## Ports

All four languages (Python, Rust, JS/WASM, others) must implement the same key hierarchy and convergent-salt logic so volumes are transferable and tests are identical. Python oracle is the reference; deviations need explicit justification.
