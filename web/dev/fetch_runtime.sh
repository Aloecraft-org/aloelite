#!/usr/bin/env bash
# Fetch the Pyodide runtime + the wasm wheels aloelite needs into
# web/.runtime/. Prefer reusing notebook/pyodide-poc's copy (serve.sh finds
# it automatically); run this only when the PoC copy is absent.
#
# The wheels ship inside the pyodide GitHub release tarball matching the npm
# runtime version. The tarball is large (~400 MB) but is streamed: only the
# needed wheels are written to disk (~15 MB). With open network access,
# skipping the wheel step also works: loadPackage falls back to the CDN.
set -euo pipefail
cd "$(dirname "$0")/.."

mkdir -p .runtime
cd .runtime
[ -f package.json ] || echo '{"private": true, "dependencies": {"pyodide": "^314.0.3"}}' > package.json
npm install --no-audit --no-fund

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
  | tar -xjf - -C node_modules/pyodide --strip-components=1 -T wheel_members.txt
ls node_modules/pyodide/*.whl | wc -l | xargs echo "wheels in place:"
