# ./manager/sigv4.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
manager.sigv4 — AWS Signature Version 4, verification side.

The S3 frontend authenticates the way S3 does, because the clients we care
about will not do anything else. This module is the whole of that: parse the
Authorization header, rebuild the canonical request from what actually
arrived, re-derive the signature under the secret we hold for that access
key, and compare in constant time.

WHAT THIS DELIBERATELY DOES NOT IMPLEMENT
-----------------------------------------
`STREAMING-AWS4-HMAC-SHA256-PAYLOAD` — SigV4 chunked upload signing, where the
body arrives as length-prefixed chunks each carrying its own signature. It is
a large amount of machinery and the client this frontend was built for never
sends it: aws-sdk-go's s3manager buffers each part into a seekable byte slice
before signing, so every request carries a real payload hash. A request that
asks for it is REFUSED by name rather than mis-parsed as a body, because
silently treating the chunk framing as content would corrupt the object.

`UNSIGNED-PAYLOAD` is accepted: it is what a signer sends when it will not or
cannot hash the body, and the signature still covers every header, the method
and the URI. Over TLS that is the same protection the rest of the request
gets. Presigned URLs (`X-Amz-Signature` in the query string) are not
implemented; nothing in the target client mints them.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import re
from urllib.parse import quote, unquote

ALGORITHM = "AWS4-HMAC-SHA256"
STREAMING_PAYLOAD = "STREAMING-AWS4-HMAC-SHA256-PAYLOAD"
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"

# Default skew window. AWS uses 15 minutes; matching it means a client whose
# clock is tolerable to real S3 is tolerable here, which is the only bar that
# matters for a drop-in endpoint.
MAX_SKEW_SECONDS = 15 * 60

_AUTH_RE = re.compile(
    r"^AWS4-HMAC-SHA256\s+"
    r"Credential=(?P<cred>[^,]+),\s*"
    r"SignedHeaders=(?P<signed>[^,]+),\s*"
    r"Signature=(?P<sig>[0-9a-fA-F]+)\s*$"
)


class SigV4Error(Exception):
    """Rejected request. `code` is the S3 error code to return verbatim."""

    def __init__(self, code: str, message: str, status: int = 403) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class Credentials:
    """access key -> (secret, the volume ids this key may address).

    `buckets` of None means every volume the store holds. A key scoped to a
    set of bucket names can address only those, which is what lets one
    endpoint serve several jobs without each holding the others' data.
    """

    def __init__(self, secret: str, buckets: set[str] | None = None) -> None:
        self.secret = secret
        self.buckets = buckets

    def may_access(self, bucket: str) -> bool:
        return self.buckets is None or bucket in self.buckets


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _uri_encode(value: str, *, encode_slash: bool) -> str:
    """RFC 3986 unreserved-only encoding, which is what SigV4 specifies.

    `quote`'s default safe set is '/', and its default unreserved set already
    matches SigV4's (A-Z a-z 0-9 - _ . ~), so the only choice left is whether
    a slash survives. It must in a path and must not in a query value.
    """
    return quote(value, safe="/" if encode_slash else "")


def canonical_uri(path: str) -> str:
    """The path, normalised then re-encoded exactly once.

    Werkzeug hands us a DECODED path, so a key containing a literal '%2F' or a
    space has already been turned back into '/' or ' '. Re-encoding is
    therefore the correct inverse -- but only for a path that was encoded
    once, which is the only kind a signing client sends.
    """
    if not path:
        return "/"
    return _uri_encode(path, encode_slash=True)


def canonical_query(query_string: str) -> str:
    """Query parameters sorted by name then value, each re-encoded.

    Built from the RAW query string rather than a parsed mapping: S3 signs
    valueless flags such as `?delete` and `?uploads` as `delete=` and
    `uploads=`, and most parsers either drop them or fold them into a list,
    both of which change the signature.
    """
    if not query_string:
        return ""
    pairs: list[tuple[str, str]] = []
    for part in query_string.split("&"):
        if not part:
            continue
        name, sep, value = part.partition("=")
        pairs.append(
            (
                _uri_encode(unquote(name), encode_slash=False),
                _uri_encode(unquote(value), encode_slash=False) if sep else "",
            )
        )
    pairs.sort()
    return "&".join(f"{n}={v}" for n, v in pairs)


def canonical_headers(headers, signed: list[str]) -> str:
    """The signed headers, lowercased, values trimmed, one per line.

    Only the names the client listed in SignedHeaders take part; everything
    else a proxy may have added is ignored, which is what lets the signature
    survive an intermediary.
    """
    out = []
    for name in signed:
        value = headers.get(name)
        if value is None:
            raise SigV4Error(
                "SignatureDoesNotMatch",
                f"signed header {name!r} is not present on the request",
            )
        # Sequential whitespace collapses per the spec; a trim alone is not
        # enough for a value that carries an internal run of spaces.
        out.append(f"{name}:{' '.join(str(value).split())}")
    return "\n".join(out) + "\n"


def build_canonical_request(
    method: str,
    path: str,
    query_string: str,
    headers,
    signed: list[str],
    payload_hash: str,
) -> str:
    return "\n".join(
        [
            method,
            canonical_uri(path),
            canonical_query(query_string),
            canonical_headers(headers, signed),
            ";".join(signed),
            payload_hash,
        ]
    )


def signing_key(secret: str, date: str, region: str, service: str) -> bytes:
    k = _hmac(f"AWS4{secret}".encode(), date)
    k = _hmac(k, region)
    k = _hmac(k, service)
    return _hmac(k, "aws4_request")


def string_to_sign(amz_date: str, scope: str, canonical_request: str) -> str:
    return "\n".join(
        [ALGORITHM, amz_date, scope, _sha256_hex(canonical_request.encode())]
    )


def _parse_authorization(header: str) -> tuple[str, str, str, str, list[str], str]:
    m = _AUTH_RE.match(header.strip())
    if not m:
        raise SigV4Error(
            "AuthorizationHeaderMalformed", "could not parse the Authorization header"
        )
    cred = m.group("cred").split("/")
    if len(cred) != 5 or cred[4] != "aws4_request":
        raise SigV4Error(
            "AuthorizationHeaderMalformed",
            "Credential must be <key>/<date>/<region>/<service>/aws4_request",
        )
    access_key, date, region, service, _ = cred
    signed = [h for h in m.group("signed").split(";") if h]
    if not signed:
        raise SigV4Error("AuthorizationHeaderMalformed", "SignedHeaders is empty")
    return access_key, date, region, service, signed, m.group("sig").lower()


def _check_skew(amz_date: str, scope_date: str, now: _dt.datetime | None) -> None:
    try:
        stamp = _dt.datetime.strptime(amz_date, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=_dt.timezone.utc
        )
    except ValueError as e:
        raise SigV4Error(
            "AuthorizationHeaderMalformed",
            f"x-amz-date {amz_date!r} is not ISO8601 basic",
        ) from e
    if stamp.strftime("%Y%m%d") != scope_date:
        raise SigV4Error(
            "AuthorizationHeaderMalformed",
            "the credential scope date disagrees with x-amz-date",
        )
    now = now or _dt.datetime.now(_dt.timezone.utc)
    if abs((now - stamp).total_seconds()) > MAX_SKEW_SECONDS:
        raise SigV4Error(
            "RequestTimeTooSkewed",
            "the request time is too far from the server's clock",
        )


def verify(
    *,
    method: str,
    path: str,
    query_string: str,
    headers,
    body_hash: str | None,
    lookup,
    now: _dt.datetime | None = None,
) -> tuple[str, Credentials]:
    """Authenticate one request. -> (access_key, Credentials), or raise.

    `lookup(access_key)` returns Credentials or None. `body_hash` is the hex
    SHA-256 the caller computed over the body it actually read; it is compared
    against the client's claim so a signature can never certify bytes the
    server did not receive. Pass None to skip that comparison (a streaming
    read that has not finished), in which case the client's claimed hash is
    what gets signed and the body is trusted on TLS alone.
    """
    auth = headers.get("Authorization")
    if not auth:
        raise SigV4Error("AccessDenied", "missing Authorization header", status=401)
    access_key, date, region, service, signed, claimed = _parse_authorization(auth)

    amz_date = headers.get("X-Amz-Date")
    if not amz_date:
        raise SigV4Error("AccessDenied", "missing X-Amz-Date header")
    _check_skew(amz_date, date, now)

    declared = headers.get("X-Amz-Content-Sha256") or UNSIGNED_PAYLOAD
    if declared == STREAMING_PAYLOAD:
        # Refused by name rather than mis-read; see the module docstring.
        raise SigV4Error(
            "NotImplemented",
            "chunked upload signing (STREAMING-AWS4-HMAC-SHA256-PAYLOAD) is not "
            "supported by this endpoint; the body must carry a payload hash or "
            "UNSIGNED-PAYLOAD",
            status=501,
        )
    if declared != UNSIGNED_PAYLOAD and body_hash is not None and declared != body_hash:
        # The signature would verify against the CLAIM while the stored bytes
        # are something else, so this must be caught before the compare.
        raise SigV4Error(
            "XAmzContentSHA256Mismatch",
            "the body does not hash to the value in x-amz-content-sha256",
            status=400,
        )

    creds = lookup(access_key)
    if creds is None:
        raise SigV4Error("InvalidAccessKeyId", "unknown access key id")

    canonical = build_canonical_request(
        method, path, query_string, headers, signed, declared
    )
    scope = f"{date}/{region}/{service}/aws4_request"
    expected = hmac.new(
        signing_key(creds.secret, date, region, service),
        string_to_sign(amz_date, scope, canonical).encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, claimed):
        raise SigV4Error("SignatureDoesNotMatch", "computed signature does not match")
    return access_key, creds


__all__ = [
    "ALGORITHM",
    "Credentials",
    "MAX_SKEW_SECONDS",
    "STREAMING_PAYLOAD",
    "UNSIGNED_PAYLOAD",
    "SigV4Error",
    "build_canonical_request",
    "canonical_headers",
    "canonical_query",
    "canonical_uri",
    "signing_key",
    "string_to_sign",
    "verify",
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
