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
