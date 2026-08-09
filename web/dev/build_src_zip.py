# Build the aloelite engine source bundle the viewer serves at
# /aloelite_src.zip: engine package + specs + format vectors. No tests, no
# manager, no docs — this is exactly what runs inside the wasm runtime.
import os
import sys
import zipfile

repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(__file__), "..", "dist", "aloelite_src.zip"
)
os.makedirs(os.path.dirname(out), exist_ok=True)

z = zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED)
n = 0
for root in ("aloelite", "config", "sql", "conformance/vectors"):
    for dp, _, fns in os.walk(os.path.join(repo, root)):
        if "__pycache__" in dp:
            continue
        for f in fns:
            full = os.path.join(dp, f)
            z.write(full, os.path.relpath(full, repo))
            n += 1
z.close()
print(f"{out}: {os.path.getsize(out)} bytes, {n} files")
