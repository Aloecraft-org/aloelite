# ./manager/dav.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
manager.dav — the WebDAV frontend (RFC 4918 compliance class 2).

A PEER of the FUSE frontend, not a layer on it. Of aloelite's frontends only
FUSE consumes POSIX syscalls; this one consumes the ops API through the same
DirectSessionRegistry the browser UI uses, so a WebDAV mount needs no kernel
mount, no fuse3, and no root -- it works anywhere the manager runs.

    aloelite-web --webdav                    # off unless asked for
    net use Z: http://host:8080/dav/<vid>    # Windows
    mount -t davfs http://host:8080/dav/<vid> /mnt/x     # Linux

URL space is /dav/<volume-id>/<path>, one DAV collection per volume. The
volume id (not its name) addresses it, matching every other manager route.

LOCKING (CLASS 2)
-----------------
OPTIONS answers `DAV: 1, 2`, which is the header the desktop clients gate on:
macOS Finder mounts a class-1 share READ-ONLY by design, and the Windows
redirector issues LOCK before PUT and fails at first save without it. Class 1
was the first cut, not the destination.

Locks live in TWO places, because neither alone is sufficient (manager/davlock.py
has the long version):

  * manager/davlock.py -- DAV-vs-DAV exclusion, plus the protocol state the
    engine has no column for: token, <owner>, Depth 0 vs infinity, timeout.
    This is authoritative for DAV, and it exists because every DAV client on a
    volume shares ONE engine mount (an unencrypted volume serves them all from
    the primary mount; an encrypted one caches the mount token against the PIN,
    not the client). Since the engine's check excludes the caller's own mount,
    engine locks give ZERO DAV-vs-DAV exclusion on their own.
  * ops.lock -- CROSS-FRONTEND exclusion. FUSE and the browser UI really are
    different mount rows, so the engine lock is what stops them writing under a
    DAV lock holder. Taken alongside the DAV lock, released with it.

Shared locks are refused (409) rather than faked: ACC-7 makes the engine
exclusive-only and read_count/write_count are recorded-not-enforced on purpose,
so granting one would promise what nothing underneath can keep.

Locks are IN-MEMORY and die with the process. RFC 4918 6.2 permits exactly
that, clients already handle a lock vanishing, and the alternative is a schema
era bump for state whose natural lifetime is minutes.

AUTH
----
Basic, and Basic only -- no cookies are read here. That is a security property,
not an omission: with no ambient browser credential to ride, the CSRF header
dance the JSON API needs (`_csrf_check`) has nothing to defend and would only
lock out every real DAV client, none of which can send a custom header.

For an ENCRYPTED volume the Basic *password is the PIN*, and proving it once
unlocks the volume (or attaches another client mount to an already-unlocked
one) exactly as POST /volumes/<id>/mount does. The username is ignored: a
volume has one secret, and DAV clients insist on sending some username.

Deriving K_u costs an Argon2id -- ~376ms on the development machine -- so
per-request derivation is not on the table: one Finder directory listing
issues dozens of requests. `_PinCache` therefore maps a salted hash of the
presented PIN to the engine mount token that proving it produced, so the KDF
runs once per credential per manager lifetime and every later request is a
dict lookup plus an hmac compare. The PIN itself is never stored.

An UNENCRYPTED volume needs no credential, mirroring the JSON API (where a
plain volume's content routes are open once unlocked). WebDAV is exactly the
feature that tempts people to bind past loopback, so the mitigation is that
the whole frontend is opt-in and the bind address still defaults to 127.0.0.1.

XML
---
Parsed with the stdlib. `defusedxml` would be the usual answer, but it is a
dependency this project does not have and does not need here: every
entity-expansion and external-entity attack requires a DTD, DAV request bodies
never legitimately carry one, so `_parse_xml` rejects any body containing a
DOCTYPE outright and caps the body size. ElementTree already refuses undefined
entities, which closes the remainder.

COST NOTES
----------
A Depth:1 PROPFIND is one `list()` plus one `stat_by_id()` per child, because
DirEntry carries no size or mtime -- the same N+1 the JSON listing endpoint
already pays (manager/api.py `_direct_list`). Local SQLite makes it cheap
enough; if it ever stops being, the fix is a detail-carrying listing template
in the engine, not a cache here.

Every request holds that volume's session lock for its duration, since the
engine owns one sqlite connection per volume and adds no thread safety. A
large GET or PUT therefore serializes other operations on the same volume --
the known cost `_direct_download` already carries, inherited here.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import formatdate
from urllib.parse import quote, unquote, urlsplit
from xml.sax.saxutils import escape as xml_escape
from xml.sax.saxutils import quoteattr

from flask import Blueprint, Response, request

from . import errors as merr
from .davlock import MAX_TIMEOUT, LockConflict, LockRegistry, parse_if
from .engine.direct import FRONTEND_DIRECT, DirectSessionRegistry
from .engine.store import VolumeRecord, VolumeStore

DAV_NS = "DAV:"

# Compliance classes advertised on OPTIONS. Class 2 = LOCK/UNLOCK supported.
_COMPLIANCE = "1, 2"

_METHODS = (
    "OPTIONS",
    "LOCK",
    "UNLOCK",
    "HEAD",
    "GET",
    "PUT",
    "DELETE",
    "MKCOL",
    "COPY",
    "MOVE",
    "PROPFIND",
    "PROPPATCH",
)

_STREAM_CHUNK = 1 << 20
# A PROPFIND/PROPPATCH body is a handful of element names. Anything larger is
# a mistake or an attack; 1 MiB is already absurdly generous.
_MAX_XML_BODY = 1 << 20
# Dead properties live in the node's NODE-6 metadata map, which is a column in
# a row that gets read on every stat. Cap what a client can park there.
_MAX_DEAD_PROPS = 64 << 10
# Metadata keys the DAV layer owns. Anything else in the map (fuse.py's
# 'mode' and 'symlink') belongs to another frontend and must survive a
# PROPPATCH untouched -- set_metadata is a WHOLESALE REPLACE (NODE-6).
_DEAD_PREFIX = "dav:"

# Properties we compute; a client asking for one of these gets a value, and
# PROPPATCH on one gets 403 (they are live, i.e. server-maintained).
_LIVE = frozenset(
    {
        "resourcetype",
        "getcontentlength",
        "getlastmodified",
        "getetag",
        "creationdate",
        "displayname",
        "getcontenttype",
        "supportedlock",
        "lockdiscovery",
    }
)


# ===========================================================================
# Small helpers
# ===========================================================================
def _rfc1123(ns: int) -> str:
    """getlastmodified is HTTP-date (RFC 1123), per RFC 4918 15.7 -- NOT the
    ISO 8601 that creationdate uses. Clients do notice."""
    return formatdate(ns / 1_000_000_000.0, usegmt=True)


def _iso8601(ns: int) -> str:
    """creationdate is ISO 8601 (RFC 4918 15.1)."""
    return (
        datetime.fromtimestamp(ns / 1_000_000_000.0, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _etag(info) -> str:
    """A STRONG validator wherever the engine can back one.

    content.version is the CV-3 committed version pointer: it advances on
    every commit, so unlike modified_at -- a clock reading, demonstrably
    capable of aliasing two writes in one tick -- it identifies content
    exactly. That distinction is not cosmetic. If-Match and If-Range both
    require STRONG comparison (RFC 9110 8.8.3.2), and a weak validator can
    never satisfy one, so a weak etag would silently reduce both headers to
    permanent no-ops: every If-Match would 412 and every resumed download
    would restart.

    Containers have no content row and so no version; they fall back to the
    weak form, which is all a collection can honestly offer.
    """
    if info.version is None:
        return f'W/"{info.id}-{info.modified_at}"'
    return f'"{info.id}-{info.version}"'


# An entity-tag cannot contain a double quote (RFC 9110 8.8.3: etagc excludes
# it), so scanning for quoted runs is exact. Splitting on commas would NOT be:
# a comma is a legal etagc and may appear inside the quotes.
_ETAG_RE = re.compile(r'(W/)?"([^"]*)"')


def _parse_etag_list(raw: str) -> str | list[tuple[bool, str]]:
    """An If-Match/If-None-Match value as '*' or [(is_weak, opaque), ...]."""
    if raw.strip() == "*":
        return "*"
    return [(bool(weak), opaque) for weak, opaque in _ETAG_RE.findall(raw)]


def _etag_matches(candidates, current: str | None, *, strong: bool) -> bool:
    """RFC 9110 8.8.3.2 comparison. Under STRONG comparison a weak tag on
    either side never matches; under weak comparison only the opaque parts
    are compared."""
    if current is None:
        return False
    found = _ETAG_RE.fullmatch(current.strip())
    if not found:
        return False
    cur_weak, cur_opaque = bool(found.group(1)), found.group(2)
    for weak, opaque in candidates:
        if opaque != cur_opaque:
            continue
        if strong and (weak or cur_weak):
            continue
        return True
    return False


def _check_preconditions(current: str | None, *, exists: bool, is_read: bool) -> bool:
    """Evaluate If-Match / If-None-Match, in the order RFC 9110 13.2.2 fixes.

    Returns True when the caller should answer 304; raises 412 otherwise.
    This is what gives DAV clients lost-update protection WITHOUT a lock:
    'If-Match: <etag>' means "only if nobody else wrote since I read", and
    'If-None-Match: *' means "create, but do not clobber".
    """
    raw_match = request.headers.get("If-Match")
    if raw_match is not None:
        parsed = _parse_etag_list(raw_match)
        ok = exists if parsed == "*" else _etag_matches(parsed, current, strong=True)
        if not ok:
            raise DavFault(412, "If-Match precondition failed")

    raw_none = request.headers.get("If-None-Match")
    if raw_none is not None:
        parsed = _parse_etag_list(raw_none)
        matched = (
            exists if parsed == "*" else _etag_matches(parsed, current, strong=False)
        )
        if matched:
            if is_read:
                return True
            raise DavFault(412, "If-None-Match precondition failed")
    return False


def _parse_xml(data: bytes):
    """Parse a DAV request body, or raise DavFault.

    A DTD is refused outright rather than sanitized: entity-expansion
    ("billion laughs") and external-entity reads both require one, no
    legitimate DAV body carries one, and refusing is auditable in a way that
    a hand-rolled entity budget is not.
    """
    if len(data) > _MAX_XML_BODY:
        raise DavFault(413, "request body too large")
    if re.search(rb"<!DOCTYPE", data, re.IGNORECASE):
        raise DavFault(400, "DTD is not allowed in a DAV request body")
    try:
        return ET.fromstring(data)
    except ET.ParseError as e:
        raise DavFault(400, f"malformed XML body: {e}") from e


def _local(tag: str) -> tuple[str, str]:
    """ElementTree qname -> (namespace, localname). '' namespace when bare."""
    if tag.startswith("{"):
        ns, _, local = tag[1:].partition("}")
        return ns, local
    return "", tag


def _dead_key(ns: str, local: str) -> str:
    """Metadata key for one dead property.

    NOT ElementTree's '{ns}local' form, which is ambiguous: a namespace URI is
    an arbitrary string and may legally contain '}', so '{a}b}c' re-splits into
    a DIFFERENT (ns, local) pair on the way back out. That matters beyond
    tidiness -- the recovered `local` would carry characters an XML name cannot
    contain, and it is written into the PROPFIND response as an element name,
    so the round trip is an XML injection vector, not just a lossy encoding.

    Percent-encoding the namespace removes every delimiter, and ':' is a safe
    separator because an XML NCName cannot contain one.
    """
    return f"{_DEAD_PREFIX}{quote(ns, safe='')}:{local}"


def _dead_split(key: str) -> tuple[str, str]:
    """Inverse of _dead_key: 'dav:<pct-ns>:<local>' -> (ns, local)."""
    body = key[len(_DEAD_PREFIX) :]
    encoded, _, local = body.partition(":")
    return unquote(encoded), local


def _dead_tag(ns: str, local: str) -> str:
    """An empty element for one dead property, namespace and all.

    The namespace URI is CLIENT-SUPPLIED (it arrives as an xmlns attribute on a
    PROPPATCH) and goes back out as an attribute value, so it must be quoted
    with quoteattr, NOT saxutils.escape -- escape() handles &, < and > but
    leaves quotation marks alone, so a namespace containing one would close the
    attribute early. Because dead properties are persisted, that is not a
    one-request glitch: every later PROPFIND on that node would emit malformed
    XML, breaking the resource for every client until the metadata was cleared
    out of band.

    `local` needs no escaping by construction -- it came from ElementTree, so
    it is a well-formed XML name and cannot contain markup.
    """
    if not ns:
        return f"<{local}/>"
    return f"<X:{local} xmlns:X={quoteattr(ns)}/>"


def _norm_path(raw: str, *, decode: bool = False) -> str:
    """A DAV URL path -> a mount-relative aloelite path.

    `decode` is the whole subtlety. Werkzeug already percent-decodes a routed
    <path:> parameter, so decoding it AGAIN would make a file genuinely named
    'a%20b.txt' (sent as 'a%2520b.txt') resolve to 'a b.txt' -- a silent
    aliasing bug that lets one name shadow another. The Destination header is
    the opposite case: it is a raw URL nothing has touched, so it must be
    decoded exactly once, here.

    '..' is dropped rather than resolved. Resolving it would be the wrong
    shape of fix: the engine anchors every lookup at the mount point and this
    layer must not hand it a path that even tries to climb.
    """
    segs = []
    for seg in (unquote(raw or "") if decode else (raw or "")).split("/"):
        if not seg or seg == ".":
            continue
        if seg == "..":
            if segs:
                segs.pop()
            continue
        segs.append(seg)
    return "/" + "/".join(segs)


def _href(prefix: str, vid: str, path: str, is_dir: bool) -> str:
    """A multistatus href. Percent-encoded, absolute-path form, and a
    collection ALWAYS ends in '/' -- Finder and Explorer both use the trailing
    slash to tell a collection from a resource even though resourcetype
    already said so."""
    body = "/".join(quote(s, safe="") for s in path.split("/") if s)
    href = f"{prefix}/{quote(vid, safe='')}"
    if body:
        href = f"{href}/{body}"
    if is_dir and not href.endswith("/"):
        href += "/"
    return href


def _depth(default: str = "infinity") -> str:
    d = (request.headers.get("Depth") or default).strip().lower()
    return d if d in ("0", "1", "infinity") else default


def _overwrite() -> bool:
    """Overwrite defaults to T (RFC 4918 10.6)."""
    return (request.headers.get("Overwrite") or "T").strip().upper() != "F"


class DavFault(Exception):
    """A DAV-shaped failure: an HTTP status plus a human sentence."""

    def __init__(
        self,
        status: int,
        message: str = "",
        precondition: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        # An RFC 4918 11.x precondition/postcondition element name, emitted as
        # a DAV:error body when present.
        self.precondition = precondition
        # Response headers the status requires -- 416 owes a Content-Range.
        self.headers = headers or {}


def _fault_response(f: DavFault) -> Response:
    if f.precondition:
        body = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            f'<D:error xmlns:D="DAV:"><D:{f.precondition}/></D:error>'
        ).encode()
        return Response(body, status=f.status, mimetype="application/xml")
    return Response(
        f.message or "", status=f.status, mimetype="text/plain", headers=f.headers
    )


# ===========================================================================
# aloelite error -> DAV status
# ===========================================================================
def _translate(exc: BaseException) -> DavFault:
    """Map an engine/manager exception onto the DAV status a client expects.

    Deliberately NOT the JSON API's `_direct_call` table: that one answers a
    browser and can afford to call a type error 404, while a DAV client acts
    on the code. NotAContainer on a PROPFIND target is 404 (the path names
    nothing listable); NotEmpty cannot reach a client here because DELETE is
    always recursive.
    """
    from aloelite import errors as aerr

    if isinstance(exc, DavFault):
        return exc
    if isinstance(exc, (merr.NotAuthorized, aerr.MountInvalid)):
        return DavFault(401, "authentication required")
    if isinstance(exc, merr.NotMounted):
        return DavFault(503, "volume is not unlocked")
    if isinstance(exc, aerr.NotFound):
        return DavFault(404, "not found")
    if isinstance(exc, aerr.ContainerExists):
        return DavFault(405, "already exists")
    if isinstance(exc, (aerr.NotAContainer, aerr.NotAnEntry)):
        return DavFault(404, "not found")
    if isinstance(exc, aerr.NotEmpty):
        return DavFault(409, "collection is not empty")
    if isinstance(exc, aerr.LockHeld):
        return DavFault(423, "resource is locked by another writer")
    if isinstance(exc, aerr.LockInvalid):
        return DavFault(409, "write lock expired mid-transfer")
    if isinstance(exc, aerr.WouldCycle):
        return DavFault(409, "destination is inside the source collection")
    if isinstance(exc, aerr.Nameless):
        return DavFault(400, "empty name")
    if isinstance(exc, aerr.VolumeMismatch):
        return DavFault(502, "destination is on another volume")
    if isinstance(exc, aerr.Unsupported):
        return DavFault(501, str(exc) or "not supported")
    if isinstance(exc, aerr.FsError):
        return DavFault(500, f"{exc.code}: {exc}")
    raise exc


# ===========================================================================
# Auth
# ===========================================================================
class _PinCache:
    """Salted-hash(PIN) -> engine mount token, per volume.

    Exists solely so Argon2id runs once per credential rather than once per
    request (~376ms measured; a Finder listing is dozens of requests). The
    salt is per-process and random, so the table is not a PIN oracle if it is
    ever dumped, and lookups compare with hmac.compare_digest.
    """

    def __init__(self) -> None:
        self._salt = os.urandom(32)
        self._lock = threading.Lock()
        self._by_volume: dict[str, list[tuple[bytes, str]]] = {}

    def _hash(self, pin: bytes) -> bytes:
        return hashlib.pbkdf2_hmac("sha256", pin, self._salt, 1)

    def get(self, vid: str, pin: bytes) -> str | None:
        want = self._hash(pin)
        with self._lock:
            for digest, token in self._by_volume.get(vid, ()):
                if hmac.compare_digest(digest, want):
                    return token
        return None

    def put(self, vid: str, pin: bytes, token: str) -> None:
        digest = self._hash(pin)
        with self._lock:
            entries = self._by_volume.setdefault(vid, [])
            for i, (existing, _) in enumerate(entries):
                if hmac.compare_digest(existing, digest):
                    entries[i] = (digest, token)
                    return
            entries.append((digest, token))

    def forget(self, vid: str) -> None:
        with self._lock:
            self._by_volume.pop(vid, None)


# ===========================================================================
# The blueprint
# ===========================================================================
def create_dav_blueprint(
    store: VolumeStore,
    registry: DirectSessionRegistry,
    *,
    url_prefix: str = "/dav",
    realm: str = "aloelite",
) -> Blueprint:
    """A Flask blueprint serving every volume in `store` over WebDAV.

    Registered by manager.api.create_app only when WebDAV is enabled, so an
    unconfigured manager has no DAV surface at all.
    """
    bp = Blueprint("dav", __name__)
    pins = _PinCache()
    locks = LockRegistry()

    # -- auth ---------------------------------------------------------------
    def _unauthorized() -> Response:
        return Response(
            "authentication required",
            status=401,
            headers={"WWW-Authenticate": f'Basic realm="{realm}"'},
            mimetype="text/plain",
        )

    def _session_token(rec: VolumeRecord) -> str | None:
        """Prove the client may use this volume and return its mount token
        (None for a plain volume, which needs no per-client identity).

        Mirrors POST /volumes/<id>/mount's direct branch: unlock the volume if
        this is the first client, attach a fresh engine mount row if it is a
        later one. That is what makes a DAV mount self-sufficient -- point
        Windows at the URL, type the PIN, and the volume comes up.
        """
        if not rec.encrypted:
            if not registry.is_unlocked(rec.id):
                try:
                    registry.unlock(rec, None, store.sqlite_path_of(rec))
                except merr.AlreadyMounted:
                    pass  # another thread won; its session is equally good
                _mark_mounted(rec)
            return None

        auth = request.authorization
        if auth is None or auth.password is None:
            raise DavFault(401, "PIN required")
        pin = auth.password.encode()

        cached = pins.get(rec.id, pin)
        if cached is not None and registry.is_unlocked(rec.id):
            return cached
        if cached is not None:
            # Volume was locked out from under the cache (DELETE /mount).
            pins.forget(rec.id)

        try:
            if registry.is_unlocked(rec.id):
                token = registry.attach(rec, pin)
            else:
                token = registry.unlock(rec, pin, store.sqlite_path_of(rec))
        except merr.AlreadyMounted:
            token = registry.attach(rec, pin)
        except (merr.BadPin, merr.EncryptionMismatch) as e:
            raise DavFault(401, str(e) or "wrong PIN") from e
        except merr.MountFailed as e:
            raise DavFault(500, str(e) or "unlock failed") from e
        pins.put(rec.id, pin, token)
        _mark_mounted(rec)
        return token

    def _mark_mounted(rec: VolumeRecord) -> None:
        """Keep the store's view honest so the web UI and GET /volumes agree
        that this volume is up, exactly as the mount endpoint does."""
        if not rec.mounted or rec.frontend != FRONTEND_DIRECT:
            rec.mounted = True
            rec.mountpoint = None
            rec.frontend = FRONTEND_DIRECT
            store.put(rec)

    def _require_volume(vid: str) -> VolumeRecord:
        """Resolve a volume by ID, or failing that by NAME.

        The id is authoritative and is tried first, so nothing that worked
        before changes. The name fallback exists because a DAV URL is
        something a person types once into a Map Network Drive dialog and
        then has to recognise later, and

            http://host:7081/dav/ca678c06bfaa41928ea9d01d45234ee5

        is not a URL anyone can remember, check, or paste from memory -- while
        the S3 frontend on this same manager addresses the same volumes by
        name. One product should not disagree with itself about that.

        Names are unique only WITHIN a filesystem (api.py checks
        `volumes_of(fs_id)`), so two filesystems may each hold a "backups".
        An ambiguous name is refused by name rather than resolved to whichever
        record happens to sort first: silently mounting the wrong volume is a
        far worse outcome than a 409 that says which ids to choose between.
        """
        rec = store.get(vid)
        if rec is not None:
            return rec
        matches = [r for r in store.list() if r.name == vid]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise DavFault(
                409,
                "%r names %d volumes (ids: %s); use the id"
                % (vid, len(matches), ", ".join(sorted(r.id for r in matches))),
            )
        raise DavFault(404, "no such volume")

    # -- properties ---------------------------------------------------------
    def _propstat(
        info, is_dir: bool, wanted, names_only: bool, lock=None, href: str = ""
    ) -> str:
        """The <propstat> pair for one resource: found properties with 200,
        requested-but-absent ones with 404, per RFC 4918 9.1."""
        found: list[str] = []
        missing: list[str] = []
        # (ns, local) -> value, for every dead property stored on this node.
        dead = {
            _dead_split(k): v
            for k, v in info.metadata.items()
            if k.startswith(_DEAD_PREFIX)
        }

        def emit(name: str, value: str) -> None:
            found.append(
                value if value.startswith("<") else f"<D:{name}>{value}</D:{name}>"
            )

        # `wanted is None` means allprop.
        live_wanted = (
            _LIVE if wanted is None else {n for ns, n in wanted if ns == DAV_NS}
        )
        dead_wanted = set(dead) if wanted is None else set(wanted)

        for name in sorted(live_wanted):
            if names_only:
                found.append(f"<D:{name}/>")
                continue
            if name == "resourcetype":
                emit(
                    name,
                    "<D:resourcetype><D:collection/></D:resourcetype>"
                    if is_dir
                    else "<D:resourcetype/>",
                )
            elif name == "getcontentlength":
                if is_dir:
                    missing.append("<D:getcontentlength/>")
                else:
                    emit(name, str(info.size or 0))
            elif name == "getlastmodified":
                emit(name, _rfc1123(info.modified_at))
            elif name == "creationdate":
                emit(name, _iso8601(info.created_at))
            elif name == "getetag":
                emit(name, xml_escape(_etag(info)))
            elif name == "displayname":
                emit(name, xml_escape(info.name))
            elif name == "getcontenttype":
                if is_dir:
                    emit(name, "httpd/unix-directory")
                else:
                    emit(name, _content_type(info.name))
            elif name == "supportedlock":
                # Class 2, exclusive write only -- ACC-7. Advertising shared
                # here would promise something the engine cannot keep.
                found.append(
                    "<D:supportedlock><D:lockentry>"
                    "<D:lockscope><D:exclusive/></D:lockscope>"
                    "<D:locktype><D:write/></D:locktype>"
                    "</D:lockentry></D:supportedlock>"
                )
            elif name == "lockdiscovery":
                # An EMPTY lockdiscovery means "not locked", which is a real
                # answer a client acts on -- it is not the same as omitting it.
                found.append(
                    _lockdiscovery(lock, href)
                    if lock is not None
                    else "<D:lockdiscovery/>"
                )
            elif name in _LIVE:
                missing.append(f"<D:{name}/>")

        for ns, local in sorted(dead_wanted):
            if ns == DAV_NS and local in _LIVE:
                continue  # already answered by the live loop above
            tag = _dead_tag(ns, local)
            if (ns, local) in dead:
                if names_only:
                    found.append(tag)
                else:
                    opened = tag[:-2] + ">" if tag.endswith("/>") else tag
                    close = f"</X:{local}>" if ns else f"</{local}>"
                    found.append(f"{opened}{xml_escape(dead[(ns, local)])}{close}")
            elif wanted is not None:
                missing.append(tag)

        out = ""
        if found or not missing:
            out += (
                "<D:propstat><D:prop>"
                + "".join(found)
                + "</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat>"
            )
        if missing:
            out += (
                "<D:propstat><D:prop>"
                + "".join(missing)
                + "</D:prop><D:status>HTTP/1.1 404 Not Found</D:status></D:propstat>"
            )
        return out

    def _content_type(name: str) -> str:
        import mimetypes

        return mimetypes.guess_type(name)[0] or "application/octet-stream"

    def _xml(body: str) -> bytes:
        return ('<?xml version="1.0" encoding="utf-8"?>\n' + body).encode()

    def _multistatus(parts: list[str]) -> Response:
        body = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<D:multistatus xmlns:D="DAV:">' + "".join(parts) + "</D:multistatus>"
        ).encode()
        return Response(body, status=207, mimetype='application/xml; charset="utf-8"')

    # -- verbs --------------------------------------------------------------
    def _do_options(rec, m, path, stack) -> Response:
        return Response(
            b"",
            status=200,
            headers={
                "DAV": _COMPLIANCE,
                # Not a standard header. Office probes for FrontPage endpoints
                # (_vti_inf.html, /_vti_bin/shtml.dll) and can refuse to save
                # without it; one header buys that off.
                "MS-Author-Via": "DAV",
                "Allow": ", ".join(_METHODS),
                "Accept-Ranges": "bytes",
                "Content-Length": "0",
            },
        )

    def _do_propfind(rec, m, path, stack) -> Response:
        depth = _depth()
        if depth == "infinity":
            # Allowed by RFC 4918 9.1: a server MAY refuse, and must say so
            # with this precondition. Refusing keeps one client from walking
            # an entire volume under the per-volume session lock.
            raise DavFault(
                403,
                "Depth: infinity is not supported",
                precondition="propfind-finite-depth",
            )

        raw = request.get_data(cache=False)
        wanted: set[tuple[str, str]] | None = None
        names_only = False
        if raw.strip():
            root = _parse_xml(raw)
            if _local(root.tag)[1] != "propfind":
                raise DavFault(400, "body must be a DAV:propfind")
            for child in root:
                _, local = _local(child.tag)
                if local == "allprop":
                    wanted = None
                elif local == "propname":
                    names_only = True
                    wanted = None
                elif local == "prop":
                    wanted = {_local(p.tag) for p in child}

        info = m.stat(path)
        is_dir = info.type.value == "container"
        parts = [
            "<D:response><D:href>"
            + xml_escape(_href(url_prefix, rec.id, path, is_dir))
            + "</D:href>"
            + _propstat(
                info,
                is_dir,
                wanted,
                names_only,
                locks.find(rec.id, path),
                _href(url_prefix, rec.id, path, is_dir),
            )
            + "</D:response>"
        ]
        if depth == "1" and is_dir:
            for entry in m.list(path):
                if not entry.visible:
                    # NODE-5 lets same-name siblings exist with one visible;
                    # showing the shadowed ones would put duplicate hrefs in
                    # the multistatus, which clients handle badly.
                    continue
                child = m.stat_by_id(entry.node)
                child_dir = entry.type.value == "container"
                child_path = f"{path.rstrip('/')}/{entry.name}"
                parts.append(
                    "<D:response><D:href>"
                    + xml_escape(_href(url_prefix, rec.id, child_path, child_dir))
                    + "</D:href>"
                    + _propstat(
                        child,
                        child_dir,
                        wanted,
                        names_only,
                        locks.find(rec.id, child_path),
                        _href(url_prefix, rec.id, child_path, child_dir),
                    )
                    + "</D:response>"
                )
        return _multistatus(parts)

    def _do_proppatch(rec, m, path, stack) -> Response:
        raw = request.get_data(cache=False)
        if not raw.strip():
            raise DavFault(400, "PROPPATCH requires a body")
        root = _parse_xml(raw)
        if _local(root.tag)[1] != "propertyupdate":
            raise DavFault(400, "body must be a DAV:propertyupdate")

        info = m.stat(path)
        _require_unlocked(rec, path)
        _check_preconditions(_etag(info), exists=True, is_read=False)
        # set_metadata is a WHOLESALE REPLACE (NODE-6), so start from what is
        # there and keep every non-DAV key: 'mode' and 'symlink' belong to the
        # FUSE frontend and a PROPPATCH must not erase them.
        meta = dict(info.metadata)
        ok: list[str] = []
        forbidden: list[str] = []

        for action in root:
            _, verb = _local(action.tag)
            if verb not in ("set", "remove"):
                continue
            for prop in action:
                if _local(prop.tag)[1] != "prop":
                    continue
                for target in prop:
                    ns, local = _local(target.tag)
                    if ns == DAV_NS and local in _LIVE:
                        # Live properties are server-maintained. Windows tries
                        # to PROPPATCH Win32* timestamps; those are NOT in
                        # DAV: so they land in the dead-property path below
                        # and are simply stored, which is what makes Explorer
                        # stop complaining.
                        forbidden.append(f"<D:{local}/>")
                        continue
                    key = _dead_key(ns, local)
                    if verb == "set":
                        meta[key] = "".join(target.itertext())
                    else:
                        meta.pop(key, None)
                    tag = _dead_tag(ns, local)
                    ok.append(tag)

        dead_bytes = sum(
            len(k) + len(v) for k, v in meta.items() if k.startswith(_DEAD_PREFIX)
        )
        if dead_bytes > _MAX_DEAD_PROPS:
            raise DavFault(507, "dead properties exceed the per-node budget")

        if forbidden:
            # RFC 4918 9.2: the whole PROPPATCH is atomic -- if any property
            # fails, none are applied, and the others report 424.
            parts = (
                "<D:propstat><D:prop>"
                + "".join(forbidden)
                + "</D:prop><D:status>HTTP/1.1 403 Forbidden</D:status></D:propstat>"
            )
            if ok:
                parts += (
                    "<D:propstat><D:prop>"
                    + "".join(ok)
                    + "</D:prop><D:status>HTTP/1.1 424 Failed Dependency"
                    "</D:status></D:propstat>"
                )
        else:
            m.set_metadata(path, meta)
            parts = (
                "<D:propstat><D:prop>"
                + "".join(ok)
                + "</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat>"
            )

        is_dir = info.type.value == "container"
        return _multistatus(
            [
                "<D:response><D:href>"
                + xml_escape(_href(url_prefix, rec.id, path, is_dir))
                + "</D:href>"
                + parts
                + "</D:response>"
            ]
        )

    def _parse_range(size: int) -> tuple[int, int] | None:
        """A single byte range, or None for 'serve the whole thing'.

        Multi-range requests are answered with the full body: RFC 7233 lets a
        server ignore a Range it does not want to satisfy, and multipart/
        byteranges buys nothing for the clients that actually range-request
        here (media seeking, resumable downloads, davfs2's read-ahead).
        """
        header = request.headers.get("Range")
        if not header:
            return None
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
        if match is None:
            return None
        first, last = match.group(1), match.group(2)
        if not first and not last:
            return None
        if not first:  # suffix range: the final N bytes
            length = int(last)
            if length == 0:
                raise DavFault(
                    416,
                    "unsatisfiable range",
                    headers={"Content-Range": f"bytes */{size}"},
                )
            start = max(0, size - length)
            end = size - 1
        else:
            start = int(first)
            end = int(last) if last else size - 1
        if start >= size or start > end:
            raise DavFault(
                416,
                "unsatisfiable range",
                headers={"Content-Range": f"bytes */{size}"},
            )
        return start, min(end, size - 1)

    def _do_get(rec, m, path, stack, *, body: bool) -> Response:
        info = m.stat(path)
        if info.type.value == "container":
            # A GET on a collection has no defined representation. 405 keeps
            # davfs2 and rclone from mistaking an error page for content.
            raise DavFault(405, "cannot GET a collection")
        size = info.size or 0
        etag = _etag(info)
        if _check_preconditions(etag, exists=True, is_read=True):
            # 304 carries the validators and NO body, not even Content-Length
            # of the entity -- a client that sees one is meant to reuse what it
            # already has.
            return Response(
                b"",
                status=304,
                headers={"ETag": etag, "Last-Modified": _rfc1123(info.modified_at)},
            )
        headers = {
            "Content-Type": _content_type(info.name),
            "ETag": etag,
            "Last-Modified": _rfc1123(info.modified_at),
            "Accept-Ranges": "bytes",
            # Volume content may hold secrets; never let a proxy or browser
            # keep it, matching the JSON download route.
            "Cache-Control": "no-store",
        }
        rng = _parse_range(size)
        if rng is not None and (raw_if_range := request.headers.get("If-Range")):
            # THE RESUMED-DOWNLOAD CORRUPTION GUARD. A client resuming an
            # interrupted transfer sends the validator it started with; if the
            # file changed meanwhile, serving the requested range would splice
            # new bytes onto the prefix it already holds and produce a corrupt
            # file with no error anywhere. Ignoring the Range and sending the
            # whole current entity is the RFC 9110 14.2 answer and is never
            # wrong -- only more expensive.
            #
            # Comparison is STRONG, and the date form is deliberately not
            # honoured: Last-Modified is second-resolution here, so it cannot
            # distinguish a same-second change, and treating it as sufficient
            # would reintroduce exactly the corruption this guards against.
            parsed = _parse_etag_list(raw_if_range)
            if parsed == "*" or not _etag_matches(parsed, etag, strong=True):
                rng = None
        if rng is None:
            start, end = 0, size - 1
            status = 200
            length = size
        else:
            start, end = rng
            status = 206
            length = end - start + 1
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        headers["Content-Length"] = str(length)

        if not body:
            # HEAD must report the Content-Length a GET would send. Werkzeug
            # recomputes the header from the (empty) body during construction,
            # so it has to be restamped afterwards or every HEAD claims 0.
            resp = Response(status=status, headers=headers)
            resp.headers["Content-Length"] = str(length)
            return resp

        # The session stays held for the whole transfer so the descriptor and
        # the headers describe one consistent version. `stack` is the
        # dispatcher's, already holding the session; marking the response
        # streaming transfers ownership to the generator below, which is why
        # the dispatcher must NOT close it on return. Werkzeug closes the
        # generator when the client disconnects, so the stack always unwinds.
        reader = stack.enter_context(m.open_read(path))
        if start:
            reader.seek(start)

        def generate():
            try:
                remaining = length
                while remaining > 0:
                    chunk = reader.read(min(_STREAM_CHUNK, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk
            finally:
                stack.close()

        resp = Response(generate(), status=status, headers=headers)
        resp.dav_streaming = True
        # The generator's `finally` covers a body that gets consumed or
        # abandoned mid-flight. This covers the body that is never iterated at
        # all -- a client that disconnects between headers and first chunk.
        # Without it the volume's session lock would be held until the process
        # exited, wedging every other request for that volume. ExitStack.close
        # is idempotent, so the two paths cannot double-release.
        resp.call_on_close(stack.close)
        return resp

    def _do_put(rec, m, path, stack) -> Response:
        from aloelite import errors as aerr
        from aloelite.types import WriteMode

        if path == "/":
            raise DavFault(405, "cannot PUT the collection root")
        if request.headers.get("Content-Range"):
            # A partial PUT is explicitly undefined by RFC 4918 9.7.1 and
            # servers are told to reject it rather than guess.
            raise DavFault(400, "Content-Range is not allowed on PUT")
        try:
            existing = m.stat(path)
        except (aerr.NotFound, aerr.NotAContainer, aerr.NotAnEntry):
            existing = None
        if existing is not None and existing.type.value == "container":
            raise DavFault(405, "cannot PUT over a collection")
        _require_unlocked(rec, path)
        # Checked BEFORE open_write, which would otherwise truncate the very
        # content the precondition exists to protect.
        _check_preconditions(
            _etag(existing) if existing is not None else None,
            exists=existing is not None,
            is_read=False,
        )

        # open_write creates the entry when missing, but only if the PARENT
        # resolves; a missing parent surfaces as NotFound and RFC 4918 9.7.1
        # requires 409 for exactly that case.
        try:
            handle = m.open_write(path, WriteMode.TRUNCATE)
        except aerr.NotFound as e:
            raise DavFault(409, "parent collection does not exist") from e

        # An INTERRUPTED PUT LEAVES THE PREVIOUS CONTENT INTACT. The engine
        # makes that choice, not this handler: Descriptor.__exit__ commits on a
        # clean exit and aborts when the block is unwinding, so the partial
        # bytes are discarded and the committed pointer never moves. The lock
        # is released either way, so a dead transfer cannot wedge the node.
        # test_dav.py::test_interrupted_put_is_atomic_and_does_not_wedge pins
        # both halves.
        with handle as writer:
            stream = request.stream
            while True:
                chunk = stream.read(_STREAM_CHUNK)
                if not chunk:
                    break
                writer.write(chunk)

        info = m.stat(path)
        return Response(
            b"",
            status=204 if existing is not None else 201,
            headers={"ETag": _etag(info)},
        )

    def _do_mkcol(rec, m, path, stack) -> Response:
        from aloelite import errors as aerr

        if request.get_data(cache=False).strip():
            # RFC 4918 9.3.1: a MKCOL body in a format the server does not
            # support is 415.
            raise DavFault(415, "MKCOL with a body is not supported")
        if path == "/":
            raise DavFault(405, "the volume root already exists")
        try:
            m.stat(path)
        except (aerr.NotFound, aerr.NotAContainer, aerr.NotAnEntry):
            pass
        else:
            raise DavFault(405, "already exists")
        _require_unlocked(rec, path)
        try:
            m.create_container(path)
        except aerr.NotFound as e:
            raise DavFault(409, "parent collection does not exist") from e
        return Response(b"", status=201)

    def _do_delete(rec, m, path, stack) -> Response:
        if path == "/":
            raise DavFault(403, "cannot DELETE the volume root")
        info = m.stat(path)
        _require_unlocked(rec, path, depth_infinity=info.type.value == "container")
        _check_preconditions(_etag(info), exists=True, is_read=False)
        if info.type.value == "container":
            # DELETE on a collection is Depth: infinity by definition
            # (RFC 4918 9.6.1) -- there is no shallow form to honour.
            m.remove_recursive(path)
        else:
            m.remove(path)
        return Response(b"", status=204)

    def _destination(rec) -> str:
        """The Destination header as a mount-relative path in THIS volume."""
        raw = request.headers.get("Destination")
        if not raw:
            raise DavFault(400, "Destination header is required")
        parsed = urlsplit(raw)
        if parsed.netloc and parsed.netloc != request.host:
            raise DavFault(502, "destination is on another server")
        prefix = f"{url_prefix}/{quote(rec.id, safe='')}"
        target = parsed.path
        if not target.startswith(prefix):
            # Another volume, or outside the DAV space entirely. 502 is the
            # RFC's answer for a destination this server cannot write.
            raise DavFault(502, "destination is outside this volume")
        return _norm_path(target[len(prefix) :], decode=True)

    def _do_copy_move(rec, m, path, stack, *, move: bool) -> Response:
        from aloelite import errors as aerr

        if path == "/":
            raise DavFault(403, "cannot move or copy the volume root")
        dst = _destination(rec)
        if dst == "/":
            raise DavFault(403, "cannot overwrite the volume root")
        if dst == path:
            raise DavFault(403, "source and destination are the same")
        # A COPY of a collection is Depth: infinity by default and ops.copy is
        # always deep; refusing Depth: 0 is more honest than silently doing
        # more than asked.
        if not move and _depth() == "0":
            src_info = m.stat(path)
            if src_info.type.value == "container":
                raise DavFault(400, "Depth: 0 COPY of a collection is not supported")

        # A MOVE removes the source, so it needs the source's lock (and, for
        # a collection, every lock beneath it). A COPY leaves the source
        # untouched and needs only the destination's.
        if move:
            src_is_dir = m.stat(path).type.value == "container"
            _require_unlocked(rec, path, depth_infinity=src_is_dir)
        _require_unlocked(rec, dst)

        # Preconditions gate the SOURCE (the request-URI), and are evaluated
        # before the destination is cleared below: a failed precondition must
        # not leave the destination already deleted.
        _check_preconditions(_etag(m.stat(path)), exists=True, is_read=False)

        try:
            dst_info = m.stat(dst)
        except (aerr.NotFound, aerr.NotAContainer, aerr.NotAnEntry):
            dst_info = None

        if dst_info is not None:
            if not _overwrite():
                raise DavFault(412, "destination exists and Overwrite is F")
            # The engine has no replace-in-place: ops.move would create a
            # second child with the same name (NODE-5 allows same-name
            # siblings, one visible), which is not what Overwrite: T means.
            # Clear the destination first so the result is a true replace.
            if dst_info.type.value == "container":
                m.remove_recursive(dst)
            else:
                m.remove(dst)

        try:
            if move:
                m.move(path, dst)
            else:
                m.copy(path, dst)
        except aerr.NotFound as e:
            raise DavFault(409, "destination parent does not exist") from e
        return Response(b"", status=204 if dst_info is not None else 201)

    # -- dispatch -----------------------------------------------------------
    # -- class 2 -----------------------------------------------------------
    def _submitted_tokens() -> set[str]:
        return parse_if(request.headers.get("If", "")).tokens()

    def _require_unlocked(rec, path: str, *, depth_infinity: bool = False) -> None:
        """423 unless the client submitted the token for every lock it would
        disturb.

        `depth_infinity` covers DELETE and MOVE on a collection, which affect
        the whole subtree: a lock held on any descendant blocks them even
        though the request-URI itself is free. Checking only the request-URI
        would let a recursive delete destroy resources another client holds
        locked, which is the same transitive-destruction rule ACC-11 applies
        in the engine.
        """
        submitted = _submitted_tokens()
        held = locks.find(rec.id, path)
        if held is not None and held.token not in submitted:
            raise DavFault(423, f"locked by {held.path}")
        if depth_infinity:
            for other in locks.covered_by_subtree(rec.id, path):
                if other.token not in submitted:
                    raise DavFault(423, f"locked by {other.path}")

    def _lock_timeout() -> int | None:
        """Parse the Timeout header. 'Infinite' is honoured as our maximum
        rather than literally: RFC 4918 10.7 lets a server pick, and an
        unbounded lock from a client that then vanishes is exactly the state
        the timeout exists to prevent."""
        raw = request.headers.get("Timeout", "")
        for part in raw.split(","):
            part = part.strip().lower()
            if part.startswith("second-"):
                try:
                    return int(part[7:])
                except ValueError:
                    continue
            if part == "infinite":
                return MAX_TIMEOUT
        return None

    def _lockdiscovery(lock, href: str) -> str:
        owner = f"<D:owner>{lock.owner_xml}</D:owner>" if lock.owner_xml else ""
        return (
            "<D:lockdiscovery><D:activelock>"
            "<D:locktype><D:write/></D:locktype>"
            "<D:lockscope><D:exclusive/></D:lockscope>"
            f"<D:depth>{lock.depth}</D:depth>"
            f"{owner}"
            f"<D:timeout>Second-{lock.timeout}</D:timeout>"
            f"<D:locktoken><D:href>{xml_escape(lock.token)}</D:href></D:locktoken>"
            f"<D:lockroot><D:href>{xml_escape(href)}</D:href></D:lockroot>"
            "</D:activelock></D:lockdiscovery>"
        )

    def _do_lock(rec, m, path, stack) -> Response:
        from aloelite import errors as aerr

        raw = request.get_data(cache=False)
        depth = "infinity" if _depth() != "0" else "0"
        timeout = _lock_timeout()

        # A body-less LOCK is a REFRESH of the token in the If: header, not a
        # new lock (RFC 4918 9.10.2). Treating it as a new one makes every
        # Office autosave leak a lock until it times out.
        if not raw.strip():
            for token in _submitted_tokens():
                existing = locks.get(token)
                if existing is not None and existing.volume == rec.id:
                    existing.refresh(timeout)
                    href = _href(url_prefix, rec.id, existing.path, False)
                    discovery = _lockdiscovery(existing, href)
                    return Response(
                        _xml(f'<D:prop xmlns:D="DAV:">{discovery}</D:prop>'),
                        status=200,
                        mimetype="application/xml",
                        headers={"Lock-Token": f"<{existing.token}>"},
                    )
            raise DavFault(412, "no lock token to refresh")

        root = _parse_xml(raw)
        if _local(root.tag)[1] != "lockinfo":
            raise DavFault(400, "body must be a DAV:lockinfo")
        scope = root.find(f"{{{DAV_NS}}}lockscope")
        if scope is not None and scope.find(f"{{{DAV_NS}}}shared") is not None:
            # ACC-7: the engine is exclusive-only, and read_count/write_count
            # are recorded-not-enforced on purpose. Advertising shared locks
            # would be a promise nothing below can keep.
            raise DavFault(409, "only exclusive locks are supported")
        owner_el = root.find(f"{{{DAV_NS}}}owner")
        owner_xml = ""
        if owner_el is not None:
            owner_xml = "".join(
                ET.tostring(child, encoding="unicode") for child in owner_el
            ) or xml_escape(owner_el.text or "")

        # RFC 4918 7.3: LOCK on an unmapped URL creates an empty resource, so
        # a client can lock a file before writing it -- which is exactly what
        # Explorer and Finder do on 'save as'.
        created = False
        try:
            info = m.stat(path)
            is_dir = info.type.value == "container"
        except (aerr.NotFound, aerr.NotAContainer, aerr.NotAnEntry):
            if path == "/":
                raise
            try:
                m.create_entry(path, b"")
            except aerr.NotFound as e:
                raise DavFault(409, "parent collection does not exist") from e
            created = True
            is_dir = False

        try:
            lock = locks.acquire(
                rec.id, path, depth=depth, owner_xml=owner_xml, timeout=timeout
            )
        except LockConflict as conflict:
            raise DavFault(423, str(conflict)) from conflict

        # The engine lock is what excludes the OTHER frontends (FUSE, the
        # browser UI); the manager registry above is what excludes other DAV
        # clients, which all share one engine mount. Best-effort: a volume
        # busy at the engine level is a real conflict and fails the LOCK, but
        # a plain container lock has no engine counterpart to take.
        if not is_dir:
            try:
                lock.engine_lock = m.lock(path, ttl_ms=lock.timeout * 1000).id
            except aerr.LockHeld as e:
                locks.release(lock.token)
                raise DavFault(423, "locked by another frontend") from e

        href = _href(url_prefix, rec.id, path, is_dir)
        return Response(
            _xml(f'<D:prop xmlns:D="DAV:">{_lockdiscovery(lock, href)}</D:prop>'),
            status=201 if created else 200,
            mimetype="application/xml",
            headers={"Lock-Token": f"<{lock.token}>"},
        )

    def _do_unlock(rec, m, path, stack) -> Response:
        raw = request.headers.get("Lock-Token", "").strip()
        token = raw[1:-1] if raw.startswith("<") and raw.endswith(">") else raw
        if not token:
            raise DavFault(400, "UNLOCK requires a Lock-Token header")
        existing = locks.get(token)
        if existing is None or existing.volume != rec.id:
            # 409, not 404: the RESOURCE exists, it is the lock that does not.
            raise DavFault(409, "no such lock on this resource")
        locks.release(token)
        if existing.engine_lock:
            from aloelite import errors as aerr

            try:
                m.unlock(existing.engine_lock)
            except (aerr.LockInvalid, aerr.LockHeld):
                # Already expired or reclaimed by prune; the DAV lock is gone
                # either way, and failing UNLOCK here would strand the client.
                pass
        return Response(b"", status=204)

    _HANDLERS = {
        "OPTIONS": _do_options,
        "LOCK": _do_lock,
        "UNLOCK": _do_unlock,
        "PROPFIND": _do_propfind,
        "PROPPATCH": _do_proppatch,
        "GET": lambda rec, m, p, s: _do_get(rec, m, p, s, body=True),
        "HEAD": lambda rec, m, p, s: _do_get(rec, m, p, s, body=False),
        "PUT": _do_put,
        "MKCOL": _do_mkcol,
        "DELETE": _do_delete,
        "COPY": lambda rec, m, p, s: _do_copy_move(rec, m, p, s, move=False),
        "MOVE": lambda rec, m, p, s: _do_copy_move(rec, m, p, s, move=True),
    }

    def _session_ctx(rec: VolumeRecord):
        """Authenticate, then take the volume's session lock.

        Two separate steps on purpose: unlock/attach acquire that same lock
        internally, so proving the PIN must finish BEFORE the session context
        is entered. `registry.session` guards a plain threading.Lock, which is
        NOT reentrant -- entering it twice on one thread deadlocks the worker,
        which is why nothing below this line may open a second session.
        """
        token = _session_token(rec)
        if token is None:
            return registry.session(rec.id)
        return registry.session(rec.id, token=token)

    def _dispatch(vid: str, rest: str = "") -> Response:
        from contextlib import ExitStack

        stack = ExitStack()
        try:
            rec = _require_volume(vid)
            path = _norm_path(rest)
            handler = _HANDLERS.get(request.method)
            if handler is None:
                # LOCK/UNLOCK land here. 405 with an Allow header is what a
                # class 1 server owes a client that tries anyway; going class
                # 2 means adding them to _HANDLERS and _COMPLIANCE, nothing
                # else in this module.
                return Response(
                    b"",
                    status=405,
                    headers={"Allow": ", ".join(_METHODS), "DAV": _COMPLIANCE},
                )
            # OPTIONS answers before auth: a client probes it to discover both
            # the DAV classes and that it should send credentials at all, and
            # a 401 here just makes Explorer give up on the whole share.
            if request.method == "OPTIONS":
                return handler(rec, None, path, stack)
            m = stack.enter_context(_session_ctx(rec))
            resp = handler(rec, m, path, stack)
        except DavFault as f:
            stack.close()
            return _unauthorized() if f.status == 401 else _fault_response(f)
        except BaseException as exc:  # mapped below, or re-raised by _translate
            stack.close()
            fault = _translate(exc)
            return _unauthorized() if fault.status == 401 else _fault_response(fault)
        # A streaming GET owns the stack now and closes it when its generator
        # finishes or the client disconnects; everything else is done with it.
        if not getattr(resp, "dav_streaming", False):
            stack.close()
        return resp

    bp.add_url_rule(
        f"{url_prefix}/<vid>",
        endpoint="dav_root",
        view_func=_dispatch,
        methods=list(_METHODS) + ["LOCK", "UNLOCK"],
        strict_slashes=False,
    )
    bp.add_url_rule(
        f"{url_prefix}/<vid>/<path:rest>",
        endpoint="dav_path",
        view_func=_dispatch,
        methods=list(_METHODS) + ["LOCK", "UNLOCK"],
        strict_slashes=False,
    )
    return bp


__all__ = ["create_dav_blueprint", "DavFault"]
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
