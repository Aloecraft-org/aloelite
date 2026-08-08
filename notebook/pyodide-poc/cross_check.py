# Host-side cross-check: open the .fs file that was CREATED INSIDE WASM with
# native CPython aloelite (real sqlite >= 3.45, native jsonb, real cryptography)
# and verify contents, encryption, and file health. This is the portability
# proof: wasm-written bytes -> native-read bytes.
#
# Needs a host python >= 3.11 with sqlite >= 3.45 and the aloelite deps
# installed (pip install cryptography pydantic pyyaml msgpack).
import json
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from aloelite import Aloelite  # noqa: E402
import aloelite.errors as errs  # noqa: E402

FS = sys.argv[1] if len(sys.argv) > 1 else str(
    pathlib.Path(__file__).resolve().parent / "out" / "pyodide-demo.fs"
)
PIN = b"1234"
payload = ("hello from wasm \N{SPARKLES} -- " * 8).encode()

out = {"host": {}, "checks": {}}
out["host"]["python"] = sys.version.split()[0]
out["host"]["sqlite"] = sqlite3.sqlite_version

raw = sqlite3.connect(FS)
out["checks"]["integrity_check"] = raw.execute("PRAGMA integrity_check").fetchone()[0]
raw.close()

with Aloelite(FS) as fs:
    vols = fs.list_volumes()
    out["checks"]["volumes"] = [f"{v.name} ({v.id})" for v in vols]
    try:
        fs.mount("demo")
        out["checks"]["missing PIN"] = "FAIL: accepted"
    except errs.EncryptionRequired:
        out["checks"]["missing PIN"] = "rejected ok"
    with fs.mount("demo", pin=PIN) as m:
        a = m.read_all("/docs/hello.txt")
        b = m.read_all("/docs/notes/copy.txt")
        assert a == payload, "hello.txt bytes differ from what wasm wrote"
        assert b == payload, "copy.txt bytes differ from what wasm wrote"
        out["checks"]["read_all"] = f"both files byte-identical ({len(a)}B each)"
        meta = m.stat("/docs/hello.txt").metadata
        assert meta == {"author": "wasm", "lang": "en"}, meta
        out["checks"]["metadata via shim jsonb -> native read"] = meta
        out["checks"]["listing"] = {
            "/": sorted(e.name for e in m.list("/")),
            "/docs": sorted(e.name for e in m.list("/docs")),
            "/docs/notes": sorted(e.name for e in m.list("/docs/notes")),
        }
        # and write BACK from native, so the round trip is two-directional
        m.put("/docs/from-host.txt", b"written by native CPython")
        assert m.read_all("/docs/from-host.txt") == b"written by native CPython"
        out["checks"]["native write-back"] = "ok"
    out["checks"]["health_check"] = f"{len(fs.health_check())} anomalies"

print(json.dumps(out, indent=2))
