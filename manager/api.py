# ./manager/api.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
manager.api — the nine-endpoint HTTP API.

`create_app(store, supervisor, ...)` returns a Flask app. The app talks to the
metadata store and the mount supervisor through their interfaces only; volume
creation uses Aloelite (imported lazily so this module imports without the
aloelite package, e.g. for unit tests with fakes). Checkpoint/export open the
backing SQLite file *directly* (stdlib sqlite3), independent of any live FUSE
mount — that is what lets a backup run while the volume is mounted.

Endpoints:
  POST   /volumes                 create (no mount)
  DELETE /volumes/<id>            delete (unmount first if needed)
  GET    /volumes                 list with mount status
  POST   /volumes/<id>/mount      mount
  DELETE /volumes/<id>/mount      unmount
  GET    /volumes/<id>/mount      mount status
  GET    /volumes/<id>/stat       backing-file metadata (cheap poll target)
  GET    /volumes/<id>/export     checkpoint + stream the .sqlite
  POST   /volumes/<id>/checkpoint wal_checkpoint(TRUNCATE)
  GET    /admin                   admin panel
"""

from __future__ import annotations

import mimetypes
import os
import shutil
import sqlite3
import time
import uuid

from flask import Flask, Response, jsonify, redirect, request, render_template, send_file

from . import errors as merr
from .direct import FRONTEND_DIRECT, DirectSessionRegistry
from .store import FilesystemRecord, VolumeRecord, VolumeStore

def _default_root() -> str:
    if os.environ.get("ALOELITE_ROOT"):
        return os.environ["ALOELITE_ROOT"]
    if os.environ.get("ALOELITE_DIRECT_ONLY", "") not in ("", "0"):
        return os.path.expanduser("~/.aloelite")
    return "/aloelite-root"

ALOELITE_ROOT = _default_root()
HOST_MNT_PREFIX = "/mnt/aloelite"  # host-visible path consumers bind-mount
_STREAM_CHUNK = 1 << 20


# --- direct-SQLite helpers (independent of FUSE) ---------------------------
def _wal_checkpoint_truncate(sqlite_path: str) -> tuple[int, int]:
    """Run PRAGMA wal_checkpoint(TRUNCATE) on the backing file directly.
    Returns (frames_checkpointed, frames_remaining)."""
    con = sqlite3.connect(sqlite_path, timeout=5.0)
    try:
        con.execute("PRAGMA busy_timeout=5000")
        busy, log, checkpointed = con.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).fetchone()
        remaining = max(int(log) - int(checkpointed), 0) if busy == 0 else int(log)
        return int(checkpointed), remaining
    finally:
        con.close()


def create_app(
    store: VolumeStore,
    supervisor,
    *,
    registry: DirectSessionRegistry | None = None,
    aloelite_root: str = ALOELITE_ROOT,
    host_mnt_prefix: str = HOST_MNT_PREFIX,
) -> Flask:
    app = Flask(__name__)
    registry = registry or DirectSessionRegistry()
    app.config["DIRECT_REGISTRY"] = registry  # reachable for shutdown/tests

    def _host_path(volume_id: str) -> str:
        return f"{host_mnt_prefix.rstrip('/')}/{volume_id}"

    def _sqlite_path(fs_id: str) -> str:
        return os.path.join(aloelite_root, f"{fs_id}.sqlite")

    def _require(volume_id: str) -> VolumeRecord | None:
        return store.get(volume_id)

    # -- POST /volumes ------------------------------------------------------
    @app.post("/volumes")
    def create_volume():
        body = request.get_json(silent=True) or {}
        name = body.get("name")
        encrypted = bool(body.get("encrypted", False))
        pin = body.get("pin")
        if not name:
            return jsonify(error="name is required"), 400
        if encrypted and not pin:
            return jsonify(error="pin is required when encrypted is true"), 400
        if not encrypted and pin:
            return jsonify(error="pin must be omitted when encrypted is false"), 400

        vid = uuid.uuid4().hex
        fs_id = body.get("fs_id")
        new_fs = fs_id is None
        if new_fs:
            fs_id = uuid.uuid4().hex
            sqlite_path = _sqlite_path(fs_id)
        else:
            fsr = store.get_fs(fs_id)
            if fsr is None:
                return jsonify(error="no such filesystem"), 404
            if any(v.name == name for v in store.volumes_of(fs_id)):
                return jsonify(error="a volume with that name exists here"), 409
            sqlite_path = fsr.sqlite_path

        from aloelite.aloelite import Aloelite  # lazy

        with Aloelite(sqlite_path) as fs:
            fs.create_volume(
                name, pin=pin.encode() if pin else None, ensure_unique=True
            )

        now = time.time()
        if new_fs:
            store.put_fs(
                FilesystemRecord(
                    id=fs_id, display_name=name, sqlite_path=sqlite_path, created_at=now
                )
            )
        rec = VolumeRecord(
            id=vid,
            name=name,
            fs_id=fs_id,
            encrypted=encrypted,
            created_at=now,
            mounted=False,
            mountpoint=None,
        )
        store.put(rec)
        return jsonify(id=vid, name=name, encrypted=encrypted, mounted=False), 201

    # -- DELETE /volumes/<id> ----------------------------------------------
    @app.delete("/volumes/<vid>")
    def delete_volume(vid):
        rec = _require(vid)
        if rec is None:
            return jsonify(error="no such volume"), 404
        if rec.mounted:
            try:
                if rec.frontend == FRONTEND_DIRECT:
                    registry.lock(rec)
                else:
                    supervisor.unmount(rec)
            except merr.NotMounted:
                pass  # store said mounted but session already gone; proceed
            rec = _require(vid) or rec
        sqlite_path = store.sqlite_path_of(rec)
        store.delete(vid)
        if not store.volumes_of(rec.fs_id):
            # last volume in the file: retire the filesystem record + file
            store.delete_fs(rec.fs_id)
            for path in (sqlite_path, sqlite_path + "-wal", sqlite_path + "-shm"):
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
        return "", 204

    # -- GET /volumes -------------------------------------------------------
    def _volume_item(rec: VolumeRecord) -> dict:
        item = {
            "id": rec.id,
            "name": rec.name,
            "fs_id": rec.fs_id,
            "encrypted": rec.encrypted,
            "mounted": rec.mounted,
            "frontend": rec.frontend,
        }
        if rec.mounted:
            item["mountpoint"] = rec.mountpoint
        item["auto_mount"] = rec.auto_mount
        return item

    @app.get("/volumes")
    def list_volumes():
        return jsonify([_volume_item(rec) for rec in store.list()]), 200

    # -- POST /volumes/<id>/mount ------------------------------------------
    @app.post("/volumes/<vid>/mount")
    def mount_volume(vid):
        rec = _require(vid)
        if rec is None:
            return jsonify(error="no such volume"), 404
        body = request.get_json(silent=True) or {}
        pin = body.get("pin")
        pin_bytes = pin.encode() if pin else None

        if body.get("mode") == "direct":
            if body.get("persist"):
                return jsonify(
                    error="persist is not supported for direct sessions "
                    "(they end with the manager process)"
                ), 400
            try:
                registry.unlock(rec, pin_bytes, store.sqlite_path_of(rec))
            except merr.AlreadyMounted:
                return jsonify(error="already mounted"), 409
            except (merr.BadPin, merr.EncryptionMismatch) as e:
                return jsonify(error=str(e) or e.__class__.__name__), 400
            except merr.MountFailed as e:
                return jsonify(error=str(e) or "unlock failed"), 500
            rec.mounted = True
            rec.mountpoint = None
            rec.frontend = FRONTEND_DIRECT
            store.put(rec)
            return jsonify(id=vid, frontend=FRONTEND_DIRECT), 200

        mount_name = body.get("mount_name") or rec.id
        try:
            mountpoint = supervisor.mount(rec, pin_bytes, mp_path=mount_name)
        except merr.AlreadyMounted:
            return jsonify(error="already mounted"), 409
        except merr.MountTimeout:
            return jsonify(error="mount readiness check timed out"), 503
        except (merr.BadPin, merr.EncryptionMismatch) as e:
            return jsonify(error=str(e) or e.__class__.__name__), 400
        except merr.MountFailed as e:
            return jsonify(error=str(e) or "mount failed"), 500

        rec.mounted = True
        rec.mountpoint = mountpoint
        rec.frontend = "fuse"
        if body.get("persist"):
            if rec.encrypted and not (body.get("pin_env") or body.get("pin_file")):
                # roll back nothing: the mount succeeded; just refuse to persist
                store.put(rec)
                return jsonify(
                    error="persist on an encrypted volume needs pin_env or pin_file"
                ), 400
            rec.auto_mount = True
            rec.mount_name = body.get("mount_name")
            rec.pin_env = body.get("pin_env")
            rec.pin_file = body.get("pin_file")
        store.put(rec)
        return jsonify(
            id=vid, mountpoint=mountpoint, host_path=_host_path(mount_name)
        ), 200

    # -- DELETE /volumes/<id>/mount ----------------------------------------
    @app.delete("/volumes/<vid>/mount")
    def unmount_volume(vid):
        rec = _require(vid)
        if rec is None:
            return jsonify(error="no such volume"), 404
        try:
            if rec.frontend == FRONTEND_DIRECT:
                registry.lock(rec)
            else:
                supervisor.unmount(rec)
        except merr.NotMounted:
            return jsonify(error="not mounted"), 404
        rec.mounted = False
        rec.mountpoint = None
        rec.frontend = None
        rec.auto_mount = False
        store.put(rec)
        return "", 204

    # -- GET /volumes/<id>/mount -------------------------------------------
    @app.get("/volumes/<vid>/mount")
    def mount_status(vid):
        rec = _require(vid)
        if rec is None:
            return jsonify(error="no such volume"), 404
        if rec.frontend == FRONTEND_DIRECT:
            ready = registry.is_unlocked(rec.id)
        else:
            ready = bool(rec.mounted and supervisor.is_active(rec.mountpoint))
        return jsonify(
            id=vid,
            mounted=rec.mounted,
            mountpoint=rec.mountpoint,
            frontend=rec.frontend,
            ready=ready,
        ), 200

    # -- GET /volumes/<id>/stat --------------------------------------------
    @app.get("/volumes/<vid>/stat")
    def stat_volume(vid):
        rec = _require(vid)
        if rec is None:
            return jsonify(error="no such volume"), 404
        try:
            st = os.stat(store.sqlite_path_of(rec))
        except OSError as e:
            return jsonify(error=f"cannot stat backing file: {e}"), 500
        return jsonify(
            id=vid,
            name=rec.name,
            size_bytes=st.st_size,
            mtime=st.st_mtime,
            mounted=rec.mounted,
        ), 200

    # -- POST /volumes/<id>/checkpoint -------------------------------------
    @app.post("/volumes/<vid>/checkpoint")
    def checkpoint_volume(vid):
        rec = _require(vid)
        if rec is None:
            return jsonify(error="no such volume"), 404
        checkpointed, remaining = _wal_checkpoint_truncate(store.sqlite_path_of(rec))
        if remaining:
            app.logger.warning("checkpoint %s left %d WAL frames", vid, remaining)
        return jsonify(
            id=vid, wal_frames_checkpointed=checkpointed, wal_frames_remaining=remaining
        ), 200

    # -- export (shared streamer) ------------------------------------------
    def _export_response(sqlite_path: str, filename: str, log_key: str):
        _checkpointed, remaining = _wal_checkpoint_truncate(sqlite_path)
        if remaining:
            app.logger.warning(
                "export %s checkpoint left %d WAL frames", log_key, remaining
            )
        # Open + fstat the fd so Content-Length matches exactly what we stream,
        # even if writers append a new WAL after the checkpoint (WAL mode does
        # not touch the main file until the next checkpoint, so the first
        # `size` bytes remain a coherent snapshot).
        fd = os.open(sqlite_path, os.O_RDONLY)
        size = os.fstat(fd).st_size

        def generate():
            try:
                remaining_bytes = size
                while remaining_bytes > 0:
                    chunk = os.read(fd, min(_STREAM_CHUNK, remaining_bytes))
                    if not chunk:
                        break
                    remaining_bytes -= len(chunk)
                    yield chunk
            finally:
                os.close(fd)

        return Response(
            generate(),
            mimetype="application/octet-stream",
            headers={
                "Content-Length": str(size),
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )

    def _export_name(display_name: str) -> str:
        # display_name is the "take it with you" name; ensure an extension and
        # strip anything path-like or quote-breaking.
        name = os.path.basename(display_name.replace("\\", "/")).replace('"', "")
        if not name:
            name = "aloelite"
        if "." not in name:
            name += ".sqlite"
        return name

    # -- GET /volumes/<id>/export (kept as an alias of the filesystem export)
    @app.get("/volumes/<vid>/export")
    def export_volume(vid):
        rec = _require(vid)
        if rec is None:
            return jsonify(error="no such volume"), 404
        fsr = store.get_fs(rec.fs_id)
        return _export_response(
            store.sqlite_path_of(rec), _export_name(fsr.display_name), vid
        )

    # -- GET /volumes/<id>/mounts --------------------------------------------
    @app.get("/volumes/<vid>/mounts")
    def list_engine_mounts(vid):
        """Durable engine mounts inside the backing file (ACC-1a), distinct
        from the manager's FUSE session state. Opens its own connection, so it
        works whether or not the volume is FUSE-mounted, and needs no PIN
        (mount rows are metadata, not encrypted content). ?all=1 includes
        retired rows."""
        rec = _require(vid)
        if rec is None:
            return jsonify(error="no such volume"), 404
        include = request.args.get("all") in ("1", "true")

        from aloelite.aloelite import Aloelite  # lazy

        with Aloelite(store.sqlite_path_of(rec)) as fs:
            names = {v.id: v.name for v in fs.list_volumes()}
            mounts = fs.list_mounts(include_unmounted=include)
        out = [
            {
                "id": m.id,
                "volume": m.volume,
                "label": f"{names.get(m.volume) or m.volume[:8]}:{m.mount_path or '?'}",
                "mount_path": m.mount_path,
                "state": m.state.value,
                "expires_at": m.expires_at,
                "created_at": m.created_at,
            }
            for m in mounts
        ]
        return jsonify(out), 200

    # -- file explorer (over the live FUSE mountpoint) -----------------------
    #
    # These operate on the volume's *mounted* directory tree, so they work for
    # plain and encrypted volumes alike (the FUSE session already holds the
    # key) and exercise exactly the path consumers see. A volume must be
    # mounted to browse it.
    def _files_root(rec: VolumeRecord) -> str | None:
        if not rec.mounted or not rec.mountpoint:
            return None
        return rec.mountpoint.rstrip("/") or "/"

    def _safe_join(root: str, rel: str | None) -> str | None:
        """Join a client-supplied volume-relative path under root; refuse any
        traversal escape ('..')."""
        parts = [
            p for p in (rel or "/").replace("\\", "/").split("/") if p and p != "."
        ]
        if ".." in parts:
            return None
        return os.path.join(root, *parts) if parts else root

    # -- direct-mode file explorer (over the held Mount session) -------------
    #
    # Same URL surface and response shapes as the FUSE branch, but every op
    # goes through registry.session(...) -> the Mount API. Paths are the
    # volume-relative strings the UI already sends; resolve() treats '..' as
    # an ordinary (missing) name, so traversal safety is inherent. Engine
    # mtimes are ms epoch; the UI expects seconds.
    def _direct_call(fn):
        from aloelite import errors as aerr  # lazy (mirrors other aloelite use)

        try:
            return fn()
        except merr.NotMounted:
            return jsonify(error="volume is not unlocked"), 409
        except aerr.NotFound:
            return jsonify(error="not found"), 404
        except aerr.ContainerExists:
            return jsonify(error="already exists"), 409
        except aerr.NotAContainer:
            return jsonify(error="not a directory"), 404
        except aerr.NotAnEntry:
            return jsonify(error="not a file"), 404
        except aerr.LockHeld:
            return jsonify(error="file is locked by another writer"), 423
        except aerr.FsError as e:
            return jsonify(error=f"{e.code}: {e}"), 500

    def _req_path() -> str:
        return request.args.get("path") or "/"

    def _direct_list(rec: VolumeRecord):
        def run():
            with registry.session(rec.id) as m:
                out = []
                for e in m.list(_req_path()):
                    if not e.visible:
                        continue
                    st = m.stat_by_id(e.node)
                    is_dir = e.type.value == "container"
                    out.append(
                        {
                            "name": e.name,
                            "type": "dir" if is_dir else "file",
                            "size": 0 if is_dir else (st.size or 0),
                            "mtime": st.modified_at / 1000.0,
                        }
                    )
                return jsonify(out), 200

        return _direct_call(run)

    def _direct_download(rec: VolumeRecord):
        from contextlib import ExitStack

        path = _req_path()

        def run():
            # Hold the session for the whole transfer: size and descriptor are
            # taken under one lock acquisition, so headers match the bytes.
            # (Serializes other ops on this volume for the duration — known
            # cost for now.) Werkzeug closes the generator on disconnect, so
            # the stack always unwinds.
            stack = ExitStack()
            m = stack.enter_context(registry.session(rec.id))
            try:
                size = m.stat(path).size or 0
                r = stack.enter_context(m.open_read(path))
            except BaseException:
                stack.close()
                raise

            def generate():
                try:
                    remaining = size
                    while remaining > 0:
                        chunk = r.read(min(_STREAM_CHUNK, remaining))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                        yield chunk
                finally:
                    stack.close()

            name = path.rstrip("/").rsplit("/", 1)[-1] or rec.id
            inline = request.args.get("inline") in ("1", "true")
            mt = (mimetypes.guess_type(name)[0] if inline else None) \
                or "application/octet-stream"
            disp = "inline" if inline else "attachment"
            return Response(
                generate(),
                mimetype=mt,
                headers={
                    "Content-Length": str(size),
                    "Content-Disposition": f'{disp}; filename="{name}"',
                },
            )

        return _direct_call(run)

    def _direct_upload(rec: VolumeRecord):
        f = request.files.get("file")
        if f is None or not f.filename:
            return jsonify(error="multipart field 'file' is required"), 400
        name = os.path.basename(f.filename.replace("\\", "/"))
        if not name or name in (".", ".."):
            return jsonify(error="bad filename"), 400
        base = _req_path().rstrip("/")
        dst = f"{base}/{name}"

        def run():
            with registry.session(rec.id) as m:
                # TRUNCATE creates a missing entry; sequential writes ride the
                # engine's bounded-memory streaming path.
                with m.open_write(dst) as w:
                    while chunk := f.stream.read(_STREAM_CHUNK):
                        w.write(chunk)
            return jsonify(name=name), 201

        return _direct_call(run)

    def _direct_mkdir(rec: VolumeRecord):
        path = _req_path()
        if path == "/":
            return jsonify(error="path is required"), 400

        def run():
            with registry.session(rec.id) as m:
                m.mkdir(path, parents=True)  # parents matches os.makedirs
            return jsonify(path=path), 201

        return _direct_call(run)

    def _direct_delete(rec: VolumeRecord):
        path = _req_path()
        if path == "/":
            return jsonify(error="refusing to delete the volume root"), 400

        def run():
            with registry.session(rec.id) as m:
                if m.stat(path).type.value == "container":
                    m.remove_recursive(path)
                else:
                    m.remove(path)
            return "", 204

        return _direct_call(run)

    def _files_ctx(vid: str, *, want: str | None = None):
        """Resolve (record, root, abs-path) for a files request, or an error
        response tuple. `want` = 'dir'|'file' adds an existence/type check."""
        rec = _require(vid)
        if rec is None:
            return None, (jsonify(error="no such volume"), 404)
        root = _files_root(rec)
        if root is None:
            return None, (jsonify(error="volume is not mounted"), 409)
        p = _safe_join(root, request.args.get("path"))
        if p is None:
            return None, (jsonify(error="bad path"), 400)
        if want == "dir" and not os.path.isdir(p):
            return None, (jsonify(error="not a directory"), 404)
        if want == "file" and not os.path.isfile(p):
            return None, (jsonify(error="not a file"), 404)
        return (rec, root, p), None

    @app.get("/volumes/<vid>/files")
    def list_files(vid):
        rec = _require(vid)
        if rec is not None and rec.frontend == FRONTEND_DIRECT:
            return _direct_list(rec)
        ctx, err = _files_ctx(vid, want="dir")
        if err:
            return err
        _rec, _root, p = ctx
        out = []
        for name in sorted(os.listdir(p)):
            fp = os.path.join(p, name)
            try:
                st = os.stat(fp)
            except OSError:
                continue  # raced with a concurrent delete
            out.append(
                {
                    "name": name,
                    "type": "dir" if os.path.isdir(fp) else "file",
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                }
            )
        return jsonify(out), 200

    @app.get("/volumes/<vid>/files/download")
    def download_file(vid):
        rec = _require(vid)
        if rec is not None and rec.frontend == FRONTEND_DIRECT:
            return _direct_download(rec)
        ctx, err = _files_ctx(vid, want="file")
        if err:
            return err
        _rec, _root, p = ctx
        inline = request.args.get("inline") in ("1", "true")
        return send_file(
            p, as_attachment=not inline, download_name=os.path.basename(p)
        )

    @app.post("/volumes/<vid>/files/upload")
    def upload_file(vid):
        # multipart form: field 'file'; ?path= is the target directory
        rec = _require(vid)
        if rec is not None and rec.frontend == FRONTEND_DIRECT:
            return _direct_upload(rec)
        ctx, err = _files_ctx(vid, want="dir")
        if err:
            return err
        _rec, _root, p = ctx
        f = request.files.get("file")
        if f is None or not f.filename:
            return jsonify(error="multipart field 'file' is required"), 400
        name = os.path.basename(f.filename.replace("\\", "/"))
        if not name or name in (".", ".."):
            return jsonify(error="bad filename"), 400
        # werkzeug streams to disk in chunks; the sequential write rides the
        # FUSE streaming fast path (bounded memory for large uploads)
        f.save(os.path.join(p, name))
        return jsonify(name=name), 201

    @app.post("/volumes/<vid>/files/mkdir")
    def mkdir_files(vid):
        rec = _require(vid)
        if rec is not None and rec.frontend == FRONTEND_DIRECT:
            return _direct_mkdir(rec)
        ctx, err = _files_ctx(vid)
        if err:
            return err
        _rec, root, p = ctx
        if p == root:
            return jsonify(error="path is required"), 400
        if os.path.exists(p):
            return jsonify(error="already exists"), 409
        os.makedirs(p)
        return jsonify(path=p[len(root) :] or "/"), 201

    @app.delete("/volumes/<vid>/files")
    def delete_files(vid):
        rec = _require(vid)
        if rec is not None and rec.frontend == FRONTEND_DIRECT:
            return _direct_delete(rec)
        ctx, err = _files_ctx(vid)
        if err:
            return err
        _rec, root, p = ctx
        if p == root:
            return jsonify(error="refusing to delete the volume root"), 400
        if os.path.isdir(p):
            shutil.rmtree(p)
        elif os.path.isfile(p):
            os.remove(p)
        else:
            return jsonify(error="no such file"), 404
        return "", 204

    @app.post("/volumes/<vid>/files/transfer")
    def transfer_files(vid):
        """{"op": "move"|"copy", "src": "/a", "dst": "/b"} — rename is a move
        within the same directory."""
        body = request.get_json(silent=True) or {}
        op, src, dst = body.get("op"), body.get("src"), body.get("dst")
        if op not in ("move", "copy") or not src or not dst or dst == "/":
            return jsonify(error="op (move|copy), src, dst are required"), 400
        rec = _require(vid)
        if rec is None:
            return jsonify(error="no such volume"), 404
        if rec.frontend == FRONTEND_DIRECT:
            def run():
                with registry.session(rec.id) as m:
                    if op == "copy":
                        m.copy(src, dst)
                    else:
                        m.move(src, dst)
                return jsonify(src=src, dst=dst), 200

            return _direct_call(run)
        root = _files_root(rec)
        if root is None:
            return jsonify(error="volume is not mounted"), 409
        ps, pd = _safe_join(root, src), _safe_join(root, dst)
        if ps is None or pd is None or ps == root:
            return jsonify(error="bad path"), 400
        if not os.path.exists(ps):
            return jsonify(error="no such file"), 404
        if os.path.exists(pd):
            return jsonify(error="destination exists"), 409
        if op == "copy":
            (shutil.copytree if os.path.isdir(ps) else shutil.copy2)(ps, pd)
        else:
            os.replace(ps, pd)
        return jsonify(src=src, dst=dst), 200

    # -- filesystems (the unit of portability) -------------------------------
    @app.get("/filesystems")
    def list_filesystems():
        out = []
        for fsr in store.list_fs():
            try:
                size = os.stat(fsr.sqlite_path).st_size
            except OSError:
                size = None
            out.append(
                {
                    "id": fsr.id,
                    "display_name": fsr.display_name,
                    "created_at": fsr.created_at,
                    "size_bytes": size,
                    "volumes": [_volume_item(v) for v in store.volumes_of(fsr.id)],
                }
            )
        return jsonify(out), 200

    @app.patch("/filesystems/<fid>")
    def rename_filesystem(fid):
        fsr = store.get_fs(fid)
        if fsr is None:
            return jsonify(error="no such filesystem"), 404
        body = request.get_json(silent=True) or {}
        name = (body.get("display_name") or "").strip()
        if not name:
            return jsonify(error="display_name is required"), 400
        fsr.display_name = name
        store.put_fs(fsr)
        return jsonify(id=fid, display_name=name), 200

    @app.get("/filesystems/<fid>/export")
    def export_filesystem(fid):
        fsr = store.get_fs(fid)
        if fsr is None:
            return jsonify(error="no such filesystem"), 404
        return _export_response(
            fsr.sqlite_path, _export_name(fsr.display_name), fid
        )

    @app.post("/filesystems/import")
    def import_filesystem():
        """Accept an aloelite .sqlite upload, register its volumes. Encrypted
        volumes are labeled from enc_mode (readable without a PIN); unlocking
        happens later. The file lands under a fresh fs_id; display_name comes
        from the upload filename."""
        f = request.files.get("file")
        if f is None or not f.filename:
            return jsonify(error="multipart field 'file' is required"), 400
        head = f.stream.read(16)
        f.stream.seek(0)
        if head != b"SQLite format 3\x00":
            return jsonify(error="not an SQLite file"), 400

        fs_id = uuid.uuid4().hex
        sqlite_path = _sqlite_path(fs_id)
        f.save(sqlite_path)  # werkzeug streams to disk in chunks

        # Read volume rows directly (metadata is never encrypted); a file
        # without the aloelite schema is rejected and removed.
        try:
            con = sqlite3.connect(sqlite_path, timeout=5.0)
            try:
                rows = con.execute(
                    "SELECT volume_id, name, enc_mode, created_at FROM volume "
                    "ORDER BY volume_id"
                ).fetchall()
            finally:
                con.close()
        except sqlite3.Error as e:
            try:
                os.unlink(sqlite_path)
            except FileNotFoundError:
                pass
            return jsonify(error=f"not an aloelite filesystem: {e}"), 400

        display = os.path.basename(f.filename.replace("\\", "/")) or fs_id
        store.put_fs(
            FilesystemRecord(
                id=fs_id,
                display_name=display,
                sqlite_path=sqlite_path,
                created_at=time.time(),
            )
        )
        vols = []
        for volume_id, vname, enc_mode, created_at in rows:
            rec = VolumeRecord(
                id=uuid.uuid4().hex,
                name=vname or volume_id[:8],
                fs_id=fs_id,
                encrypted=(enc_mode != "none"),
                created_at=(created_at or 0) / 1000.0,
                mounted=False,
                mountpoint=None,
            )
            store.put(rec)
            vols.append(_volume_item(rec))
        return jsonify(id=fs_id, display_name=display, volumes=vols), 201

    @app.get("/health")
    def health():
        results = app.config.get("PREFLIGHT_RESULTS", [])
        warnings = [r for r in results if not r["ok"] and not r["fatal"]]
        return jsonify(ok=True, preflight=results, warnings=warnings), 200

    @app.get("/")
    def index():
        return redirect("/admin")

    @app.get("/admin")
    def admin():
        return render_template("admin.html")

    return app


__all__ = ["create_app"]
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
