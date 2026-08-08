#!/usr/bin/env bash
# Assemble a static docroot for the browser demo and serve it on :8871.
# Everything is served locally (runtime, wheels, aloelite source) -- the same
# shape a production static-host deployment would have.
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p docroot
ln -sfn ../node_modules/pyodide docroot/pyodide
ln -sf ../browser/index.html docroot/index.html
ln -sf ../pyodide_compat.py docroot/pyodide_compat.py
ln -sf ../smoke.py docroot/smoke.py

# aloelite source bundle: engine + specs + vectors (no tests, no manager)
(cd ../.. && python3 - <<'EOF'
import os, zipfile
z = zipfile.ZipFile("notebook/pyodide-poc/docroot/aloelite_src.zip", "w", zipfile.ZIP_DEFLATED)
for root in ("aloelite", "config", "sql", "conformance/vectors"):
    for dp, _, fns in os.walk(root):
        if "__pycache__" in dp:
            continue
        for f in fns:
            z.write(os.path.join(dp, f), os.path.join(dp, f))
z.close()
print("aloelite_src.zip:", os.path.getsize("notebook/pyodide-poc/docroot/aloelite_src.zip"), "bytes")
EOF
)

echo "serving http://127.0.0.1:8871/ (ctrl-c to stop)"
exec python3 -m http.server 8871 --bind 127.0.0.1 --directory docroot
