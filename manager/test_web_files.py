# ./manager/test_web_files.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
API-level tests for the pieces the web editor and paste bin stand on:
upload-overwrites-in-place (an editor save IS an upload of the same name),
no-store on content downloads, and the vendored (CDN-free) admin page.

Direct frontend only -- real engine, tmp files, no FUSE, no supervisor.
"""

from __future__ import annotations

from io import BytesIO

import pytest

from manager.api import create_app
from manager.direct import FRONTEND_DIRECT, DirectSessionRegistry
from manager.store import JsonVolumeStore, VolumeRecord


@pytest.fixture
def client(tmp_path):
    store = JsonVolumeStore(str(tmp_path / "store.json"))
    registry = DirectSessionRegistry()
    rec = VolumeRecord(
        id="v1",
        name="vol",
        fs_id="fs1",
        encrypted=False,
        created_at=0.0,
        mounted=True,
        mountpoint=None,
        frontend=FRONTEND_DIRECT,
    )
    registry.unlock(rec, None, str(tmp_path / "vol.sqlite"))  # creates volume
    store.put(rec)
    app = create_app(store, supervisor=None, registry=registry)
    app.testing = True
    try:
        yield app.test_client()
    finally:
        registry.lock(rec)
        store.close()


def _upload(client, dirpath: str, name: str, data: bytes):
    return client.post(
        f"/volumes/v1/files/upload?path={dirpath}",
        data={"file": (BytesIO(data), name)},
        content_type="multipart/form-data",
    )


def test_upload_overwrites_in_place(client):
    """The editor's save contract: re-uploading a name replaces the content
    atomically (open_write TRUNCATE underneath), it does not error or dup."""
    assert _upload(client, "/", "note.txt", b"first draft").status_code == 201
    assert _upload(client, "/", "note.txt", b"second draft").status_code == 201
    r = client.get("/volumes/v1/files/download?path=/note.txt&inline=1")
    assert r.status_code == 200
    assert r.data == b"second draft"
    names = [e["name"] for e in client.get("/volumes/v1/files?path=/").json]
    assert names.count("note.txt") == 1


def test_download_is_no_store(client):
    """Volume content (pastes may carry secrets) must never be cached."""
    _upload(client, "/", "s.txt", b"hunter2")
    r = client.get("/volumes/v1/files/download?path=/s.txt&inline=1")
    assert r.headers.get("Cache-Control") == "no-store"


def test_paste_flow(client):
    """The paste-bin sequence the UI performs: mkdir /pastes (idempotent-ish:
    the second call may fail, the UI ignores it), save, list newest, read."""
    assert client.post("/volumes/v1/files/mkdir?path=/pastes").status_code == 201
    client.post("/volumes/v1/files/mkdir?path=/pastes")  # tolerated failure
    _upload(client, "/pastes", "paste-1.txt", b"from the phone")
    entries = client.get("/volumes/v1/files?path=/pastes").json
    assert [e["name"] for e in entries] == ["paste-1.txt"]
    r = client.get("/volumes/v1/files/download?path=/pastes/paste-1.txt&inline=1")
    assert r.data == b"from the phone"
    assert (
        client.delete("/volumes/v1/files?path=/pastes/paste-1.txt").status_code == 204
    )
    assert client.get("/volumes/v1/files?path=/pastes").json == []


# --------------------------------------------------------------------------
# Cookie-auth mode (ALOELITE_AUTH=cookie): per-client engine mounts
# --------------------------------------------------------------------------
_H = {"X-Aloelite": "1"}  # the CSRF header the UI sends on every mutation


@pytest.fixture
def capp(tmp_path):
    store = JsonVolumeStore(str(tmp_path / "store.json"))
    registry = DirectSessionRegistry()
    app = create_app(
        store,
        supervisor=None,
        registry=registry,
        aloelite_root=str(tmp_path),
        auth_mode="cookie",
    )
    app.testing = True
    try:
        yield app
    finally:
        registry.shutdown()
        store.close()


def _mk_volume(app, name="vol", pin=None):
    c = app.test_client()
    body = {"name": name}
    if pin:
        body.update(encrypted=True, pin=pin)
    r = c.post("/volumes", json=body, headers=_H)
    assert r.status_code == 201, r.json
    return r.json["id"]


def _unlock(app, vid, pin=None, headers=_H):
    """A fresh browser: its own test client (cookie jar) + mount request."""
    c = app.test_client()
    body = {"mode": "direct"}
    if pin:
        body["pin"] = pin
    r = c.post(f"/volumes/{vid}/mount", json=body, headers=headers)
    return c, r


def test_cookie_required_for_files(capp):
    vid = _mk_volume(capp)
    c1, r = _unlock(capp, vid)
    assert r.status_code == 200
    # the unlocking client has the cookie; a stranger does not
    assert c1.get(f"/volumes/{vid}/files?path=/").status_code == 200
    stranger = capp.test_client()
    assert stranger.get(f"/volumes/{vid}/files?path=/").status_code == 401
    # export is a whole-file surface: same rule
    assert stranger.get(f"/volumes/{vid}/export").status_code == 401
    assert c1.get(f"/volumes/{vid}/export").status_code == 200


def test_mutations_require_csrf_header(capp):
    vid = _mk_volume(capp)
    c1, _ = _unlock(capp, vid)
    up = f"/volumes/{vid}/files/upload?path=/"
    body = {"file": (BytesIO(b"x"), "f.txt")}
    r = c1.post(up, data=body, content_type="multipart/form-data")
    assert r.status_code == 403  # cookie present, header missing
    body = {"file": (BytesIO(b"x"), "f.txt")}
    r = c1.post(up, data=body, content_type="multipart/form-data", headers=_H)
    assert r.status_code == 201
    # mount itself is mutating too
    assert _unlock(capp, vid, headers={})[1].status_code == 403


def test_each_client_gets_its_own_mount(capp):
    """The audit property: two browsers -> two engine mount rows; detaching
    one leaves the other working; the last detach locks the volume."""
    vid = _mk_volume(capp)
    c1, r1 = _unlock(capp, vid)
    c2, r2 = _unlock(capp, vid)  # attach path (already unlocked)
    assert r1.status_code == r2.status_code == 200
    mounts = c1.get(f"/volumes/{vid}/mounts").json
    assert len(mounts) == 2
    # both clients work, each on its own mount
    for c in (c1, c2):
        assert c.get(f"/volumes/{vid}/files?path=/").status_code == 200
    # c1 detaches: c1 dead, c2 alive, volume still mounted
    r = c1.delete(f"/volumes/{vid}/mount", headers=_H)
    assert r.status_code == 200 and r.json == {"locked": False}
    assert c1.get(f"/volumes/{vid}/files?path=/").status_code == 401
    assert c2.get(f"/volumes/{vid}/files?path=/").status_code == 200
    # c2 detaches: last one out locks the volume (409 = not unlocked at all,
    # the pre-existing semantic; the UI routes 401 and 409 the same way)
    assert c2.delete(f"/volumes/{vid}/mount", headers=_H).status_code == 204
    assert c2.get(f"/volumes/{vid}/files?path=/").status_code == 409


def test_encrypted_attach_reproves_pin(capp):
    vid = _mk_volume(capp, name="vault", pin="s3cret")
    c1, r1 = _unlock(capp, vid, pin="s3cret")
    assert r1.status_code == 200
    _c2, r2 = _unlock(capp, vid, pin="wrong")
    assert r2.status_code == 400
    # the failed attach must not have disturbed c1's session
    assert c1.get(f"/volumes/{vid}/files?path=/").status_code == 200
    c3, r3 = _unlock(capp, vid, pin="s3cret")
    assert r3.status_code == 200
    assert c3.get(f"/volumes/{vid}/files?path=/").status_code == 200


def test_off_mode_unchanged(client):
    """AUTH off (the default): no cookie, no CSRF header, everything works --
    aloeforge and existing scripts see no behavior change."""
    assert _upload(client, "/", "f.txt", b"x").status_code == 201
    assert client.get("/volumes/v1/files?path=/").status_code == 200


def test_admin_page_is_self_contained(client):
    """No CDN references: a VPN'd client with no other egress must get a
    working page, and page loads must not leak to third parties."""
    r = client.get("/admin")
    assert r.status_code == 200
    body = r.data.decode()
    assert "cdn.jsdelivr.net" not in body
    assert "unpkg.com" not in body
    for asset in ("bootstrap.min.css", "alpine.min.js", "bootstrap.bundle.min.js"):
        assert f"/static/{asset}" in body
        a = client.get(f"/static/{asset}")
        assert a.status_code == 200 and len(a.data) > 10_000


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
