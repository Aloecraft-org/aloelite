# The Rust port — plan and standing

<div align="center">

<img src="https://raw.githubusercontent.com/Aloecraft-org/aloelite/refs/heads/main/doc/aloelite.png" style="height:96px; width:96px;"/>

**Aloelite Single-File Filesystem**

[Overview](/README.md) | [Requirements Spec](/doc/REQUIREMENTS.md) | [Decisions](/doc/DECISIONS.md) | [Compatibility](/doc/COMPATIBILITY.md) | [Conformance](/conformance/README.md)
</div>

`rust/` is the Rust implementation of the Mount API. This document is the
plan it is built to, and it is kept current as the plan meets reality — the
"Standing" section at the bottom says what exists today. The decision it
rests on is `doc/DECISIONS.md` D-7; read that first.

## What we are building, and for whom

Three targets, as peers:

| target | runs where | frontends |
|---|---|---|
| native (x86_64, aarch64; glibc and musl) | servers, laptops, containers | FUSE, CLI, and the manager's engine seat |
| `wasm32-wasip2` | wasmtime and other WASI hosts | CLI; a component with the volume on the host filesystem |
| `wasm32-unknown-unknown` | a browser, inside a Dedicated Worker | a page, over `postMessage`; the volume in OPFS |

"As peers" is load-bearing. The failure mode this plan is shaped against is
not disagreement about whether WebAssembly matters; it is drift — a native
convenience taken today that costs a rewrite when the browser target is
picked up. Aloelite is unusually well suited to running in a browser (one
file, host-side encryption, no server trust), and that only stays true if
the browser is never the target that waits.

## The premise that makes it one engine

SQLite is the entire engine, and `rusqlite` reaches every target — `bundled`
statically links it natively and on WASI, and rusqlite's default
`ffi-sqlite-wasm-rs` feature swaps in `sqlite-wasm-rs` on
`wasm32-unknown-unknown`. Verified on this scaffold: `aloelite-core` with
rusqlite, plus `sqlite-wasm-vfs`'s OPFS VFS, type-checks for the browser
target today.

So the SQL layer is portable, and the only thing that varies per target is
**how a connection is opened**. That is the whole design: one engine crate
that never asks where it is, and a small store crate that answers the one
question the engine cannot.

## Crates

```
rust/
  aloelite-core/          the engine: schema, templates, id mint, ENC-2 ladder,
                          resolution, every operation. no I/O, no platform, no cfg
  aloelite-store/         connection provisioning: file / memory image + blob /
                          OPFS sahpool (see D-7's table)
  aloelite-conformance/   the conformance/ runner, under cargo test natively and
                          wasm-bindgen-test in a real browser
  aloelite-fuse/          the Linux FUSE daemon over fuser. native only, by nature
  aloelite-wasm/          the browser surface: wasm-bindgen, Dedicated Worker, OPFS
  aloelite-cli/           the aloelite command. native + wasm32-wasip2
```

Each crate's `README`/`lib.rs` states its contract and target set.
`rust/README.md` has the build matrix.

**The rule:** `aloelite-core` compiles to all three targets with zero `cfg`.
It takes a `rusqlite::Connection` someone else opened, a `Clock`, and a
`CryptoRngCore`. Anything that cannot meet that bar is a different crate.

**The mechanism:** the `rust` job in `.github/workflows/main.yml` builds
core for all three targets and runs the conformance suite in headless
Firefox, on every push, from before the first line of engine code. The
first `std::fs` import fails there in minutes. Intent does not prevent
drift; a red job does.

## What is inherited, and from where

The port is unusually well provisioned because the Python side was built as
a reference rather than a product. What a second implementation inherits:

| artifact | what it gives Rust |
|---|---|
| `aloelite/config/mount-api.yaml` | ~50 operations, closed error set, records, enums, and a per-op `locks` flag that is asserted, not documented |
| `aloelite/config/sql-templates.yaml` | 60 static SQL templates with named binds, written for rusqlite among others. The host owns only what is *between* templates; the `host_only:` section names exactly those four things |
| `aloelite/sql/schema.sql` | 8 tables, ~12 triggers, ~13 views. Plain SQLite; ports verbatim |
| `conformance/scenarios/` | 91 scenarios, pure data. The Rust runner reads the same files the Python one does |
| `conformance/vectors/` | byte-exact: the uuid7 mint state machine (`ids-v1.json`), chunk addressing and the whole ENC-2 ladder (`format-v1.json`) |
| `doc/REQUIREMENTS.md`, `doc/DECISIONS.md` | the contract and why it is shaped that way |

And what it does **not** inherit, which is where the cost is:

- **The POSIX/FUSE surface has no portable oracle.**
  `tests/test_posix_surface.py` and `tests/test_fuse_mount.py` are ~660
  lines of pytest against a live kernel mount, and they are the only thing
  pinning `doc/COMPATIBILITY.md`. Every ✅ in that table is re-established in
  `aloelite-fuse` by hand. This is the single largest line item.
- **Pack has scenarios but no byte vectors.** `pack`/`unpack` (OP-6/OP-7) are
  a cross-implementation wire format with no fixed-bytes pin; the scenarios
  round-trip within one implementation. Rust and Python could disagree
  byte-for-byte and both pass. Vectors are owed before Rust implements pack,
  and the v2 format question (`DECISIONS.md` D-6's neighbours; see the
  "pack format" thread in `HANDOFF-0.4.0rc1.md`) should be settled first.
- **The CLI has no spec.** 754 lines of Python, no contract, no oracle.
- **Era-1 migration** lives in `db.py::_migrate_to_era2` with a Python
  fixture only. `aloelite-rs` may refuse era-1 files and defer to the Python
  tool; that is cheaper and defensible, and wants writing down.

## Platform seam: ego-platform

[`ego-platform`](https://github.com/Aloecraft-org/ego-platform) is the
cross-target abstraction the crates use. Git dependency for now.

| ego-platform | used for |
|---|---|
| `blobs::BlobStore` (`MemStore` / `DirStore` / `IdbStore`) | whole-volume persistence in the memory-image model; atomic put is the durability primitive |
| `clock::Clock` (`SystemClock` / `ManualClock`) | id mint watermark (D-2), lock TTLs, node timestamps. `ManualClock` makes the clock-regression vectors deterministic |
| `entropy::SystemEntropy` (`CryptoRngCore`) | uuid7 random tail, ChaCha20 nonces, volume keys. Never call `getrandom` directly |
| `spawn`, `time` | FUSE TTL renewal, prune scheduling |
| `fs` | **not used for volumes** — its browser backend is `localStorage`; blobs and OPFS are the right seams |

## Crypto: aligned, not shared

`aloecrypt_core` was checked as a possible home for the ENC-2 ladder. It is
not one: its `pbkdf` is a hash-iteration KDF, not Argon2id; its `hash.rs` is
HMAC-with-domain, not RFC 5869 HKDF. Both are pinned byte-for-byte in
`format-v1.json`, so neither can be substituted, and it also carries
pre-release ML-KEM/ML-DSA the engine does not need.

`aloelite-core` therefore uses RustCrypto directly — `chacha20poly1305`,
`argon2`, `hkdf`, `sha2` — at the versions aloecrypt_core and ego-platform
already use (`chacha20poly1305 0.10`, `sha2 0.11`, `rand_core 0.10`), so one
copy of each lands in the wasm binary. Argon2id's factors (64 MiB / 3 / 4)
are format contract; in the browser they run once per mount inside the
Worker, well under a second, blocking nothing visible.

## Conformance on every target

The runner embeds the scenario and vector files with `include_str!`, so the
same suite runs under `cargo test` and under `wasm-bindgen-test --headless
--firefox` with no filesystem. Two obligations carried from
`conformance/README.md`:

- **The YAML boolean guard.** YAML 1.1 (PyYAML) reads a bare `on`/`off`/
  `yes`/`no` key as a boolean; YAML 1.2 (`serde_norway`) as a string. The
  Rust runner must carry the equivalent of Python's
  `test_no_scenario_key_is_a_yaml_boolean`, or one fixture means two things.
- **Unimplemented harnesses skip, never pass.** A runner that has not built
  a harness must skip its scenarios; silently passing reports conformance
  nobody checked.

`serde_yaml` is archived; `serde_norway` is the maintained API-compatible
fork and is test-only. Revisit `saphyr` when it stabilises.

## Toolchain requirements, by target

| need | native | wasip2 | browser |
|---|---|---|---|
| Rust 1.88+, edition 2024 | ✓ | ✓ | ✓ |
| `libfuse3-dev`, `pkg-config` (fuser) | ✓ | | |
| **wasi-sdk** — cc-rs compiles sqlite3.c and needs a WASI clang + sysroot. Set `CC_wasm32_wasip2`, `AR_wasm32_wasip2`, `CFLAGS_wasm32_wasip2=--sysroot=…` (see `rust/.cargo/config.toml`). Host clang fails with `'bits/libc-header-start.h' file not found` | | ✓ | |
| `wasm-bindgen-test-runner` at the **exact** `wasm-bindgen` version in `Cargo.lock`, plus Firefox + geckodriver | | | ✓ |

The devcontainer (`rust/.devcontainer/`) provides all of it. CI installs the
same.

## Sequence

1. **Scaffold and guard** — this commit. Manifests, contracts, the three-
   target CI job. No engine code.
2. **`aloelite-core`, SQLite-only**, driven by the 91 scenarios and the two
   vector files. The piece with a complete oracle and no unknowns: schema,
   templates, ids (`ids-v1.json` first — byte-for-byte before anything
   ships), crypto (`format-v1.json`; `encrypt_chunk_convergent` is the one
   to check first), resolve, operations. Conformance green natively **and in
   Firefox** before moving on — the browser run is not a later milestone,
   it is the same milestone.
3. **`aloelite-store`** — file, then memory-image + blob, then OPFS sahpool
   in a Worker. The suite runs against each.
4. **`aloelite-wasm`** — the Worker protocol and the Web Lock.
5. **`aloelite-fuse`** — the expensive one. Budget it; the oracle is human.
6. **`aloelite-cli`** — after deciding whether it mirrors Python or writes
   its own contract.

Postgres and MariaDB are not in this sequence. When they come, prototype the
dialect seam in Python first, where the 500-test suite can tell you
instantly what broke.

## Standing

*2026-09-02.* **The engine passes the conformance suite natively.** All 91
scenarios in `conformance/scenarios/`, the 7 fixture checks the Python
runner carries (declared operations, implemented harnesses, the YAML boolean
guard, declared errors, unique names, cited requirements, plus the error
enum projected onto the spec in both directions), and the 16 vector cases.
Every one of the seven harnesses is implemented, including the two-connection
`two_mounts_one_volume` and the four encryption harnesses. The same crate passes
**in a browser**: every scenario and vector under headless Chromium via
`wasm-bindgen-test` (5.6 s for the 91 scenarios, two-connection harness and
encrypted harnesses included, on `sqlite-wasm-rs`'s in-memory VFS); CI runs
the identical binary under headless Firefox. `wasm32-wasip2` type-checks the
engine and the runner with its tests.

**Engine modules, all under `aloelite-core/src/`, zero `cfg`:**

- `platform.rs` — `Clock` and `CryptoRngCore`, the two things the engine
  takes from the host. `aloelite-store/src/clock.rs` adapts ego-platform's.
- `types.rs` / `records.rs` — the spec's scalars, enums and records. Ids are
  newtypes that bind and read directly; records serialize, which is what the
  runner compares against and what a wasm binding will hand to JavaScript.
- `errors.rs` — the closed set as one enum; `code()` is the spec name. Three
  engine-side variants carry no code: `Sqlite`, `Internal`, and `Usage`
  (closed descriptor, negative seek — the reference's `ValueError`).
- `templates.rs` + `build.rs` — the sixty-four templates as `const &str`,
  generated from `sql-templates.yaml` at build time. A template name is a
  compile error, not a run-time `KeyError`, and the shipped engine carries no
  YAML parser. `schema.sql` is `include_str!`.
- `db.rs` — the substrate, mirroring `db.py`: capability probe, journal-mode
  probe with the PERSIST fallback, era gate, era-1→2 migration, derived-object
  refresh, the mint with its fenced high-water mark flushed per write
  transaction, `txn`, and the chunk staging/reassembly primitives.
- `resolve.rs` — the one-query walk; `.`/`..` are ordinary names.
- `descriptor.rs` — the streaming descriptor with bounded memory; each call
  takes the `Db` it was opened on (the flat `read(fd, len)` shape).
- `ops/` — every operation, function-for-function with `operations.py`, split
  by the spec's groups (`session`, `read`, `structural`, `content`, `tree`,
  `locking`, `streaming`, `maintenance`).

**Settled on the way:**

- rand_core 0.10's `DerefMut` blanket makes `Box<dyn CryptoRngCore + Send>`
  itself an `Rng + CryptoRng`; the generic crypto and id functions take
  `R: ?Sized`, so the engine's boxed source passes straight through.
- The pack codec is `rmp_serde::to_vec_named` + `serde_bytes`, byte-identical
  to `msgpack.packb(use_bin_type=True)`; a unit test pins the bytes against a
  hand-encoded sample, and the version gate is read before the body so a v2
  blob's unknown fields can never masquerade as corruption.
- `profile.dev` optimizes `argon2`/`blake2` alone; the encrypted harnesses
  would otherwise dominate the browser run.
- The runner mints one test per scenario at build time (`<file>_<scenario>`),
  so both `cargo test` and the browser report scenarios individually. Its
  only `cfg` is where a scenario's database file lives: the temp directory
  natively, a name in `sqlite-wasm-rs`'s in-memory VFS in the browser, where
  `std::env::temp_dir()` panics.

**Parity notes against the reference:** the spec's `move` is `ops::move_`;
`stat_by_id` is the only `*_by_id` variant, as in `operations.py`; `mount`
takes a `MountOptions` struct for its six optional parameters. Resolving a
volume by NAME is a facade rule, not a Mount API operation: the most
recently created volume wins (`created_at`, then id) — the Rust facade must
apply the same rule when it exists.

**Pack interop is proven, not reasoned:** `conformance/vectors/pack-v1.json`
pins the codec (`aloelite_core::pack`, `aloelite/pack.py`) byte-for-byte in
both directions, and `coherence.yaml` restores a reference-produced pack end
to end through the API in both runners. The v2 scope is decided in D-8 and
lands Python-first; until then Rust writes v1, which stays readable under
any v2.

**Next:** `aloelite-store` (file / memory image + `BlobStore` / OPFS
sahpool), then the `aloelite-wasm` worker surface, `aloelite-fuse`,
`aloelite-cli`. Pack v2 (D-8) in Rust follows the Python implementation.
