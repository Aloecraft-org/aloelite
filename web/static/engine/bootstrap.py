# Pyodide-side glue for the web viewer. Runs INSIDE the wasm runtime.
#
# Design: one JSON entry point + one bytes channel, so the JS adapter never
# manages PyProxies. `call(op, args_json, data)` returns a JSON envelope
# string ({"ok": ...} or {"err": {code, message}}); operations that produce
# bytes park them and the adapter collects with `take_bytes()`, which builds
# the Uint8Array on the Python side (no proxy crosses back to JS).
#
# Error mapping: aloelite's FsError subclass names snake_case exactly onto
# mount-api.yaml's closed error codes (NotAContainer -> not_a_container,
# BadKey -> bad_key, ...). Anything that doesn't land in the closed set maps
# to the adapter-level 'fs_error' with the message preserved.
#
# The engine source is expected at /repo, this file's directory on sys.path
# (for pyodide_compat), and a writable /work for volume files.
import json
import re
import sys

for p in ("/repo", "/app"):
    if p not in sys.path:
        sys.path.insert(0, p)

# sqlite capability shim: only when this runtime's sqlite actually lacks the
# builtins (see pyodide_compat.py; self-retires once Pyodide ships >= 3.45).
import sqlite3 as _sq  # noqa: E402

try:
    _sq.connect(":memory:").execute("SELECT jsonb(1)")
except _sq.OperationalError:
    import pyodide_compat  # noqa: E402

    pyodide_compat.install()

import aloelite.errors as aloe_errors  # noqa: E402
from aloelite import Aloelite  # noqa: E402

# mount-api.yaml's closed set; used to validate the derived mapping.
_CLOSED = {
    "not_found", "not_a_container", "not_an_entry", "nameless", "would_cycle",
    "volume_mismatch", "not_empty", "mount_invalid", "mount_point_archived",
    "lock_held", "lock_invalid", "corrupt", "unsupported", "container_exists",
    "bad_key", "encryption_required",
}


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _error_code(exc: BaseException) -> str:
    code = _snake(type(exc).__name__)
    return code if code in _CLOSED else "fs_error"


_sessions: dict[int, dict] = {}
_mounts: dict[int, dict] = {}
_next_id = 0
_pending_bytes: bytes = b""


def _nid() -> int:
    global _next_id
    _next_id += 1
    return _next_id


def _session(a: dict) -> dict:
    s = _sessions.get(a["session"])
    if s is None:
        raise aloe_errors.MountInvalid("session is closed")
    return s


def _mount(a: dict) -> dict:
    m = _mounts.get(a["mount"])
    if m is None:
        raise aloe_errors.MountInvalid("mount is closed")
    return m


def _writable(m: dict) -> None:
    if m["read_only"]:
        raise _ReadOnly("this volume was opened read-only")


class _ReadOnly(Exception):
    pass


def _volumes(fs: Aloelite) -> list[dict]:
    out = []
    for v in fs.list_volumes():
        crypto = fs.db.one("resolution.get_volume_crypto", {"volume": v.id})
        enc_mode = crypto["enc_mode"] if crypto is not None else "none"
        out.append(
            {
                "id": v.id,
                "name": v.name,
                "encMode": enc_mode,
                "encrypted": enc_mode != "none",
                "createdAt": v.created_at,
            }
        )
    return out


def _entry_row(e) -> dict:
    return {"name": e.name, "type": e.type.value, "node": e.node}


def _stat_row(st) -> dict:
    return {
        "id": st.id,
        "type": st.type.value,
        "name": st.name,
        "size": st.size,
        "createdAt": st.created_at,
        "modifiedAt": st.modified_at,
        "metadata": st.metadata,
    }


# --- operations ---------------------------------------------------------------
def _register_session(fs: Aloelite, path: str, build) -> dict:
    """Register fs under a new session id, closing the connection if the rest
    of session construction fails (a leaked open connection pins the MEMFS
    file in the wasm heap with no recovery path)."""
    try:
        result = build(fs)
    except BaseException:
        fs.close()
        raise
    sid = _nid()
    _sessions[sid] = {"fs": fs, "path": path, "mounts": set()}
    return {"session": sid, **result}


def _quarantine_untrusted(path: str) -> None:
    """Strip attacker-defined derived objects BEFORE the engine touches the
    file. db.py's era-refresh drops+recreates triggers/views only when the
    file's era is OLDER than the installed schema; a hostile file stamped
    with the current era keeps its own definitions (IF NOT EXISTS), and those
    triggers would fire on the open/mount writes. Dropping everything and
    zeroing the era forces the engine's own refresh path to rebuild every
    derived object from the INSTALLED schema. (Also filed as an engine-level
    hardening candidate in web/HANDOFF.md — this is the viewer-side seal.)"""
    c = _sq.connect(path)
    try:
        derived = c.execute(
            "SELECT type, name FROM sqlite_master WHERE type IN ('trigger', 'view')"
        ).fetchall()
        quoted = lambda n: n.replace('"', '""')  # noqa: E731
        for typ, name in derived:
            if typ == "trigger":
                c.execute(f'DROP TRIGGER IF EXISTS "{quoted(name)}"')
        for typ, name in derived:
            if typ == "view":
                c.execute(f'DROP VIEW IF EXISTS "{quoted(name)}"')
        c.execute("PRAGMA user_version = 0")
        c.commit()
    finally:
        c.close()


def _op_open_file(a: dict) -> dict:
    _quarantine_untrusted(a["path"])
    return _register_session(
        Aloelite(a["path"]), a["path"], lambda fs: {"volumes": _volumes(fs)}
    )


def _op_create_scratch(a: dict) -> dict:
    path = a.get("path", "/work/scratch.fs")
    pin = a.get("pin")

    def build(fs):
        fs.create_volume(a.get("name") or "scratch", pin=pin.encode() if pin else None)
        return {"volumes": _volumes(fs)}

    return _register_session(Aloelite(path), path, build)


def _op_mount(a: dict) -> dict:
    s = _session(a)
    pin = a.get("pin")
    # Mount strictly BY ID. Aloelite.mount() resolves name-first, so a volume
    # whose NAME equals another volume's id string would silently win — with a
    # hostile or merely odd file the user would browse the wrong volume. The
    # viewer only ever addresses volumes by id, so mirror aloelite.py's
    # id branch directly against the ops layer.
    import aloelite.operations as ops
    from aloelite.aloelite import Mount as _Mount

    db = s["fs"].db
    if db.one("resolution.get_volume", {"volume": a["volume"]}) is None:
        raise aloe_errors.NotFound(f"no volume with id {a['volume']!r}")
    mid_engine = ops.mount(db, a["volume"], "/", None, pin.encode() if pin else None)
    sess = db.active_session
    token = sess["token"] if sess and sess.get("mount_id") == mid_engine else None
    m = _Mount(db, mid_engine, token=token)
    mid = _nid()
    _mounts[mid] = {
        "m": m,
        "session": a["session"],
        "read_only": bool(a.get("readOnly", True)),
        "volume": a["volume"],
    }
    s["mounts"].add(mid)
    return {"mount": mid, "readOnly": _mounts[mid]["read_only"]}


def _op_list(a: dict) -> list[dict]:
    rows = [_entry_row(e) for e in _mount(a)["m"].list(a["path"]) if e.visible]
    rows.sort(key=lambda r: (r["type"] != "container", r["name"].lower()))
    return rows


def _op_stat(a: dict) -> dict:
    return _stat_row(_mount(a)["m"].stat(a["path"]))


def _op_read_file(a: dict) -> dict:
    global _pending_bytes
    _pending_bytes = _mount(a)["m"].read_all(a["path"])
    return {"size": len(_pending_bytes)}


def _op_put_file(a: dict, data: bytes) -> dict:
    m = _mount(a)
    _writable(m)
    m["m"].put(a["path"], data)
    return {}


def _op_mkdir(a: dict) -> dict:
    m = _mount(a)
    _writable(m)
    m["m"].mkdir(a["path"], parents=True, exist_ok=True)
    return {}


def _op_remove(a: dict) -> dict:
    m = _mount(a)
    _writable(m)
    if a.get("recursive"):
        m["m"].remove_recursive(a["path"])
    else:
        m["m"].remove(a["path"])
    return {}


def _op_unmount(a: dict) -> dict:
    m = _mounts.pop(a["mount"], None)
    if m is not None:
        _sessions.get(m["session"], {"mounts": set()})["mounts"].discard(a["mount"])
        try:
            m["m"].unmount()
        except aloe_errors.FsError:
            pass  # already invalid is fine; the handle is gone either way
    return {}


def _op_export(a: dict) -> dict:
    global _pending_bytes
    s = _session(a)
    import os

    tmp = "/work/.export.tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    # VACUUM INTO writes a compact, consistent single-file snapshot without
    # closing the live connection (journal sidecars never travel).
    s["fs"].db.connection.execute("VACUUM INTO ?", (tmp,))
    with open(tmp, "rb") as f:
        _pending_bytes = f.read()
    os.remove(tmp)
    return {"size": len(_pending_bytes)}


def _op_close_session(a: dict) -> dict:
    # Teardown is best-effort and MUST reach the unlink step: MEMFS is this
    # tab's wasm heap, and a session whose close raised would otherwise pin
    # the volume's full size forever (the session id is already popped, so
    # there is no retry path).
    s = _sessions.pop(a["session"], None)
    if s is not None:
        try:
            for mid in list(s["mounts"]):
                _op_unmount({"mount": mid})
            s["fs"].close()
        finally:
            import os

            sidecars = ("", "-journal", "-wal", "-shm")
            for path in (s["path"] + suffix for suffix in sidecars):
                if os.path.exists(path):
                    os.remove(path)
    return {}


_OPS = {
    "open_file": _op_open_file,
    "create_scratch": _op_create_scratch,
    "mount": _op_mount,
    "list": _op_list,
    "stat": _op_stat,
    "read_file": _op_read_file,
    "put_file": _op_put_file,
    "mkdir": _op_mkdir,
    "remove": _op_remove,
    "unmount": _op_unmount,
    "export": _op_export,
    "close_session": _op_close_session,
}


def call(op: str, args_json: str, data=None) -> str:
    """The single JS entry point. Returns a JSON envelope string."""
    try:
        args = json.loads(args_json) if args_json else {}
        fn = _OPS[op]
        if op == "put_file":
            payload = bytes(data.to_py()) if data is not None else b""
            result = fn(args, payload)
        else:
            result = fn(args)
        return json.dumps({"ok": result})
    except _ReadOnly as e:
        return json.dumps({"err": {"code": "read_only", "message": str(e)}})
    except KeyError as e:
        msg = f"unknown op {e}"
        return json.dumps({"err": {"code": "unsupported", "message": msg}})
    except aloe_errors.FsError as e:
        return json.dumps({"err": {"code": _error_code(e), "message": str(e)}})
    except Exception as e:  # noqa: BLE001 — the envelope IS the error channel
        msg = f"{type(e).__name__}: {e}"
        return json.dumps({"err": {"code": "fs_error", "message": msg}})


def take_bytes():
    """Hand the parked bytes to JS as a real Uint8Array (built Python-side, so
    no PyProxy needs managing over there). The global is cleared BEFORE the JS
    allocation so an OOM there can't strand a huge payload in the parking slot."""
    global _pending_bytes
    import js

    payload, _pending_bytes = _pending_bytes, b""
    buf = js.Uint8Array.new(len(payload))
    buf.assign(payload)
    return buf
