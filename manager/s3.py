# ./manager/s3.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
manager.s3 — an S3-compatible frontend, scoped to what a replication client
actually calls.

WHAT THIS IS FOR
----------------
Standing up an endpoint a backup tool can ship to, alongside (not instead of)
a real S3 bucket. The surface was derived by reading litestream 0.3.13's
`s3/replica_client.go` rather than from the S3 API docs, because the docs
describe a hundred operations and that client calls six:

    ListObjects (V1)   GET  /<bucket>?prefix=&delimiter=&marker=
    GetObject          GET  /<bucket>/<key>
    PutObject          PUT  /<bucket>/<key>            (bodies < part size)
    CreateMultipart    POST /<bucket>/<key>?uploads    (bodies >= part size)
    UploadPart         PUT  /<bucket>/<key>?partNumber=&uploadId=
    CompleteMultipart  POST /<bucket>/<key>?uploadId=
    AbortMultipart     DELETE /<bucket>/<key>?uploadId=
    DeleteObjects      POST /<bucket>?delete           (batch; the ONLY delete)

Two of those surprised the estimate and are worth stating plainly:

* It lists with **ListObjects V1**, not V2 -- `Marker`/`NextMarker`, not a
  continuation token. Implementing only V2 would leave the client unable to
  enumerate a single generation.
* Every write goes through `s3manager.Uploader`, whose default part size is
  5 MiB. WAL segments land as a single PutObject, but a snapshot larger than
  that becomes a real multipart upload with up to 5 concurrent UploadPart
  requests. Multipart is not optional for anything that takes snapshots.

There is no HeadObject, HeadBucket, CopyObject or CreateBucket in that client,
so there is none here. Buckets are volumes, created out of band through the
manager's own API, which is the same posture the DAV frontend takes.

ADDRESSING
----------
Path-style (`/<bucket>/<key>`) is the default and is what an S3-compatible
endpoint normally wants. Note that litestream does NOT infer path-style from
`endpoint` alone: `s3.ParseHost` only sets it for hosts it recognises as a
known provider, and a bare `s3://bucket/prefix` URL falls through to
`forcePathStyle = false`. A deployment pointing litestream here must set
`force-path-style: true` in its replica block, or set `virtual_host_suffix`
here and provide the wildcard DNS that virtual-host addressing needs.

BUCKET NAMES THE MANAGER OWNS
-----------------------------
The frontend mounts at the application root so that `endpoint` needs no path
component. Flask routes static rules ahead of dynamic ones, so the manager's
own paths still win; a volume NAMED one of them would be shadowed instead of
served, which is a confusing failure, so those names are refused by name.
"""

from __future__ import annotations

import hashlib
import threading
import uuid
from contextlib import ExitStack
from urllib.parse import quote, unquote
from xml.sax.saxutils import escape as xml_escape

from flask import Blueprint, Response, request

from . import errors as merr
from .direct import FRONTEND_DIRECT, DirectSessionRegistry
from .sigv4 import Credentials, SigV4Error
from .sigv4 import verify as sigv4_verify
from .store import VolumeRecord, VolumeStore

# Names the manager serves itself. A volume with one of these names cannot be
# reached through this frontend, and saying so beats a silent 404 from a route
# that matched something else.
RESERVED_BUCKETS = frozenset(
    {"admin", "filesystems", "health", "volumes", "dav", "static", "s3"}
)

# S3's own default and maximum for a list page.
DEFAULT_MAX_KEYS = 1000
MAX_MAX_KEYS = 1000

# A single buffered body. The signature covers the payload hash, so the body
# has to be in hand before it can be authenticated -- which bounds how much a
# request may carry. 5 MiB is s3manager's default part size; the headroom
# above it accommodates a client configured with larger parts.
MAX_BODY_BYTES = 64 * 1024 * 1024

_STREAM_CHUNK = 1024 * 1024


class S3Error(Exception):
    """An S3 error code, rendered as S3 renders it."""

    def __init__(self, code: str, message: str, status: int, key: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.key = key


def _error_response(e: S3Error, resource: str = "") -> Response:
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Error><Code>%s</Code><Message>%s</Message><Resource>%s</Resource>"
        "<RequestId>%s</RequestId></Error>"
        % (
            xml_escape(e.code),
            xml_escape(e.message),
            xml_escape(resource),
            uuid.uuid4().hex,
        )
    )
    return Response(body.encode(), status=e.status, mimetype="application/xml")


def _translate(exc: BaseException) -> S3Error:
    """Engine and manager errors -> the S3 code that means the same thing."""
    from aloelite import errors as aerr

    if isinstance(exc, S3Error):
        return exc
    if isinstance(exc, aerr.NotFound):
        return S3Error("NoSuchKey", "the specified key does not exist", 404)
    if isinstance(exc, (aerr.NotAnEntry, aerr.NotAContainer)):
        # A key that is a prefix of other keys, or vice versa. S3 has a flat
        # keyspace and permits both; a tree cannot, so this is refused rather
        # than resolved arbitrarily.
        return S3Error(
            "InvalidRequest",
            "a key on this path already exists as the other kind of node; "
            "the aloelite backing store is a tree, so a key cannot be both an "
            "object and a prefix of other objects",
            409,
        )
    if isinstance(exc, aerr.LockHeld):
        return S3Error(
            "OperationAborted",
            "another write to this key is in progress",
            409,
        )
    if isinstance(exc, (merr.BadPin, merr.EncryptionMismatch)):
        return S3Error("AccessDenied", "the volume credentials were refused", 403)
    if isinstance(exc, merr.MountFailed):
        return S3Error("InternalError", "the volume could not be opened", 500)
    raise exc


def _norm_key(raw: str) -> str:
    """An S3 key -> an absolute aloelite path.

    S3 keys have no leading slash and use '/' purely as a naming convention;
    aloelite paths are absolute. Empty segments are dropped rather than
    preserved, since '//' in a key would otherwise mint an unnamed container.
    """
    parts = [p for p in raw.split("/") if p and p not in (".", "..")]
    return "/" + "/".join(parts)


def _key_of(path: str) -> str:
    return path.lstrip("/")


def _etag(info) -> str:
    """An opaque strong validator.

    NOT the MD5 of the object: aloelite's chunk addresses are taken over
    CIPHERTEXT (so the same bytes under two volume keys differ), and storing a
    plaintext digest beside the ciphertext would hand anyone with file access
    an offline confirmation oracle -- which is precisely what the volume's
    'random' enc_mode gives up dedup to avoid. content.version identifies the
    committed bytes exactly, which is all an ETag promises. Multipart part
    ETags are round-tripped verbatim by the client, so opacity costs nothing.
    """
    if getattr(info, "version", None) is None:
        return '"%s"' % info.id
    return '"%s-%s"' % (info.id, info.version)


def _iso8601(ms: int) -> str:
    import datetime as dt

    return (
        dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        )[:-3]
        + "Z"
    )


class _MultipartUploads:
    """In-memory staging for multipart uploads.

    Parts are held in memory until Complete concatenates them, which bounds a
    single upload by (part size x part count). That is the honest limit of
    this first implementation and it is enforced rather than discovered:
    Complete refuses an upload whose parts exceed the cap instead of trying
    to buffer it.

    Deliberately NOT persisted. An upload interrupted by a manager restart is
    abandoned, which is the same outcome the client already handles -- it
    retries the whole object -- and it keeps a crash from leaving staged parts
    referenced by nothing.
    """

    def __init__(self, max_bytes: int) -> None:
        self._lock = threading.Lock()
        self._uploads: dict[str, dict] = {}
        self._max_bytes = max_bytes

    def create(self, vid: str, key: str, metadata: dict[str, str]) -> str:
        upload_id = uuid.uuid4().hex
        with self._lock:
            self._uploads[upload_id] = {
                "vid": vid,
                "key": key,
                "metadata": metadata,
                "parts": {},
                "bytes": 0,
            }
        return upload_id

    def _get(self, upload_id: str, vid: str, key: str) -> dict:
        rec = self._uploads.get(upload_id)
        if rec is None or rec["vid"] != vid or rec["key"] != key:
            raise S3Error(
                "NoSuchUpload",
                "the specified upload does not exist, or does not belong to this key",
                404,
            )
        return rec

    def put_part(
        self, upload_id: str, vid: str, key: str, number: int, data: bytes
    ) -> str:
        with self._lock:
            rec = self._get(upload_id, vid, key)
            previous = rec["parts"].get(number)
            grown = rec["bytes"] - (len(previous) if previous else 0) + len(data)
            if grown > self._max_bytes:
                raise S3Error(
                    "EntityTooLarge",
                    "the multipart upload exceeds this endpoint's staging limit "
                    "of %d bytes" % self._max_bytes,
                    400,
                )
            rec["parts"][number] = data
            rec["bytes"] = grown
        # A part etag must be stable for the part's bytes, because the client
        # sends it back in CompleteMultipartUpload and we verify it there.
        return '"%s"' % hashlib.sha256(data).hexdigest()[:32]

    def complete(
        self, upload_id: str, vid: str, key: str, wanted: list[tuple[int, str]]
    ):
        with self._lock:
            rec = self._get(upload_id, vid, key)
            parts = rec["parts"]
            if not wanted:
                raise S3Error("InvalidRequest", "no parts were listed", 400)
            numbers = [n for n, _ in wanted]
            if numbers != sorted(numbers):
                raise S3Error(
                    "InvalidPartOrder", "parts must be listed in ascending order", 400
                )
            body = bytearray()
            for number, etag in wanted:
                data = parts.get(number)
                if data is None:
                    raise S3Error(
                        "InvalidPart", "part %d was never uploaded" % number, 400
                    )
                if etag and etag.strip('"') != hashlib.sha256(data).hexdigest()[:32]:
                    raise S3Error(
                        "InvalidPart",
                        "the etag for part %d does not match the staged bytes" % number,
                        400,
                    )
                body += data
            metadata = rec["metadata"]
            del self._uploads[upload_id]
        return bytes(body), metadata

    def abort(self, upload_id: str, vid: str, key: str) -> None:
        with self._lock:
            self._get(upload_id, vid, key)
            del self._uploads[upload_id]

    def forget_volume(self, vid: str) -> None:
        with self._lock:
            for uid in [u for u, r in self._uploads.items() if r["vid"] == vid]:
                del self._uploads[uid]


def create_s3_blueprint(
    store: VolumeStore,
    registry: DirectSessionRegistry,
    *,
    credentials: dict[str, Credentials],
    region: str = "us-east-1",
    virtual_host_suffix: str | None = None,
    max_body_bytes: int = MAX_BODY_BYTES,
) -> Blueprint:
    """A Flask blueprint serving volumes in `store` over an S3 subset.

    Registered by manager.api.create_app only when S3 is enabled, so an
    unconfigured manager has no S3 surface at all -- the same posture as DAV.

    `credentials` maps access key id -> Credentials(secret, buckets). The
    volume PIN is NOT the S3 secret: an S3 key is a separate credential with
    its own scope, because a backup job holding the PIN could also unlock the
    volume through every other frontend.
    """
    bp = Blueprint("s3", __name__)
    uploads = _MultipartUploads(max_body_bytes * 16)

    # -- auth ---------------------------------------------------------------
    def _authenticate(body: bytes | None) -> Credentials:
        try:
            _, creds = sigv4_verify(
                method=request.method,
                path=request.path,
                query_string=request.query_string.decode("latin-1"),
                headers=request.headers,
                body_hash=hashlib.sha256(body).hexdigest()
                if body is not None
                else None,
                lookup=credentials.get,
            )
            return creds
        except SigV4Error as e:
            raise S3Error(e.code, e.message, e.status) from e

    def _body() -> bytes:
        data = request.get_data(cache=False)
        if len(data) > max_body_bytes:
            raise S3Error(
                "EntityTooLarge",
                "request body exceeds this endpoint's limit of %d bytes"
                % max_body_bytes,
                400,
            )
        return data

    # -- volume resolution --------------------------------------------------
    def _resolve_bucket(bucket: str) -> VolumeRecord:
        if bucket in RESERVED_BUCKETS:
            raise S3Error(
                "InvalidBucketName",
                "%r is reserved by the manager's own API and cannot be served "
                "as a bucket" % bucket,
                400,
            )
        for rec in store.list():
            if rec.name == bucket:
                return rec
        raise S3Error("NoSuchBucket", "no volume is named %r" % bucket, 404)

    def _mark_mounted(rec: VolumeRecord) -> None:
        if not rec.mounted or rec.frontend != FRONTEND_DIRECT:
            rec.mounted = True
            rec.mountpoint = None
            rec.frontend = FRONTEND_DIRECT
            store.put(rec)

    def _session(rec: VolumeRecord, creds: Credentials):
        """Open the volume. An encrypted volume needs a PIN this frontend does
        not have, so it must already be unlocked by another path (the API, the
        web UI, or auto-mount). Refusing is the honest answer -- there is
        nowhere to prompt in an S3 request."""
        if not creds.may_access(rec.name):
            raise S3Error(
                "AccessDenied", "this access key may not address %r" % rec.name, 403
            )
        if not registry.is_unlocked(rec.id):
            if rec.encrypted:
                raise S3Error(
                    "AccessDenied",
                    "volume %r is encrypted and locked; unlock it through the "
                    "manager API before replicating to it" % rec.name,
                    403,
                )
            try:
                registry.unlock(rec, None, store.sqlite_path_of(rec))
            except merr.AlreadyMounted:
                pass
            _mark_mounted(rec)
        else:
            _mark_mounted(rec)
        # `token` is OMITTED deliberately. Passing it -- including passing
        # None -- asks for that client's own mount and raises NotAuthorized
        # when there isn't one; omitting it yields the shared primary mount,
        # which is what a credential-authenticated frontend wants.
        return registry.session(rec.id)

    # -- listing ------------------------------------------------------------
    def _walk(m, path: str, prefix_path: str, out: list) -> None:
        """Every entry at or under `path`, as (key, info). Containers are not
        objects in S3, so only entries are emitted."""
        try:
            entries = m.list(path)
        except Exception:
            return
        for e in entries:
            child = (path.rstrip("/") + "/" + e.name) if path != "/" else "/" + e.name
            if e.type.value == "container":
                _walk(m, child, prefix_path, out)
            else:
                out.append((_key_of(child), child))

    def _do_list(rec, m) -> Response:
        args = request.args
        prefix = args.get("prefix", "")
        delimiter = args.get("delimiter", "")
        marker = args.get("marker", "")
        try:
            max_keys = min(int(args.get("max-keys", DEFAULT_MAX_KEYS)), MAX_MAX_KEYS)
        except ValueError:
            raise S3Error(
                "InvalidArgument", "max-keys must be an integer", 400
            ) from None
        if max_keys < 0:
            raise S3Error("InvalidArgument", "max-keys must not be negative", 400)

        # Walk from the deepest container the prefix names, so a prefix deep
        # in the tree does not enumerate the whole volume.
        root = _norm_key(prefix.rsplit("/", 1)[0]) if "/" in prefix else "/"
        found: list = []
        _walk(m, root, prefix, found)

        keys = sorted(k for k, _ in found if k.startswith(prefix))

        # S3 orders lexicographically; aloelite's listing order is canonical
        # (edge_id, node_id), so the sort above is load-bearing, not cosmetic.
        contents: list[str] = []
        common: list[str] = []
        seen_prefixes: set[str] = set()
        truncated = False
        next_marker = ""
        emitted = 0

        for key in keys:
            if marker and key <= marker:
                continue
            if delimiter:
                rest = key[len(prefix) :]
                idx = rest.find(delimiter)
                if idx >= 0:
                    group = prefix + rest[: idx + len(delimiter)]
                    if group in seen_prefixes:
                        continue
                    if emitted >= max_keys:
                        truncated = True
                        next_marker = key
                        break
                    seen_prefixes.add(group)
                    common.append(
                        "<CommonPrefixes><Prefix>%s</Prefix></CommonPrefixes>"
                        % xml_escape(group)
                    )
                    emitted += 1
                    next_marker = key
                    continue
            if emitted >= max_keys:
                truncated = True
                break
            info = m.stat(_norm_key(key))
            contents.append(
                "<Contents><Key>%s</Key><LastModified>%s</LastModified>"
                "<ETag>%s</ETag><Size>%d</Size>"
                "<StorageClass>STANDARD</StorageClass></Contents>"
                % (
                    xml_escape(key),
                    _iso8601(info.modified_at or info.created_at),
                    xml_escape(_etag(info)),
                    info.size or 0,
                )
            )
            emitted += 1
            next_marker = key

        body = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            "<Name>%s</Name><Prefix>%s</Prefix><Marker>%s</Marker>"
            "<MaxKeys>%d</MaxKeys><Delimiter>%s</Delimiter>"
            "<IsTruncated>%s</IsTruncated>%s%s%s</ListBucketResult>"
            % (
                xml_escape(rec.name),
                xml_escape(prefix),
                xml_escape(marker),
                max_keys,
                xml_escape(delimiter),
                "true" if truncated else "false",
                (
                    "<NextMarker>%s</NextMarker>" % xml_escape(next_marker)
                    if truncated and delimiter
                    else ""
                ),
                "".join(contents),
                "".join(common),
            )
        )
        return Response(body.encode(), status=200, mimetype="application/xml")

    # -- objects ------------------------------------------------------------
    def _do_get(rec, m, key: str, *, body: bool) -> Response:
        path = _norm_key(key)
        info = m.stat(path)
        if info.type.value == "container":
            raise S3Error("NoSuchKey", "the specified key does not exist", 404)
        headers = {
            "ETag": _etag(info),
            "Last-Modified": _iso8601(info.modified_at or info.created_at),
            "Content-Length": str(info.size),
            "Accept-Ranges": "bytes",
            "x-amz-request-id": uuid.uuid4().hex,
        }
        ctype = info.metadata.get("s3:content-type", "application/octet-stream")
        for k, v in info.metadata.items():
            if k.startswith("s3:meta:"):
                headers["x-amz-meta-" + k[len("s3:meta:") :]] = v
        if not body:
            return Response(b"", status=200, headers=headers, mimetype=ctype)
        data = m.read_all(path)
        return Response(data, status=200, headers=headers, mimetype=ctype)

    def _do_put(rec, m, key: str, data: bytes) -> Response:
        path = _norm_key(key)
        parent = path.rsplit("/", 1)[0] or "/"
        if parent != "/":
            # S3 has no directories; a key's parents are implied and must be
            # created on demand.
            m.mkdir(parent, parents=True, exist_ok=True)
        _write(m, path, data, _request_metadata())
        return Response(b"", status=200, headers={"ETag": _etag(m.stat(path))})

    def _request_metadata() -> dict[str, str]:
        meta = {}
        ctype = request.headers.get("Content-Type")
        if ctype:
            meta["s3:content-type"] = ctype
        for name, value in request.headers.items():
            low = name.lower()
            if low.startswith("x-amz-meta-"):
                meta["s3:meta:" + low[len("x-amz-meta-") :]] = value
        return meta

    def _write(m, path: str, data: bytes, metadata: dict[str, str]) -> None:
        from aloelite.types import WriteMode

        # TRUNCATE creates the entry and clears it, so a zero-length object is
        # the loop simply not running -- no empty write needed, and none of
        # the special-casing that invites an off-by-one.
        with m.open_write(path, WriteMode.TRUNCATE) as writer:
            for i in range(0, len(data), _STREAM_CHUNK):
                writer.write(data[i : i + _STREAM_CHUNK])
        if metadata:
            m.set_metadata(path, metadata)

    def _do_delete_batch(rec, m, data: bytes) -> Response:
        import xml.etree.ElementTree as ET

        try:
            root = ET.fromstring(data)
        except ET.ParseError as e:
            raise S3Error(
                "MalformedXML", "the delete request is not valid XML", 400
            ) from e
        quiet = (
            root.findtext("{*}Quiet") or root.findtext("Quiet") or ""
        ).lower() == "true"
        deleted, errors = [], []
        for obj in root.iter():
            if not obj.tag.endswith("Object"):
                continue
            key = obj.findtext("{*}Key") or obj.findtext("Key")
            if not key:
                continue
            try:
                m.remove(_norm_key(key))
                deleted.append(key)
            except Exception as exc:
                try:
                    err = _translate(exc)
                except Exception:
                    raise
                if err.code == "NoSuchKey":
                    # S3 reports deleting a missing key as SUCCESS. Litestream
                    # deletes generations it may have partially written, so a
                    # 404 here would fail a retry that should be a no-op.
                    deleted.append(key)
                else:
                    errors.append((key, err.code, err.message))
        parts = []
        if not quiet:
            parts += [
                "<Deleted><Key>%s</Key></Deleted>" % xml_escape(k) for k in deleted
            ]
        parts += [
            "<Error><Key>%s</Key><Code>%s</Code><Message>%s</Message></Error>"
            % (xml_escape(k), xml_escape(c), xml_escape(msg))
            for k, c, msg in errors
        ]
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<DeleteResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">%s</DeleteResult>'
            % "".join(parts)
        )
        return Response(body.encode(), status=200, mimetype="application/xml")

    # -- multipart ----------------------------------------------------------
    def _do_create_multipart(rec, m, key: str) -> Response:
        upload_id = uploads.create(rec.id, key, _request_metadata())
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<InitiateMultipartUploadResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            "<Bucket>%s</Bucket><Key>%s</Key><UploadId>%s</UploadId>"
            "</InitiateMultipartUploadResult>"
            % (xml_escape(rec.name), xml_escape(key), xml_escape(upload_id))
        )
        return Response(body.encode(), status=200, mimetype="application/xml")

    def _do_upload_part(rec, key: str, data: bytes) -> Response:
        try:
            number = int(request.args["partNumber"])
        except (KeyError, ValueError):
            raise S3Error(
                "InvalidArgument", "partNumber must be an integer", 400
            ) from None
        if not 1 <= number <= 10000:
            raise S3Error("InvalidArgument", "partNumber must be 1..10000", 400)
        etag = uploads.put_part(request.args["uploadId"], rec.id, key, number, data)
        return Response(b"", status=200, headers={"ETag": etag})

    def _do_complete_multipart(rec, m, key: str, data: bytes) -> Response:
        import xml.etree.ElementTree as ET

        try:
            root = ET.fromstring(data) if data else None
        except ET.ParseError as e:
            raise S3Error(
                "MalformedXML", "the complete request is not valid XML", 400
            ) from e
        wanted: list[tuple[int, str]] = []
        if root is not None:
            for part in root.iter():
                if not part.tag.endswith("Part"):
                    continue
                num = part.findtext("{*}PartNumber") or part.findtext("PartNumber")
                tag = part.findtext("{*}ETag") or part.findtext("ETag") or ""
                if num:
                    wanted.append((int(num), tag))
        body, metadata = uploads.complete(request.args["uploadId"], rec.id, key, wanted)

        path = _norm_key(key)
        parent = path.rsplit("/", 1)[0] or "/"
        if parent != "/":
            m.mkdir(parent, parents=True, exist_ok=True)
        _write(m, path, body, metadata)
        info = m.stat(path)
        out = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<CompleteMultipartUploadResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            "<Location>%s</Location><Bucket>%s</Bucket><Key>%s</Key><ETag>%s</ETag>"
            "</CompleteMultipartUploadResult>"
            % (
                xml_escape(quote("/%s/%s" % (rec.name, key))),
                xml_escape(rec.name),
                xml_escape(key),
                xml_escape(_etag(info)),
            )
        )
        return Response(out.encode(), status=200, mimetype="application/xml")

    # -- dispatch -----------------------------------------------------------
    def _split_target(bucket_in_path: str | None, rest: str) -> tuple[str, str]:
        """-> (bucket, key), honouring virtual-host addressing when enabled."""
        host = (request.host or "").split(":")[0]
        if virtual_host_suffix and host.endswith("." + virtual_host_suffix.lstrip(".")):
            bucket = host[: -(len(virtual_host_suffix.lstrip(".")) + 1)]
            key = ((bucket_in_path + "/") if bucket_in_path else "") + rest
            return bucket, unquote(key).strip("/")
        if not bucket_in_path:
            raise S3Error("InvalidBucketName", "no bucket in the request", 400)
        return bucket_in_path, unquote(rest).strip("/")

    def _dispatch(bucket_in_path: str | None = None, rest: str = "") -> Response:
        stack = ExitStack()
        try:
            needs_body = request.method in ("PUT", "POST")
            data = _body() if needs_body else b""
            creds = _authenticate(data if needs_body else b"")
            bucket, key = _split_target(bucket_in_path, rest)
            rec = _resolve_bucket(bucket)
            m = stack.enter_context(_session(rec, creds))

            args = request.args
            if request.method == "GET":
                if not key:
                    return _do_list(rec, m)
                return _do_get(rec, m, key, body=True)
            if request.method == "HEAD":
                if not key:
                    return Response(b"", status=200)
                return _do_get(rec, m, key, body=False)
            if request.method == "PUT":
                if "uploadId" in args and "partNumber" in args:
                    return _do_upload_part(rec, key, data)
                return _do_put(rec, m, key, data)
            if request.method == "POST":
                if "delete" in args and not key:
                    return _do_delete_batch(rec, m, data)
                if "uploads" in args:
                    return _do_create_multipart(rec, m, key)
                if "uploadId" in args:
                    return _do_complete_multipart(rec, m, key, data)
                raise S3Error("InvalidRequest", "unsupported POST", 400)
            if request.method == "DELETE":
                if "uploadId" in args:
                    uploads.abort(request.args["uploadId"], rec.id, key)
                    return Response(b"", status=204)
                try:
                    m.remove(_norm_key(key))
                except Exception as exc:
                    err = _translate(exc)
                    if err.code != "NoSuchKey":
                        raise
                return Response(b"", status=204)
            raise S3Error("MethodNotAllowed", "unsupported method", 405)
        except S3Error as e:
            return _error_response(e, request.path)
        except BaseException as exc:
            return _error_response(_translate(exc), request.path)
        finally:
            stack.close()

    _METHODS = ["GET", "HEAD", "PUT", "POST", "DELETE"]
    bp.add_url_rule(
        "/<bucket_in_path>",
        endpoint="s3_bucket",
        view_func=_dispatch,
        methods=_METHODS,
        strict_slashes=False,
    )
    bp.add_url_rule(
        "/<bucket_in_path>/<path:rest>",
        endpoint="s3_object",
        view_func=_dispatch,
        methods=_METHODS,
        strict_slashes=False,
    )
    return bp


__all__ = ["create_s3_blueprint", "S3Error", "RESERVED_BUCKETS"]
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
