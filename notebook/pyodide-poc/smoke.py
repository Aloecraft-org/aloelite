# aloelite-in-Pyodide smoke test.
# Runs UNMODIFIED aloelite source (mounted at /repo) inside the wasm runtime:
#   1. platform probes (sqlite capabilities, dep versions)
#   2. conformance format vectors (byte-exact crypto in wasm)
#   3. end-to-end engine: encrypted volume, multi-chunk roundtrip, dedup,
#      wrong-PIN rejection; the .fs file lands in /out for host cross-check.
import json
import os
import sys
import time

REPO = "/repo"
sys.path.insert(0, REPO)

report = {"probes": {}, "vectors": {}, "engine": {}, "ok": False}
P, RV, E = report["probes"], report["vectors"], report["engine"]

# --- 1. platform probes -----------------------------------------------------
P["python"] = sys.version.split()[0]
P["sys.platform"] = sys.platform

import sqlite3

P["sqlite"] = sqlite3.sqlite_version
_c = sqlite3.connect(":memory:")
try:
    _c.execute("SELECT jsonb(1)")
    P["jsonb (>=3.45)"] = "native"
except Exception:  # noqa: BLE001
    P["jsonb (>=3.45)"] = "MISSING"
P["unixepoch subsec (>=3.42)"] = (
    "native"
    if _c.execute("SELECT unixepoch('subsec') IS NOT NULL").fetchone()[0]
    else "MISSING"
)
_c.close()

if "MISSING" in (P["jsonb (>=3.45)"], P["unixepoch subsec (>=3.42)"]):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else "/poc")
    import pyodide_compat

    P["compat shim"] = pyodide_compat.install()

import cryptography

P["cryptography"] = cryptography.__version__
import msgpack

P["msgpack"] = getattr(msgpack, "__version__", ".".join(map(str, msgpack.version)))
import pydantic

P["pydantic"] = pydantic.VERSION
import yaml

P["pyyaml"] = yaml.__version__

from cryptography.hazmat.primitives.kdf.argon2 import Argon2id  # noqa: F401

P["argon2id import"] = "ok"

# --- 2. conformance format vectors ------------------------------------------
from aloelite import crypto as C
from aloelite.db import chunk_hash, split_chunks

V = json.load(open(f"{REPO}/conformance/vectors/format-v1.json"))
hx = bytes.fromhex

n = 0
for c in V["chunk_address"]:
    assert chunk_hash(hx(c["bytes"])) == c["chunk_hash"], c["name"]
    n += 1
RV["chunk_address"] = f"{n}/{n} ok"

n = 0
for c in V["chunk_split"]:
    got = [len(x) for x in split_chunks(b"\x00" * c["size"], c["chunk_size"])]
    assert got == c["lengths"]
    n += 1
RV["chunk_split"] = f"{n}/{n} ok"

for c in V["volume_hash"]:
    assert C.volume_hash(c["volume_id"], c["root_node_id"]).hex() == c["h_v"]
RV["volume_hash"] = "ok"

t0 = time.time()
for c in V["unlock_key"]:
    assert C.derive_unlock_key(c["pin_utf8"].encode(), hx(c["h_v"])).hex() == c["k_u"]
RV["unlock_key (Argon2id 64MiB t=3 p=4)"] = (
    f"{len(V['unlock_key'])}/{len(V['unlock_key'])} ok, "
    f"{(time.time() - t0) / len(V['unlock_key']):.2f}s per derive"
)

for c in V["chunk_key"]:
    cc = C.ChunkCipher(hx(c["volume_key"]), c["volume_id"])
    assert cc._k.hex() == c["k_chunk"]
RV["chunk_key"] = "ok"

n = 0
cc = C.ChunkCipher(hx(V["inputs"]["volume_key"]), V["inputs"]["volume_id"])
for c in V["encrypt_chunk_convergent"]:
    ct, nc, tag = cc.encrypt_chunk(hx(c["plaintext"]))
    assert (nc.hex(), ct.hex(), tag.hex(), chunk_hash(ct)) == (
        c["n_c"],
        c["ciphertext"],
        c["tag"],
        c["chunk_hash"],
    ), c["name"]
    n += 1
RV["encrypt_chunk_convergent"] = f"{n}/{n} ok (byte-identical ciphertext in wasm)"

for c in V["unwrap_volume_key"]:
    got = C.unwrap_volume_key(hx(c["k_u"]), hx(c["wrapped_key"]), hx(c["wrap_nonce"]))
    assert got.hex() == c["volume_key"]
RV["unwrap_volume_key"] = "ok"

for c in V["session_kek"]:
    assert C.session_kek(hx(c["token"]), hx(c["mount_nonce"])).hex() == c["kek"]
RV["session_kek"] = "ok"

# --- 3. end-to-end engine ---------------------------------------------------
import aloelite.errors as errs
from aloelite import Aloelite

FS_PATH = "/out/pyodide-demo.fs"
if os.path.exists(FS_PATH):
    os.remove(FS_PATH)
for sidecar in (FS_PATH + "-wal", FS_PATH + "-shm", FS_PATH + "-journal"):
    if os.path.exists(sidecar):
        os.remove(sidecar)

t0 = time.time()
PIN = b"1234"
payload = ("hello from wasm \N{SPARKLES} -- " * 8).encode()  # > chunk_size => multi-chunk
with Aloelite(FS_PATH) as fs:
    vinfo = fs.create_volume("demo", chunk_size=64, pin=PIN)
    E["volume"] = f"{vinfo.id} (encrypted, convergent, chunk_size=64)"
    with fs.mount("demo", pin=PIN) as m:
        m.mkdir("/docs/notes", parents=True)
        m.put("/docs/hello.txt", payload)
        assert m.read_all("/docs/hello.txt") == payload
        nchunks = -(-len(payload) // 64)
        E["roundtrip"] = f"{len(payload)}B across {nchunks} chunks ok"
        m.put("/docs/notes/copy.txt", payload)  # identical content => dedup
        m.put("/docs/notes/meta.txt", b"tiny")
        m.set_metadata("/docs/hello.txt", {"author": "wasm", "lang": "en"})
        st = m.stat("/docs/hello.txt")
        assert st.metadata == {"author": "wasm", "lang": "en"}
        E["stat"] = {"type": str(st.type), "size": st.size, "metadata": st.metadata}
        E["list /docs"] = sorted(e.name for e in m.list("/docs"))
        packed = m.pack("/docs/notes")  # msgpack subtree -> entry (same name)
        assert m.stat("/docs/notes").type.value == "entry"
        E["pack(msgpack subtree)"] = f"node {str(packed)[:13]}... ok"
        m.unpack("/docs/notes")
        assert m.read_all("/docs/notes/copy.txt") == payload
        E["unpack"] = "ok"

    try:
        fs.mount("demo", pin=b"9999")
        E["wrong PIN"] = "FAIL: accepted"
    except errs.BadKey:
        E["wrong PIN"] = "rejected (BadKey) ok"
    try:
        fs.mount("demo")
        E["missing PIN"] = "FAIL: accepted"
    except errs.EncryptionRequired:
        E["missing PIN"] = "rejected (EncryptionRequired) ok"

    pool = fs.db.connection.execute("SELECT count(*) FROM content_chunk").fetchone()[0]
    E["dedup"] = f"{pool} pool chunks for 2 identical multi-chunk files + 1 tiny + 1 pack"
    E["health_check"] = f"{len(fs.health_check())} anomalies"

E["fs file"] = f"{FS_PATH} ({os.path.getsize(FS_PATH)} bytes)"
E["elapsed"] = f"{time.time() - t0:.2f}s"
report["ok"] = True

print(json.dumps(report, indent=2))
json.dumps(report)
