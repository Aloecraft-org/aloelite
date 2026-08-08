#!/usr/bin/env bash
# Fetch the wasm wheels aloelite needs (cryptography, pydantic, pyyaml,
# msgpack, + pytest for the conformance run) into node_modules/pyodide/ so
# loadPackage resolves them locally -- no CDN at runtime.
#
# The wheels ship inside the pyodide GitHub release tarball matching the
# installed npm `pyodide` version. The tarball is large (~400 MB) but is
# streamed: only the listed wheels are written to disk (~15 MB).
# (With open network access, `loadPackage` fetching from the pyodide CDN
# works out of the box and this script is unnecessary.)
set -euo pipefail
cd "$(dirname "$0")"

PYODIDE_DIR="node_modules/pyodide"
VERSION=$(node -p "require('pyodide/package.json').version")
echo "pyodide $VERSION"

node - <<'EOF' > wheel_members.txt
const lock = require("pyodide/pyodide-lock.json");
const norm = (s) => s.toLowerCase().replaceAll("_", "-");
const byName = {};
for (const [k, v] of Object.entries(lock.packages)) byName[norm(k)] = v;
const want = ["cryptography", "pydantic", "pyyaml", "msgpack", "pytest"];
const seen = new Set();
const add = (n) => {
  const p = byName[norm(n)];
  if (!p || seen.has(p.file_name)) return;
  seen.add(p.file_name);
  (p.depends ?? []).forEach(add);
};
want.forEach(add);
console.log([...seen].map((f) => `pyodide/${f}`).join("\n"));
EOF

echo "streaming $(wc -l < wheel_members.txt) wheels out of the release tarball..."
curl -sSL "https://github.com/pyodide/pyodide/releases/download/${VERSION}/pyodide-${VERSION}.tar.bz2" \
  | tar -xjf - -C "$PYODIDE_DIR" --strip-components=1 -T wheel_members.txt
ls "$PYODIDE_DIR"/*.whl | wc -l | xargs echo "wheels in place:"
