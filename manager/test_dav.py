# ./manager/test_dav.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
WebDAV (class 1) frontend tests.

Real engine, tmp files, no FUSE, no supervisor -- the same shape as
test_web_files.py. Werkzeug's test client drives arbitrary methods through
`client.open(method=...)`, so PROPFIND/MKCOL/COPY/MOVE need no live socket.

XML is asserted through xml.etree (stdlib) rather than string matching, so a
namespace-prefix or attribute-order change does not break a test that was
meant to be about semantics.
"""

from __future__ import annotations

import base64
import xml.etree.ElementTree as ET

import pytest

from manager.api import create_app
from manager.direct import FRONTEND_DIRECT, DirectSessionRegistry
from manager.store import FilesystemRecord, JsonVolumeStore, VolumeRecord

D = "{DAV:}"
PIN = "correct horse"


def _record(vid="v1", name="vol", encrypted=False) -> VolumeRecord:
    return VolumeRecord(
        id=vid,
        name=name,
        fs_id=f"fs-{vid}",
        encrypted=encrypted,
        created_at=0.0,
        mounted=True,
        mountpoint=None,
        frontend=FRONTEND_DIRECT,
    )


@pytest.fixture
def client(tmp_path):
    """A plain (unencrypted) volume, already unlocked."""
    store = JsonVolumeStore(str(tmp_path / "store.json"))
    registry = DirectSessionRegistry()
    rec = _record()
    registry.unlock(rec, None, str(tmp_path / "vol.sqlite"))  # creates the volume
    store.put(rec)
    app = create_app(
        store, supervisor=None, registry=registry, auth_mode="off", webdav=True
    )
    app.testing = True
    try:
        yield app.test_client()
    finally:
        registry.lock(rec)
        store.close()


@pytest.fixture
def enc_client(tmp_path):
    """An ENCRYPTED volume that has never been unlocked -- the DAV Basic
    handshake is what brings it up."""
    store = JsonVolumeStore(str(tmp_path / "store.json"))
    registry = DirectSessionRegistry()
    rec = _record(vid="e1", name="secret", encrypted=True)
    rec.mounted = False
    sqlite_path = str(tmp_path / "enc.sqlite")
    registry.unlock(rec, PIN.encode(), sqlite_path)
    registry.lock(rec)  # created and encrypted on disk, now closed
    # DAV re-unlocks by path, so the volume needs its parent filesystem row --
    # unlike test_web_files.py, where the volume is handed over already open.
    store.put_fs(
        FilesystemRecord(
            id=rec.fs_id,
            display_name="secret",
            sqlite_path=sqlite_path,
            created_at=0.0,
        )
    )
    store.put(rec)
    app = create_app(
        store, supervisor=None, registry=registry, auth_mode="cookie", webdav=True
    )
    app.testing = True
    try:
        yield app.test_client(), registry, rec
    finally:
        try:
            registry.lock(rec)
        except Exception:
            pass
        store.close()


def _basic(pin: str) -> dict:
    raw = base64.b64encode(f"aloelite:{pin}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


def _put(client, path: str, data: bytes, **kw):
    return client.open(f"/dav/v1{path}", method="PUT", data=data, **kw)


def _propfind(client, path: str, depth="1", body=None, **kw):
    return client.open(
        f"/dav/v1{path}",
        method="PROPFIND",
        headers={"Depth": depth, **kw.pop("headers", {})},
        data=body,
        **kw,
    )


def _responses(resp) -> dict[str, ET.Element]:
    """href -> <response>, the shape every multistatus assertion wants."""
    root = ET.fromstring(resp.data)
    assert root.tag == f"{D}multistatus"
    return {r.find(f"{D}href").text: r for r in root.findall(f"{D}response")}


def _prop(response: ET.Element, name: str):
    """The 200-status value of one property, or None if it came back 404."""
    for propstat in response.findall(f"{D}propstat"):
        status = propstat.find(f"{D}status").text
        if "200" not in status:
            continue
        found = propstat.find(f"{D}prop").find(f"{D}{name}")
        if found is not None:
            return found
    return None


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------
def test_options_advertises_class_1_and_ms_author_via(client):
    """The two headers the redirectors read: DAV tells them it is a WebDAV
    server at all, MS-Author-Via stops Office probing FrontPage endpoints."""
    r = client.open("/dav/v1/", method="OPTIONS")
    assert r.status_code == 200
    assert r.headers["DAV"] == "1"
    assert r.headers["MS-Author-Via"] == "DAV"
    assert r.headers["Accept-Ranges"] == "bytes"
    allow = {v.strip() for v in r.headers["Allow"].split(",")}
    assert {"PROPFIND", "MKCOL", "COPY", "MOVE", "PUT", "DELETE"} <= allow
    assert "LOCK" not in allow  # class 1: advertised nowhere


def test_lock_is_405_with_allow(client):
    """A class 1 server owes a client that tries LOCK anyway a 405 and an
    Allow header, not a 500 or a silent hang."""
    r = client.open("/dav/v1/", method="LOCK")
    assert r.status_code == 405
    assert "PROPFIND" in r.headers["Allow"]
    assert r.headers["DAV"] == "1"


def test_options_needs_no_credentials_on_encrypted_volume(enc_client):
    """Explorer probes OPTIONS before it will authenticate; 401 here makes it
    abandon the whole share."""
    c, _registry, _rec = enc_client
    r = c.open("/dav/e1/", method="OPTIONS")
    assert r.status_code == 200
    assert r.headers["DAV"] == "1"


# --------------------------------------------------------------------------
# PROPFIND
# --------------------------------------------------------------------------
def test_propfind_depth_1_lists_children_with_live_properties(client):
    _put(client, "/note.txt", b"hello dav")
    assert client.open("/dav/v1/sub", method="MKCOL").status_code == 201

    r = _propfind(client, "/", depth="1")
    assert r.status_code == 207
    assert r.mimetype == "application/xml"
    by_href = _responses(r)
    assert set(by_href) == {"/dav/v1/", "/dav/v1/note.txt", "/dav/v1/sub/"}

    entry = by_href["/dav/v1/note.txt"]
    assert _prop(entry, "getcontentlength").text == str(len(b"hello dav"))
    assert _prop(entry, "resourcetype").find(f"{D}collection") is None
    # getlastmodified is HTTP-date, creationdate is ISO 8601. Mixing them is
    # the classic bug and clients do notice.
    assert _prop(entry, "getlastmodified").text.endswith("GMT")
    assert _prop(entry, "creationdate").text.endswith("Z")
    # Strong for an entry: backed by content.version, not by modified_at.
    assert _prop(entry, "getetag").text.startswith('"')

    coll = by_href["/dav/v1/sub/"]
    assert _prop(coll, "resourcetype").find(f"{D}collection") is not None
    # A collection has no content length; asking must yield 404, not "0".
    assert _prop(coll, "getcontentlength") is None


def test_collection_hrefs_end_in_slash_and_are_percent_encoded(client):
    assert client.open("/dav/v1/a b", method="MKCOL").status_code == 201
    _put(client, "/caf%C3%A9.txt", "café".encode())
    hrefs = set(_responses(_propfind(client, "/", depth="1")))
    assert "/dav/v1/a%20b/" in hrefs  # space encoded, collection slashed
    assert "/dav/v1/caf%C3%A9.txt" in hrefs


def test_propfind_named_props_reports_absent_ones_as_404(client):
    _put(client, "/f.txt", b"x")
    body = (
        b'<?xml version="1.0"?><D:propfind xmlns:D="DAV:"><D:prop>'
        b"<D:getcontentlength/><D:nosuchprop/>"
        b"</D:prop></D:propfind>"
    )
    r = _propfind(client, "/f.txt", depth="0", body=body)
    resp = _responses(r)["/dav/v1/f.txt"]
    statuses = {ps.find(f"{D}status").text for ps in resp.findall(f"{D}propstat")}
    assert any("200" in s for s in statuses)
    assert any("404" in s for s in statuses)


def test_propfind_depth_infinity_is_refused_with_the_precondition(client):
    """Allowed by RFC 4918 9.1, and refusing keeps one client from walking a
    whole volume while holding the per-volume session lock."""
    r = _propfind(client, "/", depth="infinity")
    assert r.status_code == 403
    assert ET.fromstring(r.data).find(f"{D}propfind-finite-depth") is not None


def test_propfind_default_depth_is_infinity_so_it_is_refused(client):
    """RFC 4918's default when the header is absent -- not 0, and not 1."""
    assert _propfind(client, "/", depth=None).status_code == 403


def test_propfind_rejects_a_dtd(client):
    """Every entity-expansion and external-entity attack needs a DTD, and no
    legitimate DAV body has one."""
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]>'
        b'<D:propfind xmlns:D="DAV:"><D:allprop/></D:propfind>'
    )
    r = _propfind(client, "/", depth="0", body=bomb)
    assert r.status_code == 400
    assert b"DTD" in r.data


def test_propfind_on_missing_path_is_404(client):
    assert _propfind(client, "/nope", depth="0").status_code == 404


# --------------------------------------------------------------------------
# GET / HEAD / Range
# --------------------------------------------------------------------------
def test_get_returns_content_etag_and_no_store(client):
    _put(client, "/a.txt", b"0123456789")
    r = client.get("/dav/v1/a.txt")
    assert r.status_code == 200
    assert r.data == b"0123456789"
    assert r.headers["Accept-Ranges"] == "bytes"
    assert r.headers["Cache-Control"] == "no-store"
    assert r.headers["ETag"].startswith('"')  # strong; see _etag


def test_head_has_headers_and_no_body(client):
    _put(client, "/a.txt", b"0123456789")
    r = client.open("/dav/v1/a.txt", method="HEAD")
    assert r.status_code == 200
    assert r.headers["Content-Length"] == "10"
    assert r.data == b""


@pytest.mark.parametrize(
    "header,expected,content_range",
    [
        ("bytes=0-3", b"0123", "bytes 0-3/10"),
        ("bytes=4-", b"456789", "bytes 4-9/10"),
        ("bytes=-3", b"789", "bytes 7-9/10"),
        ("bytes=8-99", b"89", "bytes 8-9/10"),  # clamped to the end
    ],
)
def test_ranged_get(client, header, expected, content_range):
    """Range is what lets Finder and davfs2 seek instead of pulling whole
    files, and it is the roadmap's top item for the JSON API too."""
    _put(client, "/a.txt", b"0123456789")
    r = client.get("/dav/v1/a.txt", headers={"Range": header})
    assert r.status_code == 206
    assert r.data == expected
    assert r.headers["Content-Range"] == content_range
    assert r.headers["Content-Length"] == str(len(expected))


def test_unsatisfiable_range_is_416_with_content_range(client):
    _put(client, "/a.txt", b"0123456789")
    r = client.get("/dav/v1/a.txt", headers={"Range": "bytes=99-"})
    assert r.status_code == 416
    assert r.headers["Content-Range"] == "bytes */10"


def test_multi_range_falls_back_to_the_whole_body(client):
    """RFC 7233 lets a server ignore a Range it will not satisfy; answering
    200 beats a malformed multipart/byteranges."""
    _put(client, "/a.txt", b"0123456789")
    r = client.get("/dav/v1/a.txt", headers={"Range": "bytes=0-1,4-5"})
    assert r.status_code == 200
    assert r.data == b"0123456789"


def test_get_on_a_collection_is_405(client):
    assert client.get("/dav/v1/").status_code == 405


# --------------------------------------------------------------------------
# PUT / MKCOL / DELETE
# --------------------------------------------------------------------------
def test_put_creates_then_overwrites(client):
    r = _put(client, "/n.txt", b"first")
    assert r.status_code == 201
    assert r.headers["ETag"]
    r = _put(client, "/n.txt", b"second")
    assert r.status_code == 204  # existing resource -> no new URL created
    assert client.get("/dav/v1/n.txt").data == b"second"


def test_put_into_a_missing_collection_is_409(client):
    """RFC 4918 9.7.1: PUT must not create intermediate collections."""
    assert _put(client, "/nope/deep/f.txt", b"x").status_code == 409


def test_put_with_content_range_is_rejected(client):
    """A partial PUT is undefined by RFC 4918 9.7.1; guessing corrupts."""
    _put(client, "/a.txt", b"0123456789")
    r = _put(client, "/a.txt", b"XX", headers={"Content-Range": "bytes 0-1/10"})
    assert r.status_code == 400


def test_put_over_a_collection_is_405(client):
    client.open("/dav/v1/dir", method="MKCOL")
    assert _put(client, "/dir", b"x").status_code == 405


def test_zero_length_put(client):
    """Finder's save dance opens with an empty PUT before the real one."""
    assert _put(client, "/empty.txt", b"").status_code == 201
    r = client.get("/dav/v1/empty.txt")
    assert r.status_code == 200
    assert r.data == b""


def test_interrupted_put_is_atomic_and_does_not_wedge(client):
    """A client that vanishes mid-PUT must leave the PREVIOUS content intact.

    This used to pin the opposite: Descriptor.close() committed
    unconditionally, so the partial bytes replaced the file and the loss was
    silent -- the error was about the transfer, not the data, so nothing
    surfaced it. Descriptor.abort() plus abort-on-exception in __exit__ closed
    it. The second assertion is the one that was always required: the session
    lock is released either way, so one dead transfer cannot wedge the volume.
    """
    from werkzeug.test import EnvironBuilder

    _put(client, "/b.txt", b"ORIGINAL")

    class _Vanishes:
        def __init__(self):
            self.calls = 0

        def read(self, n):
            self.calls += 1
            if self.calls == 1:
                return b"AAAA"
            raise ConnectionResetError("client vanished")

    env = EnvironBuilder(
        path="/dav/v1/b.txt", method="PUT", data=b"AAAABBBB"
    ).get_environ()
    env["wsgi.input"] = _Vanishes()
    env["CONTENT_LENGTH"] = "8"
    app = client.application
    app.testing = False  # let the error be handled, not re-raised
    try:
        body = app(env, lambda status, headers, exc_info=None: lambda x: None)
        b"".join(body)
        body.close()
    except ConnectionResetError:
        pass
    finally:
        app.testing = True

    # The partial write is discarded; the previous version survives whole.
    assert client.get("/dav/v1/b.txt").data == b"ORIGINAL"
    # The volume still answers -- the write lock was released, not stranded.
    assert _propfind(client, "/", depth="1").status_code == 207
    # And the resource is still writable, which is the half that a
    # "just don't close it" fix would have broken.
    assert _put(client, "/b.txt", b"AFTER").status_code == 204
    assert client.get("/dav/v1/b.txt").data == b"AFTER"


def test_mkcol_conflicts(client):
    assert client.open("/dav/v1/d", method="MKCOL").status_code == 201
    assert client.open("/dav/v1/d", method="MKCOL").status_code == 405
    assert client.open("/dav/v1/miss/d", method="MKCOL").status_code == 409
    r = client.open("/dav/v1/withbody", method="MKCOL", data=b"<x/>")
    assert r.status_code == 415


def test_delete_is_recursive_on_a_collection(client):
    client.open("/dav/v1/d", method="MKCOL")
    _put(client, "/d/f.txt", b"x")
    assert client.delete("/dav/v1/d").status_code == 204
    assert _propfind(client, "/d", depth="0").status_code == 404


def test_delete_of_the_volume_root_is_refused(client):
    assert client.delete("/dav/v1/").status_code == 403


# --------------------------------------------------------------------------
# COPY / MOVE
# --------------------------------------------------------------------------
def _dest(path: str) -> dict:
    return {"Destination": f"http://localhost/dav/v1{path}"}


def test_move_renames(client):
    _put(client, "/a.txt", b"payload")
    r = client.open("/dav/v1/a.txt", method="MOVE", headers=_dest("/b.txt"))
    assert r.status_code == 201
    assert client.get("/dav/v1/b.txt").data == b"payload"
    assert client.get("/dav/v1/a.txt").status_code == 404


def test_move_overwrite_replaces_rather_than_duplicating(client):
    """The engine allows same-name siblings (NODE-5, one visible), so a naive
    move onto an existing name would leave TWO children. Overwrite: T has to
    mean replace."""
    _put(client, "/a.txt", b"new")
    _put(client, "/b.txt", b"old")
    r = client.open("/dav/v1/a.txt", method="MOVE", headers=_dest("/b.txt"))
    assert r.status_code == 204
    assert client.get("/dav/v1/b.txt").data == b"new"
    names = list(_responses(_propfind(client, "/", depth="1")))
    assert names.count("/dav/v1/b.txt") == 1
    assert "/dav/v1/a.txt" not in names


def test_move_with_overwrite_false_onto_existing_is_412(client):
    _put(client, "/a.txt", b"a")
    _put(client, "/b.txt", b"b")
    r = client.open(
        "/dav/v1/a.txt",
        method="MOVE",
        headers={**_dest("/b.txt"), "Overwrite": "F"},
    )
    assert r.status_code == 412
    assert client.get("/dav/v1/b.txt").data == b"b"  # untouched


def test_copy_is_deep_for_collections(client):
    client.open("/dav/v1/src", method="MKCOL")
    _put(client, "/src/f.txt", b"deep")
    r = client.open("/dav/v1/src", method="COPY", headers=_dest("/dst"))
    assert r.status_code == 201
    assert client.get("/dav/v1/dst/f.txt").data == b"deep"
    assert client.get("/dav/v1/src/f.txt").data == b"deep"  # source survives


def test_copy_move_to_another_volume_is_502(client):
    _put(client, "/a.txt", b"x")
    r = client.open(
        "/dav/v1/a.txt",
        method="MOVE",
        headers={"Destination": "http://localhost/dav/OTHER/a.txt"},
    )
    assert r.status_code == 502


def test_move_without_destination_is_400(client):
    _put(client, "/a.txt", b"x")
    assert client.open("/dav/v1/a.txt", method="MOVE").status_code == 400


# --------------------------------------------------------------------------
# PROPPATCH
# --------------------------------------------------------------------------
_SET_BODY = (
    b'<?xml version="1.0"?><D:propertyupdate xmlns:D="DAV:" xmlns:Z="urn:schemas-'
    b'microsoft-com:"><D:set><D:prop><Z:Win32FileAttributes>00000020'
    b"</Z:Win32FileAttributes></D:prop></D:set></D:propertyupdate>"
)


def test_proppatch_stores_and_returns_a_dead_property(client):
    """Explorer PROPPATCHes Win32* properties after a save; storing them is
    what stops it complaining."""
    _put(client, "/a.txt", b"x")
    r = client.open("/dav/v1/a.txt", method="PROPPATCH", data=_SET_BODY)
    assert r.status_code == 207
    resp = _responses(r)["/dav/v1/a.txt"]
    assert "200" in resp.find(f"{D}propstat").find(f"{D}status").text

    body = (
        b'<?xml version="1.0"?><D:propfind xmlns:D="DAV:" xmlns:Z="urn:schemas-'
        b'microsoft-com:"><D:prop><Z:Win32FileAttributes/></D:prop></D:propfind>'
    )
    got = _responses(_propfind(client, "/a.txt", depth="0", body=body))
    prop = got["/dav/v1/a.txt"].find(f"{D}propstat").find(f"{D}prop")
    assert prop[0].text == "00000020"


def _xml_attr_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")


# Namespace URIs containing '}' or '<' are rejected by expat itself (ElementTree
# uses '}' as its internal namespace separator), so they can never reach the
# response builder. These two CAN.
@pytest.mark.parametrize("ns", ['urn:z"/><pwn', "urn:amp&x"])
def test_hostile_dead_property_namespace_cannot_break_the_response(client, ns):
    """A dead property is PERSISTED, so an injection here is not a one-request
    glitch: every later PROPFIND on that node would return malformed XML and
    the resource would stay broken for every client until the metadata was
    cleared out of band. The namespace URI is client-supplied and lands in an
    attribute value, so it needs quoteattr -- saxutils.escape handles &, < and
    > but leaves quotation marks alone."""
    _put(client, "/a.txt", b"x")
    body = (
        '<?xml version="1.0"?><D:propertyupdate xmlns:D="DAV:" xmlns:E="{}">'
        "<D:set><D:prop><E:p>v</E:p></D:prop></D:set></D:propertyupdate>"
    ).format(_xml_attr_escape(ns))
    assert (
        client.open("/dav/v1/a.txt", method="PROPPATCH", data=body.encode()).status_code
        == 207
    )

    r = _propfind(client, "/a.txt", depth="0")
    assert r.status_code == 207
    root = ET.fromstring(r.data)  # must still be well-formed
    stored = root.find(f".//{{{ns}}}p")
    assert stored is not None and stored.text == "v"


def test_dead_property_value_with_markup_round_trips(client):
    """The value is client-supplied too, and goes out as element text."""
    _put(client, "/a.txt", b"x")
    body = (
        '<?xml version="1.0"?><D:propertyupdate xmlns:D="DAV:" xmlns:E="urn:t">'
        "<D:set><D:prop><E:p>&lt;b&gt;&amp;&quot;café&quot;&lt;/b&gt;</E:p>"
        "</D:prop></D:set></D:propertyupdate>"
    ).encode()
    assert (
        client.open("/dav/v1/a.txt", method="PROPPATCH", data=body).status_code == 207
    )
    root = ET.fromstring(_propfind(client, "/a.txt", depth="0").data)
    assert root.find(".//{urn:t}p").text == '<b>&"café"</b>'


def test_proppatch_on_a_live_property_is_403_and_atomic(client):
    """RFC 4918 9.2: if one property fails, none are applied and the rest
    report 424."""
    _put(client, "/a.txt", b"x")
    body = (
        b'<?xml version="1.0"?><D:propertyupdate xmlns:D="DAV:" xmlns:Z="u:">'
        b"<D:set><D:prop><D:getetag>forged</D:getetag><Z:mine>v</Z:mine>"
        b"</D:prop></D:set></D:propertyupdate>"
    )
    r = client.open("/dav/v1/a.txt", method="PROPPATCH", data=body)
    assert r.status_code == 207
    statuses = [
        ps.find(f"{D}status").text
        for ps in _responses(r)["/dav/v1/a.txt"].findall(f"{D}propstat")
    ]
    assert any("403" in s for s in statuses)
    assert any("424" in s for s in statuses)


def test_proppatch_preserves_another_frontend_s_metadata(client, tmp_path):
    """set_metadata is a WHOLESALE replace (NODE-6). fuse.py keeps 'mode' and
    'symlink' in that same map, so a PROPPATCH that clobbered them would
    silently strip permissions off every file Explorer touched."""
    _put(client, "/a.txt", b"x")
    registry = client.application.config["DIRECT_REGISTRY"]
    with registry.session("v1") as m:
        m.set_metadata("/a.txt", {"mode": "755", "symlink": "1"})

    assert client.open("/dav/v1/a.txt", method="PROPPATCH", data=_SET_BODY).status_code

    with registry.session("v1") as m:
        meta = m.stat("/a.txt").metadata
    assert meta["mode"] == "755"
    assert meta["symlink"] == "1"
    assert any(k.startswith("dav:") for k in meta)


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------
def test_encrypted_volume_demands_basic_and_unlocks_with_the_pin(enc_client):
    """The Basic handshake IS the unlock: point a client at the URL, give the
    PIN, and the volume comes up -- the same path POST /volumes/<id>/mount
    takes for a browser."""
    c, registry, _rec = enc_client
    assert not registry.is_unlocked("e1")

    r = c.open("/dav/e1/", method="PROPFIND", headers={"Depth": "0"})
    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"].startswith("Basic ")

    r = c.open("/dav/e1/", method="PROPFIND", headers={"Depth": "0", **_basic(PIN)})
    assert r.status_code == 207
    assert registry.is_unlocked("e1")


def test_wrong_pin_is_401_not_500(enc_client):
    c, _registry, _rec = enc_client
    r = c.open("/dav/e1/", method="PROPFIND", headers={"Depth": "0", **_basic("wrong")})
    assert r.status_code == 401


def test_pin_is_derived_once_and_then_cached(enc_client):
    """Argon2id costs ~376ms; one Finder listing is dozens of requests, so a
    per-request derivation would make the mount unusable. The cache must reuse
    the engine mount token rather than re-attaching."""
    c, registry, _rec = enc_client
    headers = {"Depth": "0", **_basic(PIN)}
    assert c.open("/dav/e1/", method="PROPFIND", headers=headers).status_code == 207
    with registry._lock:
        after_first = len(registry._sessions["e1"].clients)
    for _ in range(4):
        assert c.open("/dav/e1/", method="PROPFIND", headers=headers).status_code == 207
    with registry._lock:
        assert len(registry._sessions["e1"].clients) == after_first


def test_dav_ignores_the_session_cookie(enc_client):
    """DAV authenticates with Basic and nothing else. Refusing to read cookies
    is what makes the JSON API's CSRF header unnecessary here: there is no
    ambient browser credential for a cross-site request to ride."""
    c, _registry, _rec = enc_client
    c.open("/dav/e1/", method="PROPFIND", headers={"Depth": "0", **_basic(PIN)})
    c.set_cookie("aloe_m_e1", "whatever", domain="localhost")
    r = c.open("/dav/e1/", method="PROPFIND", headers={"Depth": "0"})
    assert r.status_code == 401


def test_webdav_is_off_by_default(tmp_path):
    """A second write-capable surface on every volume does not appear by
    accident."""
    store = JsonVolumeStore(str(tmp_path / "store.json"))
    registry = DirectSessionRegistry()
    rec = _record()
    registry.unlock(rec, None, str(tmp_path / "vol.sqlite"))
    store.put(rec)
    app = create_app(store, supervisor=None, registry=registry, auth_mode="off")
    app.testing = True
    try:
        assert app.test_client().open("/dav/v1/", method="OPTIONS").status_code == 404
    finally:
        registry.lock(rec)
        store.close()


# --------------------------------------------------------------------------
# Path handling
# --------------------------------------------------------------------------
def test_dot_dot_cannot_climb_out_of_the_volume(client):
    """'..' is dropped, not resolved: the DAV layer must never hand the engine
    a path that even tries to leave the mount point."""
    _put(client, "/secret.txt", b"s")
    client.open("/dav/v1/d", method="MKCOL")
    r = client.get("/dav/v1/d/../secret.txt")
    assert r.status_code == 200  # normalized to /secret.txt, still inside
    assert r.data == b"s"  # consume: a streaming body holds the session lock
    assert client.get("/dav/v1/../../etc/passwd").status_code == 404


def test_percent_in_a_name_is_decoded_exactly_once(client):
    """Werkzeug already decodes a routed <path:>. Decoding again would make a
    file genuinely named 'a%20b.txt' collide with one named 'a b.txt' -- two
    distinct names silently aliasing onto one node."""
    _put(client, "/a%2520b.txt", b"literal-percent")  # -> name is 'a%20b.txt'
    _put(client, "/a%20b.txt", b"real-space")  # -> name is 'a b.txt'
    assert client.get("/dav/v1/a%2520b.txt").data == b"literal-percent"
    assert client.get("/dav/v1/a%20b.txt").data == b"real-space"
    hrefs = set(_responses(_propfind(client, "/", depth="1")))
    assert {"/dav/v1/a%2520b.txt", "/dav/v1/a%20b.txt"} <= hrefs


def test_destination_header_is_decoded_exactly_once(client):
    """The mirror case: Destination is a raw URL nothing has decoded yet."""
    _put(client, "/src.txt", b"x")
    r = client.open(
        "/dav/v1/src.txt",
        method="MOVE",
        headers={"Destination": "http://localhost/dav/v1/a%2520b.txt"},
    )
    assert r.status_code == 201
    assert client.get("/dav/v1/a%2520b.txt").data == b"x"


def test_trailing_slash_on_a_file_still_resolves(client):
    _put(client, "/a.txt", b"x")
    assert _propfind(client, "/a.txt/", depth="0").status_code == 207


def test_unknown_volume_is_404(client):
    assert client.open("/dav/nosuch/", method="PROPFIND").status_code == 404


# --------------------------------------------------------------------------
# Conditional requests (RFC 9110). Lost-update protection WITHOUT locking.
# --------------------------------------------------------------------------
def _etag_of(client, path="/a.txt") -> str:
    return client.open(f"/dav/v1{path}", method="HEAD").headers["ETag"]


def test_file_etag_is_strong_and_collection_etag_is_weak(client):
    """If-Match and If-Range use strong comparison, so a weak etag makes both
    permanent no-ops. Files get a strong tag from content.version; collections
    have no version and must not pretend otherwise."""
    _put(client, "/a.txt", b"x")
    assert not _etag_of(client).startswith("W/")

    client.open("/dav/v1/sub", method="MKCOL")
    coll = _prop(
        _responses(_propfind(client, "/sub/", depth="0"))["/dav/v1/sub/"], "getetag"
    )
    assert coll.text.startswith('W/"')


def test_etag_distinguishes_writes_in_the_same_millisecond(client):
    """The whole reason for moving off modified_at: a tight rewrite loop can
    land two commits in one millisecond, and a validator that aliases them
    lets a client keep stale content it believes it revalidated."""
    _put(client, "/a.txt", b"seed")
    tags = []
    for i in range(6):
        _put(client, "/a.txt", f"body-{i}".encode())
        tags.append(_etag_of(client))
    assert len(set(tags)) == len(tags)


def test_if_none_match_returns_304_without_a_body(client):
    _put(client, "/a.txt", b"hello")
    tag = _etag_of(client)
    r = client.get("/dav/v1/a.txt", headers={"If-None-Match": tag})
    assert r.status_code == 304
    assert r.data == b""
    assert r.headers["ETag"] == tag
    # A stale validator must still deliver the body.
    r = client.get("/dav/v1/a.txt", headers={"If-None-Match": '"nope-1"'})
    assert r.status_code == 200 and r.data == b"hello"


def test_if_none_match_star_makes_put_create_only(client):
    """'Create, but do not clobber' -- the safe way to upload without first
    issuing a HEAD and racing between the two."""
    assert (
        _put(client, "/new.txt", b"first", headers={"If-None-Match": "*"}).status_code
        == 201
    )
    r = _put(client, "/new.txt", b"second", headers={"If-None-Match": "*"})
    assert r.status_code == 412
    assert client.get("/dav/v1/new.txt").data == b"first"  # untouched


def test_if_match_prevents_the_lost_update(client):
    """Two clients read the same version; the second writer must be refused
    rather than silently overwriting the first."""
    _put(client, "/a.txt", b"original")
    stale = _etag_of(client)
    _put(client, "/a.txt", b"someone else's edit")  # the other writer wins the race

    r = _put(client, "/a.txt", b"my edit", headers={"If-Match": stale})
    assert r.status_code == 412
    assert client.get("/dav/v1/a.txt").data == b"someone else's edit"

    # With the current validator the same write succeeds.
    r = _put(client, "/a.txt", b"my edit", headers={"If-Match": _etag_of(client)})
    assert r.status_code == 204
    assert client.get("/dav/v1/a.txt").data == b"my edit"


def test_if_match_on_a_missing_resource_is_412(client):
    assert (
        _put(client, "/ghost.txt", b"x", headers={"If-Match": '"any-1"'}).status_code
        == 412
    )


def test_weak_validator_never_satisfies_if_match(client):
    """Strong comparison, per RFC 9110 8.8.3.2 -- a W/ tag must not pass."""
    _put(client, "/a.txt", b"x")
    weak = "W/" + _etag_of(client)
    assert _put(client, "/a.txt", b"y", headers={"If-Match": weak}).status_code == 412


def test_if_range_stale_serves_the_whole_entity_not_a_spliced_range(client):
    """THE RESUMED-DOWNLOAD CORRUPTION GUARD. A client resumes with the
    validator it started on; if the file changed, honouring the Range would
    splice new bytes onto the prefix it already holds and hand back a corrupt
    file with no error. The RFC answer is to ignore the Range and send 200."""
    _put(client, "/a.txt", b"AAAAAAAAAA")
    stale = _etag_of(client)
    _put(client, "/a.txt", b"BBBBBBBBBBBBBBBBBBBB")  # changed under the client

    r = client.get("/dav/v1/a.txt", headers={"Range": "bytes=5-9", "If-Range": stale})
    assert r.status_code == 200  # NOT 206
    assert r.data == b"BBBBBBBBBBBBBBBBBBBB"

    # Current validator -> the range is honoured as normal.
    r = client.get(
        "/dav/v1/a.txt", headers={"Range": "bytes=5-9", "If-Range": _etag_of(client)}
    )
    assert r.status_code == 206 and r.data == b"BBBBB"


def test_if_range_is_ignored_without_a_range_header(client):
    _put(client, "/a.txt", b"hello")
    r = client.get("/dav/v1/a.txt", headers={"If-Range": '"stale-1"'})
    assert r.status_code == 200 and r.data == b"hello"


def test_failed_precondition_on_move_leaves_the_destination_intact(client):
    """Overwrite: T clears the destination before moving, so the precondition
    has to be evaluated first -- otherwise a 412 would still have destroyed
    the destination's content."""
    _put(client, "/src.txt", b"source")
    _put(client, "/dst.txt", b"destination")
    r = client.open(
        "/dav/v1/src.txt",
        method="MOVE",
        headers={"Destination": "/dav/v1/dst.txt", "If-Match": '"stale-1"'},
    )
    assert r.status_code == 412
    assert client.get("/dav/v1/dst.txt").data == b"destination"
    assert client.get("/dav/v1/src.txt").data == b"source"


def test_if_match_guards_delete(client):
    _put(client, "/a.txt", b"x")
    assert (
        client.open(
            "/dav/v1/a.txt", method="DELETE", headers={"If-Match": '"stale-1"'}
        ).status_code
        == 412
    )
    assert client.get("/dav/v1/a.txt").status_code == 200
    assert (
        client.open(
            "/dav/v1/a.txt", method="DELETE", headers={"If-Match": _etag_of(client)}
        ).status_code
        == 204
    )


def test_etag_list_parsing_handles_commas_inside_the_opaque_part(client):
    """A comma is a legal etagc, so splitting the header on commas is wrong.
    Unit-level because our own etags never contain one."""
    from manager.dav import _etag_matches, _parse_etag_list

    parsed = _parse_etag_list('"a,b", W/"c,d"')
    assert parsed == [(False, "a,b"), (True, "c,d")]
    assert _etag_matches(parsed, '"a,b"', strong=True)
    assert not _etag_matches(parsed, '"c,d"', strong=True)  # weak side
    assert _etag_matches(parsed, '"c,d"', strong=False)
    assert _parse_etag_list("  *  ") == "*"


def test_multiple_candidate_etags_match_any(client):
    _put(client, "/a.txt", b"x")
    tag = _etag_of(client)
    r = _put(client, "/a.txt", b"y", headers={"If-Match": f'"other-1", {tag}'})
    assert r.status_code == 204


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
