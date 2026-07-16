# ./aloelite/operations.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
The flat Mount API function layer — the FFI surface.

Every operation is a module-level function over a Db (the current, transient
connection), a MountId (the durable identity that brokers access), and plain
values; it returns a Pydantic model or raises an FsError. No object state, no
ergonomic sugar — that lives in aloelite.py on top of this. One transaction per
action (Db.txn); each mutating op is atomic on its own.

The other three implementations mirror this file function-for-function, and the
conformance suite drives it. The two pieces with genuine host logic — path
resolution (resolve.py) and the copy/pack/unpack subtree walk (here) — are the
parts most worth pinning in conformance.

MsgPack pack format is a CROSS-IMPLEMENTATION CONTRACT (see _PACK_* below): all
four implementations must read and write it identically or a subtree packed on
one platform won't unpack on another.
"""

from __future__ import annotations

import json
import time
from typing import Any

import msgpack

from .db import Db, split_chunks
from .descriptor import Descriptor
from .errors import (
    BadKey,
    Corrupt,
    EncryptionRequired,
    LockHeld,
    NotAContainer,
    NotAnEntry,
    NotEmpty,
    NotFound,
    MountInvalid,
    WouldCycle,
)
from . import crypto
from .models import (
    Anomaly,
    ContentPruneReport,
    DirEntry,
    MountInfo,
    NodeInfo,
    PruneReport,
    VolumeInfo,
)
from .resolve import resolve, resolve_parent, split_path
from .types import (
    FdId,
    LockId,
    MountId,
    NodeId,
    NodeType,
    Timestamp,
    VolumeId,
    Whence,
    WriteMode,
)


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Mount precondition — every operation starts here
# ---------------------------------------------------------------------------
class _Mount:
    __slots__ = ("id", "mount_point", "volume")

    def __init__(self, id: MountId, mount_point: NodeId, volume: VolumeId) -> None:
        self.id = id
        self.mount_point = mount_point
        self.volume = volume


def _require_mount(db: Db, mount: MountId) -> _Mount:
    """Resolve a mount to its anchor + volume, or raise MountInvalid.

    A mount is untrusted-until-validated (or revalidated-per-access): it may have been unmounted or expired
    (possibly from another connection), so this runs as the first step of every
    operation. (ACC-1/4/5.)
    """
    row = db.one("resolution.get_valid_mount", {"mount": mount})
    if row is None:
        raise MountInvalid(mount=mount)
    return _Mount(mount, NodeId(row["mount_point"]), VolumeId(row["volume_id"]))


def _meta_to_json(metadata: dict[str, str] | None) -> str | None:
    """Host->SQL: a shallow {string:string} map serializes to a JSON string for
    jsonb() storage; None/empty stays NULL (NODE-6: NULL == empty map)."""
    if not metadata:
        return None
    return json.dumps(metadata, separators=(",", ":"), sort_keys=True)


def _new_node(
    db: Db,
    m: _Mount,
    *,
    type: NodeType,
    name: str,
    created_at: int | None = None,
    modified_at: int | None = None,
    metadata: dict[str, str] | None = None,
) -> NodeId:
    nid = db.create_monotonic(
        "mutation.create_node",
        "mutation.get_generated_node_id",
        {
            "type": type.value,
            "name": name,
            "created_at": created_at,
            "modified_at": modified_at,
            "volume": m.volume,
            "metadata": _meta_to_json(metadata),
        },
    )
    return NodeId(nid)


def _link(db: Db, m: _Mount, parent: NodeId, child: NodeId) -> str:
    return db.create_monotonic(
        "mutation.create_edge",
        "mutation.get_generated_edge_id",
        {"from_id": parent, "to_id": child, "volume": m.volume},
    )


def _put_initial_content(db: Db, m: _Mount, node: NodeId, data: bytes) -> None:
    """Establish an entry's content at birth via INSERTs (create_content +
    staged chunks). Used by create/pack/unpack — never bumps modified_at. Empty
    data => committed version 0 with zero chunks; non-empty => version 1 staged.
    """
    version = 1 if data else 0
    db.run(
        "mutation.create_content",
        {"node": node, "version": version, "size": len(data), "hash": None},
    )
    if data:
        db.stage_chunks(node, version, m.volume, data)


def _require_name(name: str) -> str:
    from .errors import Nameless

    if not name:
        raise Nameless()
    return name


# ===========================================================================
# Session / lifecycle
# ===========================================================================
def create_volume(
    db: Db,
    name: str | None = None,
    chunk_size: int = 1048576,
    pin: bytes | None = None,
    *,
    enc_mode: str = "convergent",
) -> VolumeInfo:
    """Create a volume and its root container, linked, in one transaction.

    chunk_size (CV-1) is fixed here and immutable thereafter; it defaults to
    1 MiB and is stored on the volume row, read back on every content write.

    If `pin` is given the volume is encrypted: a random volume key K_v is
    generated and sealed under K_u = Argon2id(pin, H_v) where
    H_v = SHA256(volume_id || root_node_id). The wrapped key + nonce land on the
    volume row; the PIN itself is never stored. `enc_mode` selects 'convergent'
    (default; dedup preserved) or 'random' (dedup sacrificed, zero equality
    leakage). Without a pin the volume is 'none' (unencrypted) and behaves
    exactly as before.
    """
    mode = enc_mode if pin is not None else "none"
    with db.txn():
        vid = db.gen_id()
        db.run(
            "mutation.create_volume",
            {"id": vid, "name": name, "chunk_size": chunk_size, "enc_mode": mode},
        )
        # the root container is created with the volume so monotonic ids work
        root = db.create_monotonic(
            "mutation.create_node",
            "mutation.get_generated_node_id",
            {
                "type": "container",
                "name": "/",
                "created_at": None,
                "modified_at": None,
                "volume": vid,
                "metadata": None,
            },
        )
        db.rowcount("mutation.link_root", {"volume": vid, "root": root})
        if pin is not None:
            # H_v needs the root id, so seal after link_root, still in this txn.
            h_v = crypto.volume_hash(vid, root)
            k_u = crypto.derive_unlock_key(pin, h_v)
            k_v = crypto.new_volume_key()
            wrapped, wrap_nonce = crypto.wrap_volume_key(k_u, k_v)
            db.rowcount(
                "mutation.update_volume_crypto",
                {
                    "volume": vid,
                    "enc_mode": mode,
                    "wrapped_key": wrapped,
                    "wrap_nonce": wrap_nonce,
                },
            )
    return VolumeInfo.from_row(db.one("resolution.get_volume", {"volume": vid}))


def list_volumes(db: Db) -> list[VolumeInfo]:
    return [VolumeInfo.from_row(r) for r in db.all("resolution.list_volumes")]


def mount(
    db: Db,
    volume: VolumeId,
    at: str = "/",
    ttl_ms: int | None = None,
    pin: bytes | None = None,
) -> MountId:
    """Open a mount on a volume, anchored at `at` (relative to the
    volume root). Returns the durable MountId.

    For an encrypted volume a `pin` is required: it derives K_u, unwraps the
    volume key K_v, installs the chunk cipher on this connection for the life of
    the mount, and mints a per-mount token T plus a memory-only sealed mount
    secret (so subsequent re-attach within the process needs only T + N_m, not
    the PIN). The token is exposed via db.active_session['token'] (the wrapper
    surfaces it as Mount.token). A wrong PIN raises BadKey; a PIN on an
    unencrypted volume (or none on an encrypted one) raises EncryptionRequired.
    """
    with db.txn():
        crow = db.one("resolution.get_volume_crypto", {"volume": volume})
        if crow is None or crow["root_node_id"] is None:
            raise NotFound("volume or its root not found", volume=volume)
        root = NodeId(crow["root_node_id"])
        anchor = resolve(db, root, at).node  # mount point within the volume
        expires = _now_ms() + ttl_ms if ttl_ms is not None else None
        mid = db.gen_id()
        n_m = crypto.new_mount_nonce()
        db.run(
            "mutation.create_mount",
            {
                "id": mid,
                "volume": volume,
                "mount_point": anchor,
                "expires_at": expires,
                "n_m": n_m,
            },
        )

        enc_mode = crow["enc_mode"]
        if enc_mode == "none":
            if pin is not None:
                raise EncryptionRequired("volume is not encrypted but a pin was given")
            db.cipher = crypto.IdentityCipher()
            db.active_session = None
        else:
            if pin is None:
                raise EncryptionRequired("volume is encrypted; a pin is required")
            h_v = crypto.volume_hash(str(volume), str(root))
            k_u = crypto.derive_unlock_key(pin, h_v)
            try:
                k_v = crypto.unwrap_volume_key(
                    k_u, crow["wrapped_key"], crow["wrap_nonce"]
                )
            except Exception:
                raise BadKey("pin did not unlock the volume", volume=volume)
            db.cipher = crypto.ChunkCipher(
                k_v, str(volume), convergent=(enc_mode == "convergent")
            )
            token = crypto.new_token()
            mount_secret, sess_nonce = crypto.seal_mount_secret(token, n_m, k_v)
            db.active_session = {
                "mount_id": mid,
                "token": token,
                "n_m": n_m,
                "mount_secret": mount_secret,
                "sess_nonce": sess_nonce,
                "enc_mode": enc_mode,
                "volume": str(volume),
            }
    return MountId(mid)


def unmount(db: Db, mount: MountId) -> None:
    """Mark the mount invalid. Lock reclamation is deferred to prune (ACC-10).
    Tears down the session cipher: K_v and the sealed mount secret leave memory,    
    so post-unmount operations fall back to the identity (unencrypted) cipher.
    """
    with db.txn():
        db.rowcount("mutation.set_mount_state", {"mount": mount, "state": "unmounted"})
    if db.active_session is not None and db.active_session.get("mount_id") == mount:
        db.active_session = None
        db.cipher = crypto.IdentityCipher()


def renew_mount(db: Db, mount: MountId, ttl_ms: int | None = None) -> MountInfo:
    with db.txn():
        m = _require_mount(db, mount)
        expires = _now_ms() + ttl_ms if ttl_ms is not None else None
        db.rowcount("mutation.renew_mount", {"mount": mount, "expires_at": expires})
    return mount_info(db, mount)


def mount_info(db: Db, mount: MountId) -> MountInfo:
    row = db.one("resolution.get_mount", {"mount": mount})
    if row is None:
        raise NotFound(mount=mount)
    mount_path = _abs_path(db, NodeId(row["mount_point"]))
    return MountInfo.from_row(row, mount_path=mount_path)


def list_mounts(
    db: Db,
    volume: VolumeId | None = None,
    *,
    include_unmounted: bool = False,
) -> "builtins.list[MountInfo]":
    """List durable mounts (ACC-1a), optionally scoped to one volume.

    Connection-scoped like list_volumes — takes no MountId, because its whole
    purpose is to surface mounts before you hold a valid one. Reads the raw
    mount table (not valid_mount), so expired mounts appear; retired
    ('unmounted') rows are excluded unless include_unmounted. A mount whose
    mount point no longer resolves (removed/archived anchor, ACC-5) is
    returned with mount_path=None rather than aborting the listing.
    """
    out = []
    rows = db.all(
        "resolution.list_mounts",
        {"volume": volume, "include_unmounted": 1 if include_unmounted else 0},
    )
    for r in rows:
        try:
            mount_path = _abs_path(db, NodeId(r["mount_point"]))
        except (NotFound, Corrupt):
            mount_path = None
        out.append(MountInfo.from_row(r, mount_path=mount_path))
    return out


# ===========================================================================
# Path / ancestor helpers (host-side, over the path_of template)
# ===========================================================================
def _abs_path(db: Db, node: NodeId) -> str:
    """Volume-absolute path of a node (root = '/'). Used for mount_path."""
    return _build_path(db, node, stop_at=None)


def _build_path(db: Db, node: NodeId, stop_at: NodeId | None) -> str:
    rows = db.all("resolution.path_of", {"node": node})
    if not rows:
        raise NotFound(node=node)
    if not rows[-1]["is_root"]:
        # walk hit the depth bound or a null-parent without reaching a root
        raise Corrupt("ancestor chain does not reach a volume root", node=node)
    names: list[str] = []
    reached = stop_at is None
    for r in rows:
        if stop_at is not None and r["node_id"] == stop_at:
            reached = True
            break
        if stop_at is None and r["is_root"]:
            break  # root is '/', don't include its name
        names.append(r["name"])
    if not reached:
        raise NotFound("node is not under the given anchor", node=node)
    names.reverse()
    return "/" + "/".join(names)


# ===========================================================================
# Read / resolution
# ===========================================================================
def stat(db: Db, mount: MountId, path: str) -> NodeInfo:
    m = _require_mount(db, mount)
    node = resolve(db, m.mount_point, path).node
    return stat_by_id(db, mount, node)


def stat_by_id(db: Db, mount: MountId, node: NodeId) -> NodeInfo:
    _require_mount(db, mount)
    row = db.one("resolution.get_node", {"node": node})
    if row is None:
        raise NotFound(node=node)
    return NodeInfo.from_row(row)


def exists(db: Db, mount: MountId, path: str) -> bool:
    m = _require_mount(db, mount)
    try:
        resolve(db, m.mount_point, path)
        return True
    except (NotFound, NotAContainer):
        # a missing segment, or descending through a non-container, both mean
        # the path does not resolve to anything.
        return False


def list(db: Db, mount: MountId, path: str = "/") -> "builtins.list[DirEntry]":
    m = _require_mount(db, mount)
    found = resolve(db, m.mount_point, path)
    if found.type is not NodeType.CONTAINER:
        raise NotAContainer(node=found.node)
    # Stamp the normalized listing path as each entry's fetch context.
    cwd = "/" + "/".join(split_path(path))
    return [
        DirEntry.from_row(r, current_directory=cwd)
        for r in db.all("resolution.list_children", {"container": found.node})
    ]


def read_all(db: Db, mount: MountId, path: str) -> bytes:
    m = _require_mount(db, mount)
    found = resolve(db, m.mount_point, path)
    if found.type is not NodeType.ENTRY:
        raise NotAnEntry(node=found.node)
    return db.read_content_bytes(found.node)


def path_of(db: Db, mount: MountId, node: NodeId) -> str:
    """Mount-relative path of a node ('/' = the mount point)."""
    m = _require_mount(db, mount)
    return _build_path(db, node, stop_at=m.mount_point)


# ===========================================================================
# Structural
# ===========================================================================
def create_container(db: Db, mount: MountId, path: str) -> NodeId:
    with db.txn():
        m = _require_mount(db, mount)
        parent, name = resolve_parent(db, m.mount_point, path)
        _require_name(name)
        node = _new_node(db, m, type=NodeType.CONTAINER, name=name)
        _link(db, m, parent, node)
    return node


def _create_entry_internal(
    db: Db, mount: MountId, path: str, data: bytes | None = None
) -> NodeId:
    m = _require_mount(db, mount)
    parent, name = resolve_parent(db, m.mount_point, path)
    _require_name(name)
    node = _new_node(db, m, type=NodeType.ENTRY, name=name)
    # Establish content at creation. Uniform chunking: empty => version 0 / zero
    # chunks; with data => version 1 staged. create_content + stage_chunks are
    # INSERTs, so they do NOT bump modified_at (a fresh file keeps
    # modified_at == created_at).
    _put_initial_content(db, m, node, data or b"")
    _link(db, m, parent, node)
    return node


def create_entry(
    db: Db, mount: MountId, path: str, data: bytes | None = None
) -> NodeId:
    with db.txn():
        return _create_entry_internal(db, mount, path, data)


def write_all(db: Db, mount: MountId, path: str, data: bytes) -> None:
    with db.txn():
        m = _require_mount(db, mount)
        found = resolve(db, m.mount_point, path)
        if found.type is not NodeType.ENTRY:
            raise NotAnEntry(node=found.node)
        if db.scalar(
            "validation.check_lock_held", {"node": found.node, "mount": mount}
        ):
            raise LockHeld(node=found.node)
        # Allocate the next version, stage its chunks, and swap the committed
        # pointer — all inside this single op transaction, so the whole replace
        # is atomic (IO-2r1). A new write produces NEW chunks/hashes and never
        # mutates a pooled chunk in place (CV-2).
        version = db.alloc_version(found.node)
        size = db.stage_chunks(found.node, version, m.volume, data)
        db.rowcount(
            "mutation.update_content",
            {"node": found.node, "version": version, "size": size, "hash": None},
        )

def append(db: Db, mount: MountId, path: str, data: bytes) -> int:
    """Atomically append `data` to an entry and return the new size.

    Bounded-memory and bounded-cost: the prior version's full leading chunks are
    carried forward BY REFERENCE (copy_chunk_refs_range — no data copy), and only
    the old partial final chunk + the new bytes are re-chunked and staged. Each
    call is its own atomic commit (one new version, committed-pointer swap), which
    is what makes it safe to call repeatedly for append-mostly files (logs, mbox):
    every commit is immediately visible to readers and to /export, and a crash
    loses only the uncommitted in-flight batch, never prior commits.

    Cost per call is O(full_chunk_count) ref-copies (a single INSERT..SELECT) plus
    O(len(partial_tail) + len(data)) staging. With the 1 MiB default chunk size and
    FUSE-side batching this is cheap for logs and per-message mail files. Each call
    creates a new version; run prune_content (or set_retention keep=N) periodically
    on hot append files to bound manifest growth.
    """
    if not data:
        m = _require_mount(db, mount)
        found = resolve(db, m.mount_point, path)
        if found.type is not NodeType.ENTRY:
            raise NotAnEntry(node=found.node)
        meta = db.read_content_meta(found.node)
        return meta[1] if meta is not None else 0
    with db.txn():
        m = _require_mount(db, mount)
        found = resolve(db, m.mount_point, path)
        if found.type is not NodeType.ENTRY:
            raise NotAnEntry(node=found.node)
        if db.scalar("validation.check_lock_held", {"node": found.node, "mount": mount}):
            raise LockHeld(node=found.node)
        cs = db.chunk_size_of(m.volume)
        meta = db.read_content_meta(found.node)
        src_version, size = meta if meta is not None else (0, 0)
        new_version = db.alloc_version(found.node)
        full = size // cs
        partial = size % cs
        # carry the unchanged full leading chunks forward by reference
        if full > 0:
            db.run("mutation.copy_chunk_refs_range", {
                "node": found.node, "dst_version": new_version,
                "src_version": src_version, "lo": 0, "hi": full - 1,
            })
        # rebuild from the old partial tail + new data, re-chunked from index `full`
        tail = b""
        if partial > 0:
            rows = db.read_chunk_range(found.node, src_version, full, full)
            tail = rows[0][1] if rows else b""
        index = full
        for chunk in split_chunks(tail + data, cs):
            db.stage_chunk(found.node, new_version, index, chunk)
            index += 1
        new_size = size + len(data)
        db.rowcount("mutation.update_content", {
            "node": found.node, "version": new_version, "size": new_size, "hash": None,
        })
    return new_size

def write_range(db: Db, mount: MountId, path: str, offset: int, data: bytes) -> int:
    """Atomically overwrite bytes [offset, offset+len(data)) of an entry,
    extending it (zero-filling any gap) when the range passes EOF. Returns
    the new size.

    Bounded memory and bounded cost: full chunks strictly before the touched
    window are carried into the new version BY REFERENCE (prefix), as are
    full chunks strictly after it (suffix — chunk alignment is preserved
    because an in-place overwrite never shifts bytes). Only the window is
    read, patched, re-chunked, and staged. The window extends down to the
    chunk containing the prior EOF when writing at/past it, so a short final
    chunk is always rebuilt, preserving the uniform-chunks-except-last
    invariant the ranged reader depends on.

    Each call is one atomic commit = one new content version. Hot
    random-write entries should carry set_retention(keep=N) and see periodic
    prune_content (CV-6/CV-7) to bound manifest growth.
    """
    if offset < 0:
        raise ValueError("negative offset")
    if not data:
        m = _require_mount(db, mount)
        found = resolve(db, m.mount_point, path)
        if found.type is not NodeType.ENTRY:
            raise NotAnEntry(node=found.node)
        meta = db.read_content_meta(found.node)
        return meta[1] if meta is not None else 0
    with db.txn():
        m = _require_mount(db, mount)
        found = resolve(db, m.mount_point, path)
        if found.type is not NodeType.ENTRY:
            raise NotAnEntry(node=found.node)
        if db.scalar("validation.check_lock_held", {"node": found.node, "mount": mount}):
            raise LockHeld(node=found.node)
        cs = db.chunk_size_of(m.volume)
        meta = db.read_content_meta(found.node)
        src_version, size = meta if meta is not None else (0, 0)
        end = offset + len(data)
        new_size = max(size, end)
        new_version = db.alloc_version(found.node)

        lo = offset // cs
        hi = (end - 1) // cs
        # The window reaches down to the chunk containing the prior EOF when
        # writing at/past it (rebuild a short final chunk; zero-fill a gap).
        window_lo = min(lo, size // cs)
        src_last = (size - 1) // cs if size > 0 else -1

        if window_lo > 0:
            db.run("mutation.copy_chunk_refs_range", {
                "node": found.node, "dst_version": new_version,
                "src_version": src_version, "lo": 0, "hi": window_lo - 1,
            })
        if src_last > hi:
            db.run("mutation.copy_chunk_refs_range", {
                "node": found.node, "dst_version": new_version,
                "src_version": src_version, "lo": hi + 1, "hi": src_last,
            })

        base = window_lo * cs
        buf = bytearray()
        win_src_hi = min(hi, src_last)
        if win_src_hi >= window_lo:
            rows = db.read_chunk_range(found.node, src_version, window_lo, win_src_hi)
            buf = bytearray(b"".join(d for _, d in rows))
        if offset - base > len(buf):
            buf.extend(b"\x00" * (offset - base - len(buf)))
        buf[offset - base : end - base] = data
        index = window_lo
        for chunk in split_chunks(bytes(buf), cs):
            db.stage_chunk(found.node, new_version, index, chunk)
            index += 1
        db.rowcount("mutation.update_content", {
            "node": found.node, "version": new_version, "size": new_size, "hash": None,
        })
    return new_size


def truncate(db: Db, mount: MountId, path: str, size: int) -> None:
    """Atomically set an entry's size. Shrink carries full leading chunks by
    reference and trims the boundary chunk; grow zero-fills (rebuilding only
    the prior short final chunk). One new version per call; no-op when the
    size is unchanged."""
    if size < 0:
        raise ValueError("negative size")
    with db.txn():
        m = _require_mount(db, mount)
        found = resolve(db, m.mount_point, path)
        if found.type is not NodeType.ENTRY:
            raise NotAnEntry(node=found.node)
        if db.scalar("validation.check_lock_held", {"node": found.node, "mount": mount}):
            raise LockHeld(node=found.node)
        cs = db.chunk_size_of(m.volume)
        meta = db.read_content_meta(found.node)
        src_version, cur = meta if meta is not None else (0, 0)
        if size == cur:
            return
        new_version = db.alloc_version(found.node)
        if size < cur:
            full = size // cs
            if full > 0:
                db.run("mutation.copy_chunk_refs_range", {
                    "node": found.node, "dst_version": new_version,
                    "src_version": src_version, "lo": 0, "hi": full - 1,
                })
            rem = size % cs
            if rem:
                rows = db.read_chunk_range(found.node, src_version, full, full)
                db.stage_chunk(found.node, new_version, full, rows[0][1][:rem])
        else:
            full = cur // cs
            if full > 0:
                db.run("mutation.copy_chunk_refs_range", {
                    "node": found.node, "dst_version": new_version,
                    "src_version": src_version, "lo": 0, "hi": full - 1,
                })
            tail = b""
            if cur % cs:
                rows = db.read_chunk_range(found.node, src_version, full, full)
                tail = rows[0][1] if rows else b""
            pad = tail + b"\x00" * (size - full * cs - len(tail))
            index = full
            for chunk in split_chunks(pad, cs):
                db.stage_chunk(found.node, new_version, index, chunk)
                index += 1
        db.rowcount("mutation.update_content", {
            "node": found.node, "version": new_version, "size": size, "hash": None,
        })


def rename(db: Db, mount: MountId, path: str, name: str) -> None:
    with db.txn():
        m = _require_mount(db, mount)
        _require_name(name)
        node = resolve(db, m.mount_point, path).node
        db.rowcount("mutation.rename_node", {"node": node, "name": name})


def set_metadata(db: Db, mount: MountId, path: str, metadata: dict[str, str]) -> None:
    """Replace a node's metadata map wholesale (NODE-6). Applies to containers
    and entries alike. Does NOT bump modified_at (metadata is annotation, not
    content). Pass {} to clear.
    """
    with db.txn():
        m = _require_mount(db, mount)
        node = resolve(db, m.mount_point, path).node
        db.rowcount(
            "mutation.set_metadata",
            {"node": node, "metadata": _meta_to_json(metadata)},
        )


def move(db: Db, mount: MountId, src: str, dst: str) -> None:
    """Move src to dst. dst is the full destination path (its final segment is
    the new name); move = archive old edge + create new edge (+ rename if the
    final name differs).
    """
    with db.txn():
        m = _require_mount(db, mount)
        found = resolve(db, m.mount_point, src)
        node, ntype = found.node, found.type
        parent, new_name = resolve_parent(db, m.mount_point, dst)
        _require_name(new_name)
        if ntype is NodeType.CONTAINER:
            if db.scalar(
                "validation.check_cycle", {"moving": node, "new_parent": parent}
            ):
                raise WouldCycle(moving=node, new_parent=parent)
        old = db.one("resolution.get_active_parent", {"node": node})
        if old is None:
            raise NotFound("node has no active placement to move", node=node)
        db.rowcount("mutation.archive_edge", {"edge": old["edge_id"]})
        _link(db, m, parent, node)
        cur = db.one("resolution.get_node", {"node": node})
        if cur is not None and cur["name"] != new_name:
            db.rowcount("mutation.rename_node", {"node": node, "name": new_name})


def remove(db: Db, mount: MountId, path: str) -> None:
    """Detach a leaf or empty container (archive its active edge). Refuses a
    non-empty container with NotEmpty (use remove_recursive)."""
    with db.txn():
        m = _require_mount(db, mount)
        found = resolve(db, m.mount_point, path)
        if found.type is NodeType.CONTAINER:
            if db.scalar("validation.check_empty", {"container": found.node}):
                raise NotEmpty(node=found.node)
        old = db.one("resolution.get_active_parent", {"node": found.node})
        if old is None:
            raise NotFound("node has no active placement", node=found.node)
        db.rowcount("mutation.archive_edge", {"edge": old["edge_id"]})


def remove_recursive(db: Db, mount: MountId, path: str) -> None:
    """Archive a whole active subtree in one statement (set-based)."""
    with db.txn():
        m = _require_mount(db, mount)
        node = resolve(db, m.mount_point, path).node
        db.rowcount("recursive.archive_subtree", {"root": node})


# ---------------------------------------------------------------------------
# The shared copy/pack/unpack subtree walk
#
# enumerate_subtree gives rows TOP-DOWN in canonical order (depth, edge_id,
# node_id) over ACTIVE edges only. A parent always precedes its children, so a
# single forward pass threading an old->new id map suffices: a child attaches to
# the new id its parent already received. This walk order is a CONFORMANCE
# REQUIREMENT — all four implementations must walk it identically so new-id
# sequences (and future merkle hashes) match.
# ---------------------------------------------------------------------------
def copy(db: Db, mount: MountId, src: str, dst: str) -> NodeId:
    with db.txn():
        m = _require_mount(db, mount)
        src_node = resolve(db, m.mount_point, src).node
        dst_parent, dst_name = resolve_parent(db, m.mount_point, dst)
        _require_name(dst_name)
        rows = db.all("recursive.enumerate_subtree", {"root": src_node})
        idmap: dict[str, NodeId] = {}
        new_root: NodeId | None = None
        for r in rows:
            info = db.one("resolution.get_node", {"node": r["node_id"]})
            is_root = r["parent_id"] is None
            name = dst_name if is_root else info["name"]
            parent = dst_parent if is_root else idmap[r["parent_id"]]
            ntype = NodeType(info["type"])
            # get_node returns metadata as a JSON string; _new_node re-serializes
            # from a dict, so parse here (NULL => {}). OP-4r1: metadata preserved.
            src_meta = (
                json.loads(info["metadata"]) if info["metadata"] is not None else {}
            )
            new_id = _new_node(
                db,
                m,
                type=ntype,
                name=name,
                created_at=info["created_at"],  # OP-4: preserve
                modified_at=info["modified_at"],
                metadata=src_meta,
            )
            idmap[r["node_id"]] = new_id
            if ntype is NodeType.ENTRY:
                # Dedup-preserving copy: re-reference the source's committed
                # version's chunks (immutable, shared) rather than re-hashing.
                src_meta = db.one("resolution.get_content_meta", {"node": r["node_id"]})
                sv = src_meta["version"] if src_meta else 0
                ssize = src_meta["size"] if src_meta else 0
                db.run(
                    "mutation.create_content",
                    {"node": new_id, "version": sv, "size": ssize, "hash": None},
                )
                if sv > 0:
                    db.run(
                        "mutation.copy_chunk_refs",
                        {
                            "dst": new_id,
                            "dst_version": sv,
                            "src": r["node_id"],
                            "src_version": sv,
                        },
                    )
            _link(db, m, parent, new_id)
            if is_root:
                new_root = new_id
        if new_root is None:
            raise Corrupt("empty subtree enumeration", node=src_node)
    return new_root


# --- MsgPack pack format (CROSS-IMPLEMENTATION CONTRACT) --------------------
# A packed subtree is a msgpack map:
#   { "fmt": "aloefs.pack", "ver": 1, "nodes": [ <node>, ... ] }
# nodes are in TOP-DOWN canonical order (parents before children); each node:
#   { "p": <parent index or -1 for the root>,
#     "t": "container" | "entry",
#     "n": <name>, "c": <created_at>, "m": <modified_at>,
#     "d": <payload bytes>  (entries only; omitted/None for containers) }
_PACK_FMT = "aloefs.pack"
_PACK_VER = 1


def pack(db: Db, mount: MountId, path: str) -> NodeId:
    """Consolidate a container's subtree into a single packed entry that
    supersedes the original placement (OP-6)."""
    with db.txn():
        m = _require_mount(db, mount)
        found = resolve(db, m.mount_point, path)
        if found.type is not NodeType.CONTAINER:
            raise NotAContainer(node=found.node)
        node = found.node
        placement = db.one("resolution.get_active_parent", {"node": node})
        if placement is None:
            raise NotFound("cannot pack a node with no active placement", node=node)
        parent = NodeId(placement["parent_id"])
        cont = db.one("resolution.get_node", {"node": node})
        pack_name = cont["name"]

        rows = db.all("recursive.enumerate_subtree", {"root": node})
        index: dict[str, int] = {}
        packed_nodes: list[dict[str, Any]] = []
        for r in rows:
            info = db.one("resolution.get_node", {"node": r["node_id"]})
            i = len(packed_nodes)
            index[r["node_id"]] = i
            entry: dict[str, Any] = {
                "p": -1 if r["parent_id"] is None else index[r["parent_id"]],
                "t": info["type"],
                "n": info["name"],
                "c": info["created_at"],
                "m": info["modified_at"],
            }
            # NODE-6: carry metadata when present (key "x"); omit for the common
            # empty case to keep the blob small. Stored in the pack as a map.
            meta = json.loads(info["metadata"]) if info["metadata"] is not None else {}
            if meta:
                entry["x"] = meta
            if info["type"] == NodeType.ENTRY.value:
                entry["d"] = db.read_content_bytes(r["node_id"])
            packed_nodes.append(entry)

        blob = msgpack.packb(
            {"fmt": _PACK_FMT, "ver": _PACK_VER, "nodes": packed_nodes},
            use_bin_type=True,
        )
        # supersede: archive the original subtree, then place the packed entry.
        # The blob flows through the chunker like any payload (Option A), so the
        # blob-size ceiling disappears for free.
        db.rowcount("recursive.archive_subtree", {"root": node})
        packed = _new_node(db, m, type=NodeType.ENTRY, name=pack_name)
        _put_initial_content(db, m, packed, blob)
        _link(db, m, parent, packed)
    return packed


def unpack(db: Db, mount: MountId, path: str) -> None:
    """Restore a packed entry's subtree, superseding the packed entry (OP-7)."""
    with db.txn():
        m = _require_mount(db, mount)
        found = resolve(db, m.mount_point, path)
        if found.type is not NodeType.ENTRY:
            raise NotAnEntry(node=found.node)
        node = found.node
        blob = db.read_content_bytes(node)
        doc = msgpack.unpackb(blob, raw=False)
        if not isinstance(doc, dict) or doc.get("fmt") != _PACK_FMT:
            raise Corrupt("not an aloefs pack blob", node=node)

        placement = db.one("resolution.get_active_parent", {"node": node})
        if placement is None:
            raise NotFound("packed entry has no active placement", node=node)
        parent = NodeId(placement["parent_id"])

        db.rowcount("mutation.archive_edge", {"edge": placement["edge_id"]})
        idmap: dict[int, NodeId] = {}
        for i, pn in enumerate(doc["nodes"]):
            ntype = NodeType(pn["t"])
            target_parent = parent if pn["p"] == -1 else idmap[pn["p"]]
            # NODE-6: tolerant read — a blob written before metadata existed has
            # no "x" key, which restores as an empty map.
            new_id = _new_node(
                db,
                m,
                type=ntype,
                name=pn["n"],
                created_at=pn.get("c"),
                modified_at=pn.get("m"),
                metadata=pn.get("x"),
            )
            idmap[i] = new_id
            if ntype is NodeType.ENTRY:
                _put_initial_content(db, m, new_id, pn.get("d") or b"")
            _link(db, m, target_parent, new_id)


# ===========================================================================
# Streaming
# ===========================================================================
def open_read(db: Db, mount: MountId, path: str) -> Descriptor:
    m = _require_mount(db, mount)
    found = resolve(db, m.mount_point, path)
    if found.type is not NodeType.ENTRY:
        raise NotAnEntry(node=found.node)
    meta = db.read_content_meta(found.node)
    version, size = meta if meta is not None else (0, 0)
    return Descriptor(
        db,
        found.node,
        FdId(db.gen_id()),
        writable=False,
        volume=m.volume,
        chunk_size=db.chunk_size_of(m.volume),
        version=version,
        size=size,
    )


def open_write(
    db: Db, mount: MountId, path: str, mode: WriteMode = WriteMode.TRUNCATE
) -> Descriptor:
    with db.txn():
        m = _require_mount(db, mount)
        try:
            # Atomic resolution check inside the isolated transaction boundary
            found = resolve(db, m.mount_point, path)
            if found.type is not NodeType.ENTRY:
                raise NotAnEntry(node=found.node)
            node_id = found.node
        except NotFound:
            if mode is WriteMode.APPEND:
                raise
            # Safe creation within the isolated block (pass the MountId, not
            # the resolved _Mount — _create_entry_internal re-validates it)
            node_id = _create_entry_internal(db, mount, path)
        if db.scalar(
            "validation.check_lock_held", {"node": node_id, "mount": mount}
        ):
            raise LockHeld(node=node_id)
        lock = db.gen_id()
        db.run(
            "mutation.create_lock",
            {"id": lock, "mount": mount, "node": node_id, "expires_at": None},
        )
        cs = db.chunk_size_of(m.volume)
        # Append carries the prior version's FULL leading chunks forward
        # unchanged and rebuilds only from the partial final chunk; truncate
        # starts empty. Both stream chunk-by-chunk with bounded memory.
        carry_src = carry_full = 0
        pending = b""
        position = 0
        if mode is WriteMode.APPEND:
            meta = db.read_content_meta(node_id)
            src_version, size = meta if meta is not None else (0, 0)
            carry_src = src_version
            if size > 0 and size % cs != 0:
                carry_full = size // cs  # full leading chunks
                tail = db.read_chunk_range(
                    node_id, src_version, carry_full, carry_full
                )
                pending = tail[0][1] if tail else b""  # partial last chunk -> rebuild
            else:
                carry_full = size // cs  # all chunks full (or empty)
            position = size
    return Descriptor(
        db,
        node_id,
        FdId(db.gen_id()),
        writable=True,
        lock=LockId(lock),
        volume=m.volume,
        chunk_size=cs,
        carry_src=carry_src,
        carry_full=carry_full,
        pending=pending,
        position=position,
    )


# ===========================================================================
# Maintenance
# ===========================================================================
def prune(db: Db, volume: VolumeId | None = None) -> PruneReport:
    with db.txn():
        locks = db.rowcount("maintenance.prune_locks")
        nodes = db.rowcount("maintenance.prune_nodes", {"volume": volume})
    return PruneReport(nodes_pruned=nodes, locks_pruned=locks)


def set_retention(db: Db, mount: MountId, path: str, keep: int | None) -> None:
    """Set an entry's single content-retention policy (CV-6). `keep` = keep the
    last N versions (committed always retained); None = keep all superseded
    versions. Enforced only by prune_content, never by the write path.
    """
    with db.txn():
        m = _require_mount(db, mount)
        found = resolve(db, m.mount_point, path)
        if found.type is not NodeType.ENTRY:
            raise NotAnEntry(node=found.node)
        db.rowcount("mutation.set_retention_keep", {"node": found.node, "keep": keep})


def prune_content(db: Db, volume: VolumeId | None = None) -> ContentPruneReport:
    """Reclaim unreferenced content (CV-7), distinct from `prune`. Runs after any
    node prune: drops manifest rows beyond each entry's retention policy (and
    aborted writes above the committed pointer), then sweeps pool chunks no
    retained version references. Retained versions are resolved BEFORE any chunk
    is collected (the retained_version view), and the committed version always
    survives.
    """
    with db.txn():
        versions = db.rowcount("maintenance.prune_content_versions", {"volume": volume})
        chunks = db.rowcount("maintenance.prune_content_chunks")
    return ContentPruneReport(versions_pruned=versions, chunks_pruned=chunks)


def health_check(db: Db) -> "builtins.list[Anomaly]":
    return [Anomaly.from_row(r) for r in db.all("maintenance.health_check")]


import builtins  # noqa: E402  (used only in return annotations above)

__all__ = [
    "create_volume",
    "list_volumes",
    "mount",
    "unmount",
    "renew_mount",
    "mount_info",
    "list_mounts",
    "stat",
    "stat_by_id",
    "exists",
    "list",
    "read_all",
    "path_of",
    "create_container",
    "create_entry",
    "write_all",
    "write_range",
    "truncate",
    "rename",
    "set_metadata",
    "move",
    "remove",
    "remove_recursive",
    "copy",
    "pack",
    "unpack",
    "open_read",
    "open_write",
    "prune",
    "set_retention",
    "prune_content",
    "health_check",
]
# Copyright Michael Godfrey 2026 | aloecraft.org <michael@aloecraft.org>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
