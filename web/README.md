# aloelite viewer — client-side web tool

A hosted way to open an aloelite `.fs` volume **without installing anything
and without uploading it anywhere**: the unmodified Python engine runs in the
browser (Pyodide/wasm), so the file, the PIN, and every decrypted byte stay
in the tab. A "try it from scratch" mode creates a volume in the browser and
downloads the single `.fs`, which desktop aloelite opens as-is.

This is deliberately a *viewer*, not a manager: opened volumes are read-only
(the original file on disk is never touched either way — the browser works on
a copy in wasm memory), and only scratch volumes are writable. If you want
mounts, FUSE, and volume lifecycle, use the standard `aloelite-web` manager.

**Positioning / lifespan:** the ~20 MB runtime is the honest cost of shipping
a Python interpreter to run ~3k lines of engine; the planned TS/wasm port
replaces the backend here at a fraction of the size. That swap is contained
by design — see "the contract" below.

## Architecture

```
static/
  index.html, app.css, app.js     the UI. Talks ONLY to the engine contract.
  engine/
    api.js                        THE CONTRACT (JSDoc-typed, transcribed from
                                  config/mount-api.yaml). UI code must never
                                  import a backend directly.
    errors.js                     mount-api.yaml's closed error set + adapter codes
    pyodide-engine.js             real backend: boots Pyodide lazily, loads the
                                  UNMODIFIED engine from /aloelite_src.zip
    bootstrap.py                  runs inside Pyodide; one JSON entry point +
                                  one bytes channel; maps FsError -> closed codes
    pyodide_compat.py             sqlite 3.39 shim (twin of the PoC's copy;
                                  adapter_test enforces they stay identical)
    mock-engine.js                in-memory backend for UI dev: /?engine=mock
dev/
  serve.sh                        assemble dist/ + serve on :8872
  fetch_runtime.sh                pyodide runtime + wasm wheels (or reuse the PoC's)
  build_src_zip.py                the engine source bundle the page serves
  adapter_test.mjs                contract test in Node (runs in CI)
  browser_test.mjs                Playwright end-to-end incl. strict-CSP check
```

When the aloelite-rs / TS engine lands, implement `api.js`'s contract over it
and swap the factory in `createEngine()` — the UI does not change.

## Run it locally

```bash
cd web
./dev/fetch_runtime.sh   # skip if notebook/pyodide-poc already has the runtime
./dev/serve.sh           # http://127.0.0.1:8872/  (UI-only dev: /?engine=mock)
node dev/adapter_test.mjs   # engine contract test (Node)
node dev/browser_test.mjs   # full flow in headless Chromium + CSP check
```

## Deploying

The deployable unit is exactly what `dev/serve.sh` assembles into `dist/`:
`index.html`, `app.css`, `app.js`, `engine/`, `aloelite_src.zip`, and a
`pyodide/` directory (runtime + the four wasm wheels). Copy it — with real
files in place of the symlinks — to any static host; there is no server
component. Serve with long-lived immutable caching and (ideally) brotli for
the runtime assets.

**The trust claim is enforced, not asserted:** the CSP in `index.html`
permits no external origin for anything. Keep it that way — no CDN, no
analytics, no error reporting. `browser_test.mjs` fails on any CSP violation,
so a change that would break the claim breaks the test first.

One directive cannot live in the meta tag: serve
`Content-Security-Policy: frame-ancestors 'none'` (or `X-Frame-Options:
DENY`) as an **HTTP header** from the static host, so no other site can
frame the viewer.

Self-hosting the viewer is exactly this same static copy on your own origin —
do it if you want the no-upload guarantee behind your own firewall. It is
*not* a replacement for the manager.
