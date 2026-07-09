# ./manager/api.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
manager.api — the nine-endpoint HTTP API.

`create_app(store, supervisor, ...)` returns a Flask app. The app talks to the
metadata store and the mount supervisor through their interfaces only; volume
creation uses AloeLite (imported lazily so this module imports without the
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

import os
import sqlite3
import time
import uuid

from flask import Flask, Response, jsonify, request, render_template

from . import errors as merr
from .store import VolumeRecord, VolumeStore

ALOELITE_ROOT = "/aloelite-root"
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
    aloelite_root: str = ALOELITE_ROOT,
    host_mnt_prefix: str = HOST_MNT_PREFIX,
) -> Flask:
    app = Flask(__name__)

    def _host_path(volume_id: str) -> str:
        return f"{host_mnt_prefix.rstrip('/')}/{volume_id}"

    def _sqlite_path(volume_id: str) -> str:
        return os.path.join(aloelite_root, f"{volume_id}.sqlite")

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
        sqlite_path = _sqlite_path(vid)

        from aloelite.aloelite import AloeLite  # lazy

        with AloeLite(sqlite_path) as fs:
            fs.create_volume(name, pin=pin.encode() if pin else None)

        rec = VolumeRecord(
            id=vid,
            name=name,
            sqlite_path=sqlite_path,
            encrypted=encrypted,
            created_at=time.time(),
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
                supervisor.unmount(rec)
            except merr.NotMounted:
                pass  # store said mounted but session already gone; proceed
            rec = _require(vid) or rec
        try:
            os.unlink(rec.sqlite_path)
        except FileNotFoundError:
            pass
        # best-effort: drop any sidecar WAL/SHM left behind
        for suffix in ("-wal", "-shm"):
            try:
                os.unlink(rec.sqlite_path + suffix)
            except FileNotFoundError:
                pass
        store.delete(vid)
        return "", 204

    # -- GET /volumes -------------------------------------------------------
    @app.get("/volumes")
    def list_volumes():
        out = []
        for rec in store.list():
            item = {
                "id": rec.id,
                "name": rec.name,
                "encrypted": rec.encrypted,
                "mounted": rec.mounted,
            }
            if rec.mounted:
                item["mountpoint"] = rec.mountpoint
            out.append(item)
        return jsonify(out), 200

    # -- POST /volumes/<id>/mount ------------------------------------------
    @app.post("/volumes/<vid>/mount")
    def mount_volume(vid):
        rec = _require(vid)
        if rec is None:
            return jsonify(error="no such volume"), 404
        body = request.get_json(silent=True) or {}
        pin = body.get("pin")
        pin_bytes = pin.encode() if pin else None
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
        store.put(rec)
        return jsonify(id=vid, mountpoint=mountpoint, host_path=_host_path(mount_name)), 200

    # -- DELETE /volumes/<id>/mount ----------------------------------------
    @app.delete("/volumes/<vid>/mount")
    def unmount_volume(vid):
        rec = _require(vid)
        if rec is None:
            return jsonify(error="no such volume"), 404
        try:
            supervisor.unmount(rec)
        except merr.NotMounted:
            return jsonify(error="not mounted"), 404
        rec.mounted = False
        rec.mountpoint = None
        store.put(rec)
        return "", 204

    # -- GET /volumes/<id>/mount -------------------------------------------
    @app.get("/volumes/<vid>/mount")
    def mount_status(vid):
        rec = _require(vid)
        if rec is None:
            return jsonify(error="no such volume"), 404
        ready = bool(rec.mounted and supervisor.is_active(rec.mountpoint))
        return jsonify(
            id=vid, mounted=rec.mounted, mountpoint=rec.mountpoint, ready=ready
        ), 200

    # -- GET /volumes/<id>/stat --------------------------------------------
    @app.get("/volumes/<vid>/stat")
    def stat_volume(vid):
        rec = _require(vid)
        if rec is None:
            return jsonify(error="no such volume"), 404
        try:
            st = os.stat(rec.sqlite_path)
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
        checkpointed, remaining = _wal_checkpoint_truncate(rec.sqlite_path)
        if remaining:
            app.logger.warning("checkpoint %s left %d WAL frames", vid, remaining)
        return jsonify(
            id=vid, wal_frames_checkpointed=checkpointed, wal_frames_remaining=remaining
        ), 200

    # -- GET /volumes/<id>/export ------------------------------------------
    @app.get("/volumes/<vid>/export")
    def export_volume(vid):
        rec = _require(vid)
        if rec is None:
            return jsonify(error="no such volume"), 404
        checkpointed, remaining = _wal_checkpoint_truncate(rec.sqlite_path)
        if remaining:
            app.logger.warning(
                "export %s checkpoint left %d WAL frames", vid, remaining
            )
        # Open + fstat the fd so Content-Length matches exactly what we stream,
        # even if writers append a new WAL after the checkpoint (WAL mode does
        # not touch the main file until the next checkpoint, so the first
        # `size` bytes remain a coherent snapshot).
        fd = os.open(rec.sqlite_path, os.O_RDONLY)
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
                "Content-Disposition": f'attachment; filename="{vid}.sqlite"',
            },
        )

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
