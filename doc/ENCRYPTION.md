# Encryption Specification

<div align="center">

<img src="https://raw.githubusercontent.com/Aloecraft-org/aloelite/refs/heads/main/doc/aloelite.png" style="height:96px; width:96px;"/>

**Aloelite SQLite Filesystem**

[Overview](/README.md) | [Getting Started](/doc/GETTING_STARTED.md) |  [Frequently Asked Questions](/doc/FAQ.md) 

[Troubleshooting](/doc/TROUBLESHOOTING.md) | [Requirements Spec](/doc/REQUIREMENTS.md) | **Encryption Spec (This Document)** | [Roadmap](/doc/ROADMAP.md)
</div>

> **OPEN DECISION — `mount_secret` seals K_u or K_v?**
>
> This is the one place where the document and the implementation still
> disagree, and it is a design question rather than a typo:
>
> - **§3, §4, and the Lexicon (`U(T, N_m, S_vk) = S_v`) say K_u.**
>   Unlock consumes `S_vk`, so a PIN rotation — which re-wraps `S_vk`
>   under a new K_u — would invalidate every live mount.
> - **The implementation seals K_v.** Recovery is one unwrap
>   (`open_mount_secret -> K_v`), reads nothing from disk, and a PIN
>   rotation leaves live mounts working.
>
> Until this is settled, **ports MUST follow the code (K_v)**; volumes on
> disk were written that way. The two positions cannot both hold, because
> "mounts survive a PIN change" and "unlock reads `S_vk`" are the same
> question asked from two directions. Note that revoking a token does not
> require choosing K_u: `session_kek = HKDF(T, salt=N_m)` and `N_m` is a
> mutable column on the mount row, so rotating it invalidates every token
> for that mount alone, without touching K_v, S_vk, or the PIN.
>
> Everything else in this document has been corrected against
> `aloelite/crypto.py`, and `conformance/vectors/format-v1.json` pins the
> corrected constructions with exact bytes.
>
> **Update:** PIN rotation (previously reserved) is implemented as the
> `change_pin` operation: unwrap K_v with the old K_u, re-wrap under the
> new K_u with a fresh N_wrap; K_v, chunk addresses, and dedup are
> unchanged, and live mounts are unaffected (they hold K_v, not K_u).
> CLI: `aloelite-admin pin change`.

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

Mount:
- N_m — stored on the mount row, not memory-only. Readable by anyone with the
  file (an SQLite browser will show it), so it is a salt rather than a secret.
  Being persisted and mutable is what makes it the revocation lever: rotate it
  and every token issued for that mount stops working.

**Runtime Data**

Held by whoever the deployment chooses — these are independently placeable, not
a fixed split (see §3):
- T — the mount token
- mount_secret + N_session — the seal T opens

## Lexicon ↔ implementation

The whiteboard notation writes `S_*` for *secret*; the code writes `K_*` for
*key*. They are the same objects.

| Lexicon | `crypto.py` | Is |
|---|---|---|
| `K` (in `P(K, H_v, S_v)`) | `K_u` | PIN-derived unlock key, `Argon2id(PIN, salt=H_v)` |
| `S_v` | `K_v` | volume content key; immutable for the volume's life |
| `S_vk` | `wrapped_key` + `wrap_nonce` | `S_v` sealed under `K` |
| `S_c` | `K_chunk` | per-volume chunk subkey |
| `xC` | `content_chunk.data` | chunk ciphertext |
| `N_c` | `content_chunk.N_c` | convergent chunk nonce (12 bytes) |
| `T` | `token` | mount token |
| `N_m` | `mount.N_m` | mount nonce (16 bytes) |

The Lexicon's `U(T, N_m, S_vk) = S_v` takes `S_vk` as an input, which is the
whiteboard stating the K_u position — see the open decision at the top.

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
N_m = random(16)  # mount nonce, STORED on the mount row; not secret, but rotatable
session_kek = HKDF(T, salt=N_m, info="aloelite-session")  # HKDF not Argon2 (T is already random)
mount_secret = ChaCha20Poly1305.encrypt(
  key=session_kek,
  nonce=N_session (random, held with mount_secret),
  plaintext=K_u        # SEE THE OPEN DECISION AT THE TOP: the code seals K_v
)  # memory-only by default; a host MAY persist it, at the cost noted below
```

`N_m` is a salt, not a second key input — an implementation that concatenates
it into the HKDF input instead derives a different `session_kek` and cannot
open a `mount_secret` sealed by another implementation.

The three pieces are deliberately **independently placeable**. The simplest
deployment holds all three in one process; split custody gives a client `T`
while a host keeps `mount_secret` + `N_session`, and either side can read `N_m`
from the file. Nothing in the format couples them.

Because `mount_secret` is memory-only by default, a token already dies with the
process that minted it. A host that chooses to persist `mount_secret` gives that
property up, and should fold a per-process nonce into the derivation
(`session_kek = HKDF(T, salt=N_m || N_p, ...)`) to get it back. `N_p` is
runtime policy and **must not** become a stored field — the at-rest bytes are
identical whether or not a deployment uses one.

### 4. Per-Operation Key Recovery
```
session_kek = HKDF(T, "session" || N_m)
K_u = ChaCha20Poly1305.decrypt(session_kek, mount_secret)
K_v = ChaCha20Poly1305.decrypt(K_u, S_vk)
```

### 5. Chunk Encryption
```
K_chunk = HKDF(K_v, info="aloelite-chunk:" || volume_id)   # note the trailing colon
N_c = SHA256("aloelite-nc" || len_8be || plaintext)[:12]   # 12 bytes: ChaCha20-Poly1305 IETF
(xC, tag) = ChaCha20Poly1305.encrypt(K_chunk, N_c, plaintext_chunk)
# Store: chunk_hash, N_c, enc_tag, data (xC || enc_tag or separate columns)
```

`len_8be` is the plaintext length as 8 bytes big-endian. Both the `"aloelite-nc"`
prefix and the colon in `"aloelite-chunk:"` are load-bearing: drop either and a
port derives different nonces or a different subkey, produces different chunk
addresses, and silently stops sharing pool rows with every other implementation.

**Three string literals are frozen into the derived bytes** — `"aloelite-nc"`,
`"aloelite-chunk:"`, and `"aloelite-session"`. The *concepts* may be renamed
freely in prose and in code; changing these literals is a format break that
invalidates every existing volume. (Note that "session" is baked in this way
even though the term is otherwise being retired from the vocabulary.)

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
