# Handoff — aloelite web viewer (client-side tool)

Continuation of the assessment + PoC on this branch (see
`notebook/pyodide-poc/README.md` for the feasibility results that precede
this). State as of this handoff: **the viewer works end to end** — real
engine in the browser, strict CSP holding, tests green. What's here is a
solid v0 skeleton, not a launched product.

## Decisions already made (don't relitigate casually)

1. **Positioning**: hosted *viewer* for the no-install case + scratch demo
   mode. Not a manager replacement; self-host story = same static files.
   The Pyodide backend is the on-ramp; the TS/wasm or aloelite-rs port
   replaces it behind the same contract later.
2. **The contract is the architecture**: `static/engine/api.js` is the only
   surface the UI touches. Any new capability goes into the contract first,
   then into both backends (`pyodide-engine.js` AND `mock-engine.js` — the
   mock must stay behaviorally honest or the UI tests lie).
3. **Opened volumes are read-only; only scratch is writable.** Enforced in
   bootstrap.py (`_writable`) and surfaced in the UI. Don't add writes to
   opened files without designing the "download your changes" story.
4. **The trust claim is CSP-enforced** (no external origins, period).
   `dev/browser_test.mjs` fails on any CSP violation. Empirically verified:
   Pyodide 314 needs only `'wasm-unsafe-eval'`, not `'unsafe-eval'`.
5. **Engine runs unmodified**; platform gaps are closed in
   `pyodide_compat.py` (sqlite 3.39 → jsonb/json/unixepoch UDFs, including
   a real JSONB binary codec). The web copy and the PoC copy must stay
   identical — `adapter_test.mjs` hash-checks them, and the
   `wasm-conformance` CI workflow runs the whole suite in wasm.

## What exists and is verified

- `web/static/` — landing (no engine load until the user commits a file),
  volume picker + PIN unlock, read-only browser (breadcrumbs, lazy sizes,
  text/image/audio/video preview, per-file download), scratch mode
  (mkdir/upload/delete/export `.fs`), light+dark, dependency-free.
- `web/dev/adapter_test.mjs` — contract test in Node: full surface, closed
  error taxonomy (`not_found`, `not_a_container`, `not_an_entry`,
  `not_empty`, `bad_key`, `encryption_required`, `read_only`,
  `mount_invalid`), export→reopen round trip, PoC demo-file cross-open.
- `web/dev/browser_test.mjs` — headless Chromium: mock UI leg, real
  open→unlock(wrong PIN then right)→browse→preview leg, scratch→upload→
  export leg (download captured, sqlite header verified), zero CSP
  violations, zero page errors.
- `.github/workflows/wasm-conformance.yml` — conformance suite in wasm +
  native cross-check + adapter test, with the wheel fetch cached.

## Memory spike (size guards)

A spike measuring 100/300/600 MB volumes through the browser-realistic
path (bytes → MEMFS → open → mount → read_all → Uint8Array extraction) was
run on this branch; its numbers set the guard constants at the top of
`static/app.js` (`FS_WARN_BYTES`, `FS_HARD_BYTES`, `READ_MAX_BYTES`).
<!-- SPIKE_RESULTS -->

## Next steps, in priority order

1. **Worker**: move the engine off the main thread. The contract in api.js
   is already async everywhere, so this is transport work (postMessage +
   transferables for the bytes channel), not interface work. Argon2id
   (~0.5 s desktop, seconds on mobile) currently stalls the tab during
   unlock — mitigated today by a pre-paint yield + status toast.
2. **Streaming reads**: bootstrap only exposes `read_all`. The engine has
   `open_read` descriptors (`aloelite/descriptor.py`); expose ranged reads
   through the contract to lift `READ_MAX_BYTES` for downloads (stream to
   a Blob in chunks) and to make media previews seek without full reads.
3. **Persistence** (optional for v1): scratch volumes die with the tab —
   by design for a viewer, but OPFS/IDBFS would allow "resume where I
   left off". Decide product-wise before building.
4. **Payload**: serve brotli + immutable caching; consider a service
   worker for offline repeat visits. The 20 MB floor stands until the
   non-Pyodide engine lands.
5. **Polish**: keyboard operability of listing rows, aria on icon-only
   buttons, mobile table overflow, breadcrumb truncation for deep paths —
   an a11y pass was reviewed but only partially applied.
6. **Deploy pipeline**: a `dev/build_dist.sh` that materializes `dist/`
   with real files (no symlinks) + hashes, ready to rsync to a host.

## Security review outcomes (adversarial pass, all applied or filed)

A hostile-.fs threat-model review confirmed the trust claim holds: every DOM
write of volume-controlled data is textContent, no blob: navigation exists,
path resolution is graph-relative (no MEMFS/host traversal), and the PIN is
never retained engine-side. Applied hardening: PIN fields cleared after
unlock, explicit `object-src`/`frame-src 'none'`, failed-open MEMFS cleanup,
best-effort close with sidecar unlinks, mount-by-id (bypassing name-first
resolution a crafted file could shadow), and `_quarantine_untrusted()` in
bootstrap.py which strips a foreign file's own trigger/view definitions
before the engine opens it.

**Two items for the ENGINE (author's call, affects desktop too):**
1. `db.py` era-refresh drops+recreates derived objects only when the file's
   era is OLDER than installed. A file stamped with the CURRENT era keeps
   its own trigger/view definitions (`IF NOT EXISTS`), and those fire on
   open/mount writes. In wasm the blast radius is DoS/garbage-data only (no
   UDF callbacks, no load_extension, no network); on desktop the same
   reasoning applies but deserves the author's eyes. Candidate fix: make the
   drop+recreate unconditional on open. The viewer quarantines regardless.
2. Mounting writes mount rows into the opened file (a mount is a row), so a
   "read-only" viewer session still mutates its in-MEMFS copy — the user's
   original is untouched and export is hidden for read-only mounts, so this
   is currently inert; it becomes relevant if export-for-opened-files is
   ever added.
3. `frame-ancestors 'none'` cannot be set via meta tag — it must be an HTTP
   header on the static host (README deploy section).

## Gotchas for the next session

- The sandbox proxy blocks the Pyodide CDN; `fetch_runtime.sh` streams
  wheels out of the GitHub release tarball instead (~400 MB streamed,
  ~15 MB kept). With normal egress, the CDN route also works.
- `notebook/pyodide-poc/node_modules/pyodide` is the shared local runtime;
  `dev/serve.sh` and the tests find it automatically.
- `web/` is inside ruff's lint gate (unlike `notebook/`); bootstrap.py
  carries deliberate `# noqa: E402` (sys.path setup precedes imports).
- Scratch `.fs` files accumulate in wasm `/work` per page session; they're
  MEMFS-only and die with the tab, so cleanup is not urgent (worth adding
  `close_session` unlink when touching bootstrap anyway).
