#!/usr/bin/env bash
# Assemble web/dist (the deployable docroot) and serve it locally.
#
# dist/ is symlinks to static/ plus two generated pieces: the engine source
# zip and the Pyodide runtime directory. For a real deployment, copy the
# same layout with real files onto any static host — there is no server
# component and no build step.
#
# Runtime resolution order:
#   1. $PYODIDE_RUNTIME_DIR
#   2. web/.runtime/node_modules/pyodide      (created by fetch_runtime.sh)
#   3. notebook/pyodide-poc/node_modules/pyodide  (reuse the PoC's copy)
set -euo pipefail
cd "$(dirname "$0")/.."

RUNTIME="${PYODIDE_RUNTIME_DIR:-}"
[ -z "$RUNTIME" ] && [ -d .runtime/node_modules/pyodide ] && RUNTIME=.runtime/node_modules/pyodide
[ -z "$RUNTIME" ] && [ -d ../notebook/pyodide-poc/node_modules/pyodide ] && RUNTIME=../notebook/pyodide-poc/node_modules/pyodide
if [ -z "$RUNTIME" ]; then
  echo "no pyodide runtime found — run web/dev/fetch_runtime.sh first" >&2
  exit 1
fi

mkdir -p dist
ln -sf ../static/index.html dist/index.html
ln -sf ../static/app.css dist/app.css
ln -sf ../static/app.js dist/app.js
ln -sfn ../static/engine dist/engine
ln -sfn "$(realpath "$RUNTIME")" dist/pyodide
python3 dev/build_src_zip.py dist/aloelite_src.zip

PORT="${PORT:-8872}"
echo "aloelite viewer at http://127.0.0.1:${PORT}/  (mock UI: /?engine=mock)"
exec python3 -m http.server "$PORT" --bind 127.0.0.1 --directory dist
