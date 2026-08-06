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
    app = create_app(store, supervisor=None, registry=registry, auth_mode="off")
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


def test_plain_volume_open_to_all(capp):
    """The gate IS encryption: a plain volume in cookie mode behaves exactly
    like auth off -- no cookie, no CSRF header, any client, scripts work."""
    vid = _mk_volume(capp)
    _c1, r = _unlock(capp, vid)
    assert r.status_code == 200
    stranger = capp.test_client()
    assert stranger.get(f"/volumes/{vid}/files?path=/").status_code == 200
    body = {"file": (BytesIO(b"x"), "f.txt")}
    r = stranger.post(  # no cookie, no X-Aloelite header: still fine
        f"/volumes/{vid}/files/upload?path=/",
        data=body,
        content_type="multipart/form-data",
    )
    assert r.status_code == 201
    assert stranger.get(f"/volumes/{vid}/export").status_code == 200


def test_encrypted_volume_requires_session(capp):
    vid = _mk_volume(capp, name="vault", pin="s3cret")
    c1, r = _unlock(capp, vid, pin="s3cret")
    assert r.status_code == 200
    assert c1.get(f"/volumes/{vid}/files?path=/").status_code == 200
    stranger = capp.test_client()
    assert stranger.get(f"/volumes/{vid}/files?path=/").status_code == 401
    # export/checkpoint are whole-file surfaces: same rule
    assert stranger.get(f"/volumes/{vid}/export").status_code == 401
    assert c1.get(f"/volumes/{vid}/export").status_code == 200


def test_encrypted_mutations_require_csrf_header(capp):
    vid = _mk_volume(capp, name="vault", pin="s3cret")
    c1, _ = _unlock(capp, vid, pin="s3cret")
    up = f"/volumes/{vid}/files/upload?path=/"
    body = {"file": (BytesIO(b"x"), "f.txt")}
    r = c1.post(up, data=body, content_type="multipart/form-data")
    assert r.status_code == 403  # cookie present, header missing
    body = {"file": (BytesIO(b"x"), "f.txt")}
    r = c1.post(up, data=body, content_type="multipart/form-data", headers=_H)
    assert r.status_code == 201


def test_each_encrypted_client_gets_its_own_mount(capp):
    """The audit property: two browsers, each proving the PIN -> two engine
    mount rows; detaching one leaves the other working; the last detach
    locks the volume."""
    vid = _mk_volume(capp, name="vault", pin="s3cret")
    c1, r1 = _unlock(capp, vid, pin="s3cret")
    c2, r2 = _unlock(capp, vid, pin="s3cret")  # attach path
    assert r1.status_code == r2.status_code == 200
    assert len(c1.get(f"/volumes/{vid}/mounts").json) == 2
    for c in (c1, c2):
        assert c.get(f"/volumes/{vid}/files?path=/").status_code == 200
    r = c1.delete(f"/volumes/{vid}/mount", headers=_H)
    assert r.status_code == 200 and r.json == {"locked": False}
    assert c1.get(f"/volumes/{vid}/files?path=/").status_code == 401
    assert c2.get(f"/volumes/{vid}/files?path=/").status_code == 200
    # last one out locks the volume (409 = not unlocked at all, the
    # pre-existing semantic; the UI routes 401 and 409 the same way)
    assert c2.delete(f"/volumes/{vid}/mount", headers=_H).status_code == 204
    assert c2.get(f"/volumes/{vid}/files?path=/").status_code == 409


def test_detach_preserves_surviving_clients_cipher(capp):
    """Regression: ops.unmount clears the CONNECTION-wide cipher iff the
    closing mount is the connection's most recent one, so B (attached after
    A) detaching used to strip A's encryption key mid-session --
    'encryption_required: ... no encryption key installed' -- while the
    reverse order worked. Detach order must not matter."""
    vid = _mk_volume(capp, name="vault", pin="s3cret")
    c1, _ = _unlock(capp, vid, pin="s3cret")
    c2, _ = _unlock(capp, vid, pin="s3cret")  # most recent = c2's mount
    body = {"file": (BytesIO(b"before"), "a.txt")}
    r = c1.post(
        f"/volumes/{vid}/files/upload?path=/",
        data=body,
        content_type="multipart/form-data",
        headers=_H,
    )
    assert r.status_code == 201
    # the failing order: the most-recently-attached client detaches first
    r = c2.delete(f"/volumes/{vid}/mount", headers=_H)
    assert r.status_code == 200 and r.json == {"locked": False}
    # c1 must still hold a working cipher: write AND read back
    body = {"file": (BytesIO(b"after"), "b.txt")}
    r = c1.post(
        f"/volumes/{vid}/files/upload?path=/",
        data=body,
        content_type="multipart/form-data",
        headers=_H,
    )
    assert r.status_code == 201, r.json
    r = c1.get(f"/volumes/{vid}/files/download?path=/b.txt&inline=1")
    assert r.status_code == 200 and r.data == b"after"


def test_listing_reports_per_client_attachment(capp):
    """The badge's source of truth: gated volumes answer attached per
    REQUESTER; plain volumes are attached whenever mounted."""
    pv = _mk_volume(capp, name="plain")
    ev = _mk_volume(capp, name="vault", pin="s3cret")
    c1, _ = _unlock(capp, pv)
    c1.post(
        f"/volumes/{ev}/mount", json={"mode": "direct", "pin": "s3cret"}, headers=_H
    )
    stranger = capp.test_client()

    def items(c):
        return {v["id"]: v for v in c.get("/volumes").json}

    mine, theirs = items(c1), items(stranger)
    assert mine[pv]["attached"] is True and theirs[pv]["attached"] is True
    assert mine[ev]["attached"] is True
    assert theirs[ev]["attached"] is False  # mounted, but not for THIS client


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


def test_bearer_token_flow(capp):
    """The UI's actual path now: the token arrives in the mount response
    body, is held by the client, and is sent as X-Aloelite-Token -- no
    browser cookie storage in the loop at all (that dependency is what
    broke per-browser sessions in the field)."""
    vid = _mk_volume(capp, name="vault", pin="s3cret")
    c = capp.test_client()
    r = c.post(
        f"/volumes/{vid}/mount", json={"mode": "direct", "pin": "s3cret"}, headers=_H
    )
    assert r.status_code == 200
    tok = r.json.get("token")
    assert tok
    bare = capp.test_client()  # no cookies; header only
    hdr = {"X-Aloelite-Token": tok}
    assert bare.get(f"/volumes/{vid}/files?path=/", headers=hdr).status_code == 200
    # a bearer header is its own proof: no CSRF header needed on mutations
    body = {"file": (BytesIO(b"x"), "f.txt")}
    r = bare.post(
        f"/volumes/{vid}/files/upload?path=/",
        data=body,
        content_type="multipart/form-data",
        headers=hdr,
    )
    assert r.status_code == 201
    assert bare.get(f"/volumes/{vid}/export", headers=hdr).status_code == 200
    # a wrong token is refused
    bad = {"X-Aloelite-Token": "0" * 32}
    assert bare.get(f"/volumes/{vid}/files?path=/", headers=bad).status_code == 401
    # detach by bearer: single client, so this locks the volume
    assert bare.delete(f"/volumes/{vid}/mount", headers=hdr).status_code == 204


def test_off_mode_still_available(client):
    """ALOELITE_AUTH=off: the legacy escape hatch -- even encrypted volumes,
    once unlocked, are open to any client that can reach the manager."""
    assert _upload(client, "/", "f.txt", b"x").status_code == 201
    assert client.get("/volumes/v1/files?path=/").status_code == 200


def test_default_is_cookie_mode(tmp_path, monkeypatch):
    """No auth_mode argument, no env override: encrypted volumes are gated
    by default, and a device that proves the PIN gets in."""
    monkeypatch.delenv("ALOELITE_AUTH", raising=False)
    store = JsonVolumeStore(str(tmp_path / "s.json"))
    registry = DirectSessionRegistry()
    app = create_app(
        store, supervisor=None, registry=registry, aloelite_root=str(tmp_path)
    )
    app.testing = True
    try:
        vid = _mk_volume(app, name="vault", pin="s3cret")
        c, r = _unlock(app, vid, pin="s3cret")
        assert r.status_code == 200
        assert c.get(f"/volumes/{vid}/files?path=/").status_code == 200
        stranger = app.test_client()
        assert stranger.get(f"/volumes/{vid}/files?path=/").status_code == 401
        # the stranger's way in is the PIN, not reachability
        r2 = stranger.post(
            f"/volumes/{vid}/mount",
            json={"mode": "direct", "pin": "s3cret"},
            headers=_H,
        )
        assert r2.status_code == 200
        assert stranger.get(f"/volumes/{vid}/files?path=/").status_code == 200
    finally:
        registry.shutdown()
        store.close()


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
