# Pyodide proof of concept — aloelite fully client-side

**Result: the unmodified aloelite engine runs in the browser today.** The
portable test suite — all conformance scenarios, the format vectors, and the
encryption/operations/path/CLI tests — passes **221/221 inside the wasm
runtime**, and a `.fs` file created inside the browser opens byte-identically
in native CPython (and vice versa). Total effort was one 160-line compat shim;
zero engine changes.

Verified here (Pyodide 314.0.3, Python 3.14.2/wasm32, Node 22 + headless
Chromium):

| check | result |
|---|---|
| format vectors (`conformance/vectors/format-v1.json`) | all sections byte-exact in wasm, incl. convergent ciphertext |
| Argon2id (64 MiB, t=3, p=4) via wasm `cryptography` 47.0.0 | ok, ~0.4–0.5 s per derive |
| pytest suite, portable subset (11 modules) | **221 passed** in ~40 s |
| end-to-end smoke (encrypted volume, multi-chunk IO, pack/unpack, dedup, BadKey/EncryptionRequired) | pass, 0.85 s |
| wasm-created file → native CPython 3.11 + sqlite 3.45.1 | `integrity_check` ok, contents byte-identical, native write-back ok |
| browser (headless Chromium, all assets served from localhost) | pass, ~7 s cold start |

Excluded as inherently non-browser: `test_fuse_mount` (kernel FUSE),
`test_direct`/`test_store`/`test_integration` (manager server + threading).

## The one real gap: Pyodide ships SQLite 3.39.0

Current Pyodide (even the Python 3.14 builds) bundles SQLite **3.39.0**
(2022-07) — below the engine's ≥ 3.45 capability floor. The probe in
`db.py` refuses it exactly as designed. An audit of `schema.sql` +
`sql-templates.yaml` found only **two** post-3.39 constructs in actual SQL
(every `->` hit is in comments):

- `unixepoch('subsec')` — 26 uses (timestamps, uuid7 minting in triggers)
- `jsonb(...)` / `json(...)` — 5 uses (NODE-6 metadata write/read)

`pyodide_compat.py` closes the gap with per-connection UDFs (SQLite lets a
UDF override a builtin, and UDFs are visible to triggers and views, so the
INSTEAD OF id-minting works unchanged):

- `unixepoch('subsec')` → `time.time()`
- `jsonb(x)` → a real **JSONB binary encoder** (~100 lines, both directions).
  `node.metadata` is `BLOB` in a STRICT table and a native sqlite ≥ 3.45
  interprets metadata blobs as JSONB, so the shim must emit the genuine
  binary format — text-in-blob would corrupt cross-compat. The codec is
  validated bidirectionally against native sqlite 3.45.1
  (my bytes → native `json()`, native `jsonb()` → my decoder).
- `json(x)` → decodes JSONB blobs / normalizes text (3.39's builtin can't
  read JSONB).

Files written through the shim are fully valid for modern native SQLite —
that is what the cross-check proves. If/when Pyodide bumps its bundled
SQLite past 3.45, the shim self-disables (it only installs when the probe
finds the builtins missing... see `smoke.py`).

## Running it

```bash
cd notebook/pyodide-poc
npm install            # pyodide runtime (npm package = runtime only, no wheels)
./fetch_wheels.sh      # pull the wasm wheels out of the pyodide release tarball
npm run smoke          # probes + format vectors + end-to-end engine, in Node
npm run conformance    # aloelite's own pytest suite (portable subset) in wasm
python3 cross_check.py # open the wasm-created .fs with native CPython
npm run serve          # browser demo at http://127.0.0.1:8871/
node shot.mjs          # headless-Chromium screenshot of the browser demo
```

`fetch_wheels.sh` exists because this sandbox can't reach the pyodide CDN;
with normal network access `loadPackage` fetches wheels itself and the
script is unnecessary. A production deployment self-hosts the same files
(runtime + 4 wheels + source zip, ~20 MB) on any static host — no server
code, no CDN dependency.

## What a real hosted demo still needs (beyond this PoC)

1. **Persistence** — this PoC keeps the `.fs` in MEMFS (gone on reload) and
   proves portability via download/upload. Real version: Emscripten IDBFS
   (trivial) or OPFS (better) mounted at the volume directory, plus
   drag-in/drag-out import/export. The download/upload story is the demo's
   punchline: the file you make in a tab opens in desktop Python aloelite.
2. **A UI** — this page just runs the smoke test. Options, in effort order:
   a thin file-browser page over `Mount` calls via Pyodide's JS FFI, or a
   service-worker shim that emulates the manager's REST surface so the
   existing `admin.html`/`admin.js` runs nearly unmodified (no Flask).
3. **A worker** — move Pyodide off the main thread (Argon2id blocks ~0.5 s;
   currently visible as UI jank only during unlock).
4. **Payload trimming** — ~20 MB / ~7 s cold start is acceptable for a demo,
   not for a product page. The TS/wasm port from `doc/ROADMAP` remains the
   right long-term answer; this PoC is the fast on-ramp and, later, the
   reference oracle running *in the same browser* next to it.

Security framing for a public demo: everything runs client-side; the PIN
and unwrapped keys live in page memory (the ENCRYPTION.md §"Token scope"
browser caveat). Fine for a demo, worth a one-line disclosure on the page.
