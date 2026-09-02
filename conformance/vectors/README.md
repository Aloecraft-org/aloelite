# Format vectors

`ids-v1.json` pins the host-minting contract (doc/DECISIONS.md D-1/D-2):
the uuid7 layout's deterministic prefix and the MonotonicMint state machine
(advance, 1ms borrow on overflow, clock-regression absorption, and the
high-water-mark fence). Runner: tests/test_ids.py::test_conformance_id_vectors.

`format-v1.json` pins the byte-level contract: content addressing, chunking,
and the ENC-2 key ladder. Fixed inputs in, exact bytes out.

`pack-v2.json` pins the pack blob codec (OP-6/OP-7, `aloelite/pack.py`): the
one cross-implementation byte contract besides the chunk format. `encode`
cases are node lists with the exact blob the reference produces — every node
type, metadata, ownership and mode, xattrs, retention, unicode, and the
MsgPack marker boundaries (fixstr/str8, bin8/16/32, fixmap/map16,
fixarray/array16, the integer widths). `decode` cases are raw blobs with the
error code the gate must answer (newer version, missing version, wrong `fmt`,
a top-level array, malformed nodes, a str where bytes belong, truncation) or
the node list a tolerant read must produce (absent optional fields, null
values, unknown keys, and a v1 blob). Payload bytes appear as `d_hex`, xattr
values as `xa_hex`.

`pack-v1.json` is the reader's compatibility contract: v1 blobs, produced by
a frozen v1 encoder that no shipping writer has any more, with the nodes
every reader must recover from them (D-8: v1 is readable forever).

The walk order that feeds the codec is pinned by the scenarios;
`coherence.yaml` restores a reference-produced v1 pack end to end, and
`pack.yaml` pins the v2 round trip through the API. Runners:
`tests/test_pack_vectors.py` and
`rust/aloelite-conformance/tests/pack_vectors.rs`.

## Sections

| section | pins |
|---|---|
| `constants` | AEAD sizes and the Argon2id work factors |
| `chunk_address` | CV-2 — `SHA256(len_be64 \|\| bytes)` over the bytes actually stored |
| `chunk_split` | CV-1 — uniform chunks, short final chunk, empty stages none |
| `volume_hash` | `H_v = SHA256(volume_id \|\| root_node_id)` |
| `unlock_key` | `K_u = Argon2id(PIN, salt=H_v)` at the fixed work factors |
| `chunk_key` | `K_chunk = HKDF(K_v, info="aloelite-chunk:" \|\| volume_id)` |
| `encrypt_chunk_convergent` | `N_c`, ciphertext, tag, and resulting pool address |
| `unwrap_volume_key` | opening `S_vk` — the direction that is deterministic |
| `session_kek` | `HKDF(T, salt=N_m, info="aloelite-session")` |

All byte fields are lowercase hex. Ids are literal strings.

`encrypt_chunk_convergent` is the one to check first in a new port. Convergent
encryption has to be byte-identical everywhere or the same plaintext lands at
two different pool addresses, and cross-implementation dedup stops working
without anything appearing to be broken. `chunk_key` is included even though
`encrypt_chunk_convergent` subsumes it, because when a port disagrees you want
to know immediately whether the fault is the key derivation or the AEAD.

`wrap_volume_key` has no vector: it picks a fresh random nonce, so there is no
fixed output to assert. The unwrap direction covers the same ladder.

## What these are, and are not

They are **agreement vectors, generated from the Python reference
implementation** — the file records what Aloelite does today. They are not an
independent proof that the construction is cryptographically correct; that
question belongs to `doc/ENCRYPTION.md` and to review of the design, not to
this file. What they guarantee is that a second implementation agreeing with
them will read and write files the first one can open.

## Regenerating

```bash
python script/gen_format_vectors.py
python script/gen_pack_vectors.py
```

Every value is derived, so regenerating should produce an empty diff. A
non-empty diff means the on-disk format moved. That is not automatically wrong
— compression, a new `enc_mode`, or a chunking change would all do it — but it
is never incidental, and it invalidates every file written by every prior
implementation unless `volume.api_version` gates it.

Runners: Python's are `tests/test_format_vectors.py`, `tests/test_ids.py` and
`tests/test_pack_vectors.py`; Rust's are the three `*_vectors.rs` files under
`rust/aloelite-conformance/tests/`, run natively and in a browser.
