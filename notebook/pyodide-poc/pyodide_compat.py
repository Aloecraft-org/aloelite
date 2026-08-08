# Pyodide compatibility shim for aloelite: NOT an engine change.
#
# Pyodide 314.0.3 bundles SQLite 3.39.0 (2022-07). aloelite's capability
# probe requires jsonb() (3.45) and unixepoch('subsec') (3.42). Audit of
# schema.sql + sql-templates.yaml shows those are the ONLY two post-3.39
# constructs used (the `->` operator, json(), and window functions all
# exist in 3.39), so two per-connection user-defined functions close the
# gap. UDFs registered on a connection are visible to triggers and views,
# so the schema's INSTEAD OF id-minting and touch triggers work unchanged.
#
#   jsonb(x)             -> x  (metadata stored as JSON TEXT, not JSONB.
#                          Every SQLite JSON function and operator accepts
#                          text JSON, so files written this way open
#                          identically under a modern native sqlite -- the
#                          host cross-check in this POC proves it.)
#   unixepoch('subsec')  -> time.time()  (float seconds, ms precision --
#                          same value contract as the builtin)
#
# A production Pyodide deployment would prefer a rebuilt _sqlite3 with a
# current SQLite amalgamation; this shim is the zero-toolchain route.
import json
import sqlite3
import time

# --- JSONB (sqlite >= 3.45 binary JSON) encode/decode ------------------------
# node.metadata is BLOB in a STRICT table, so jsonb() must yield real JSONB
# bytes: a native sqlite >= 3.45 interprets any BLOB fed to a JSON function as
# JSONB and rejects anything else. Format per sqlite/src/json.c: each element
# is a header byte (low nibble = type, high nibble = payload size, with
# 12/13/14 meaning a 1/2/4-byte big-endian size follows) then the payload.
_NULL, _TRUE, _FALSE, _INT, _FLOAT, _TEXTRAW, _ARRAY, _OBJECT = 0, 1, 2, 3, 5, 10, 11, 12
_TEXT_KINDS = (7, 8, 9, 10)


def _hdr(etype: int, n: int) -> bytes:
    if n <= 11:
        return bytes([(n << 4) | etype])
    if n <= 0xFF:
        return bytes([(12 << 4) | etype, n])
    if n <= 0xFFFF:
        return bytes([(13 << 4) | etype]) + n.to_bytes(2, "big")
    return bytes([(14 << 4) | etype]) + n.to_bytes(4, "big")


def _jsonb_enc(v) -> bytes:
    if v is None:
        return _hdr(_NULL, 0)
    if v is True:
        return _hdr(_TRUE, 0)
    if v is False:
        return _hdr(_FALSE, 0)
    if isinstance(v, int):
        p = str(v).encode()
        return _hdr(_INT, len(p)) + p
    if isinstance(v, float):
        p = json.dumps(v).encode()  # rejects NaN/Infinity like SQL json() does
        return _hdr(_FLOAT, len(p)) + p
    if isinstance(v, str):
        p = v.encode()
        return _hdr(_TEXTRAW, len(p)) + p
    if isinstance(v, list):
        p = b"".join(_jsonb_enc(x) for x in v)
        return _hdr(_ARRAY, len(p)) + p
    if isinstance(v, dict):
        p = b"".join(_jsonb_enc(str(k)) + _jsonb_enc(x) for k, x in v.items())
        return _hdr(_OBJECT, len(p)) + p
    raise TypeError(f"unencodable {type(v)}")


def _jsonb_dec_one(b: bytes, i: int):
    """Decode the element at offset i -> (value, next_offset)."""
    h = b[i]
    etype, size = h & 0x0F, h >> 4
    i += 1
    if size == 12:
        size, i = b[i], i + 1
    elif size == 13:
        size, i = int.from_bytes(b[i : i + 2], "big"), i + 2
    elif size == 14:
        size, i = int.from_bytes(b[i : i + 4], "big"), i + 4
    elif size == 15:
        size, i = int.from_bytes(b[i : i + 8], "big"), i + 8
    payload, end = b[i : i + size], i + size
    if etype == _NULL:
        return None, end
    if etype == _TRUE:
        return True, end
    if etype == _FALSE:
        return False, end
    if etype in (3, 4):  # INT / INT5
        return int(payload.decode(), 0 if etype == 4 else 10), end
    if etype in (5, 6):  # FLOAT / FLOAT5
        return float(payload.decode()), end
    if etype in (7, 10):  # TEXT / TEXTRAW: no escapes / raw utf8
        return payload.decode(), end
    if etype in (8, 9):  # TEXTJ / TEXT5: JSON-escaped
        return json.loads('"' + payload.decode() + '"'), end
    if etype == _ARRAY:
        out, j = [], 0
        while j < size:
            v, nj = _jsonb_dec_one(payload, j)
            out.append(v)
            j = nj
        return out, end
    if etype == _OBJECT:
        out, j = {}, 0
        while j < size:
            k, j = _jsonb_dec_one(payload, j)
            v, j = _jsonb_dec_one(payload, j)
            out[k] = v
        return out, end
    raise ValueError(f"bad jsonb element type {etype}")


def _sql_jsonb(x):
    """jsonb(X): JSON text, number, or JSONB blob -> JSONB blob."""
    if x is None:
        return None
    if isinstance(x, bytes):
        _jsonb_dec_one(x, 0)  # validate
        return x
    if isinstance(x, (int, float)):  # numeric SQL values are JSON numbers
        return _jsonb_enc(x)
    return _jsonb_enc(json.loads(x))


def _sql_json(x):
    """json(X): JSON text, number, or JSONB blob -> canonical JSON text."""
    if x is None:
        return None
    if isinstance(x, bytes):
        v, _ = _jsonb_dec_one(x, 0)
    elif isinstance(x, (int, float)):
        v = x
    else:
        v = json.loads(x)
    return json.dumps(v, separators=(",", ":"), ensure_ascii=False)


def _unixepoch(modifier):
    if modifier == "subsec":
        return time.time()
    return None  # mirror the builtin: unknown modifier -> NULL


def _install_on(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.create_function("jsonb", 1, _sql_jsonb, deterministic=True)
    conn.create_function("json", 1, _sql_json, deterministic=True)
    conn.create_function("unixepoch", 1, _unixepoch)
    return conn


_real_connect = sqlite3.connect


def _connect(*args, **kwargs):
    return _install_on(_real_connect(*args, **kwargs))


def install() -> str:
    """Route every new sqlite3 connection through the UDF installer."""
    if sqlite3.connect is not _connect:
        sqlite3.connect = _connect
    return (
        f"sqlite {sqlite3.sqlite_version}: shimmed jsonb() + unixepoch('subsec') "
        f"via per-connection UDFs"
    )
