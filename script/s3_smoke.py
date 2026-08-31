#!/usr/bin/env python3
"""Smoke-test a live aloelite S3 endpoint with a real SDK client.

    python script/s3_smoke.py --endpoint http://127.0.0.1:7081 \
        --bucket backups --access-key AKIALOCALBACKUP --secret-key ...

Exercises exactly the calls litestream 0.3.13 makes, in the order it makes
them, so a pass here means the endpoint is usable as a replication target:
put, get, list flat, list with prefix+delimiter (CommonPrefixes), a multipart
upload past the 5 MiB part size, and a batch delete.

Requires botocore (pip install botocore). It is used deliberately rather than
hand-rolled requests: the point of the frontend is that an SDK talks to it,
and a request signed by aloelite's own code would only prove the verifier
agrees with itself.
"""

import argparse
import hashlib
import sys

try:
    from botocore.config import Config
    from botocore.exceptions import ClientError, EndpointConnectionError
    from botocore.session import get_session
except ImportError:
    sys.exit("botocore is required: pip install botocore")

PREFIX = "_aloelite_smoke/"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--endpoint",
        required=True,
        help="e.g. http://127.0.0.1:7081. Prefer an IP over a hostname when "
        "something looks like a hang: a name that resolves to an address "
        "nothing answers on is the likeliest cause, not the server.",
    )
    ap.add_argument("--bucket", required=True, help="the volume's NAME")
    ap.add_argument("--access-key", required=True)
    ap.add_argument("--secret-key", required=True)
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument(
        "--keep", action="store_true", help="leave the test objects behind"
    )
    a = ap.parse_args()

    s3 = get_session().create_client(
        "s3",
        endpoint_url=a.endpoint,
        aws_access_key_id=a.access_key,
        aws_secret_access_key=a.secret_key,
        region_name=a.region,
        # Path style: what a deployment must configure litestream for anyway.
        # A short connect timeout on purpose. botocore's default is 60s, and
        # a hostname that resolves to an address nothing answers on (a stale
        # DNS entry, an IPv6 record with no listener) then looks exactly like
        # a hung server rather than the name-resolution problem it is. Ten
        # seconds is generous for anything on a LAN or VPN, and failing with
        # "could not connect" beats sitting silent.
        config=Config(
            s3={"addressing_style": "path"},
            retries={"max_attempts": 1},
            connect_timeout=10,
            read_timeout=120,
        ),
    )

    failures = []

    def check(name, fn):
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as e:  # noqa: BLE001 - a smoke test reports, never raises
            failures.append(name)
            detail = e
            if isinstance(e, ClientError):
                detail = e.response.get("Error", {})
            print(f"  FAIL  {name}: {detail}")

    print(f"endpoint {a.endpoint}  bucket {a.bucket}\n")

    body = b"aloelite s3 smoke " * 64

    def put_get():
        s3.put_object(Bucket=a.bucket, Key=PREFIX + "hello.txt", Body=body)
        got = s3.get_object(Bucket=a.bucket, Key=PREFIX + "hello.txt")["Body"].read()
        assert got == body, f"round trip differs ({len(got)} vs {len(body)} bytes)"

    def deep_key():
        # Litestream's layout is deep; S3 has no directories, so the prefixes
        # must be created implicitly.
        k = PREFIX + "generations/abc/wal/0000/0001.wal.lz4"
        s3.put_object(Bucket=a.bucket, Key=k, Body=b"x")
        assert s3.get_object(Bucket=a.bucket, Key=k)["Body"].read() == b"x"

    def list_flat():
        out = s3.list_objects(Bucket=a.bucket, Prefix=PREFIX)
        keys = [c["Key"] for c in out.get("Contents", [])]
        assert keys == sorted(keys), "S3 promises lexicographic order"
        assert any(k.endswith("hello.txt") for k in keys), keys

    def list_delimited():
        # How litestream enumerates generations: it reads CommonPrefixes, not
        # keys. A server that omits them cannot be enumerated at all.
        out = s3.list_objects(
            Bucket=a.bucket, Prefix=PREFIX + "generations/", Delimiter="/"
        )
        got = [p["Prefix"] for p in out.get("CommonPrefixes", [])]
        assert got == [PREFIX + "generations/abc/"], got

    def multipart():
        # s3manager switches to multipart past 5 MiB, so any snapshot larger
        # than that arrives this way. Two parts, the first over the minimum.
        key = PREFIX + "snapshot.bin"
        up = s3.create_multipart_upload(Bucket=a.bucket, Key=key)["UploadId"]
        parts, blob = [], b""
        for n, chunk in ((1, b"A" * (5 * 1024 * 1024 + 1)), (2, b"B" * 1024)):
            r = s3.upload_part(
                Bucket=a.bucket, Key=key, PartNumber=n, UploadId=up, Body=chunk
            )
            parts.append({"ETag": r["ETag"], "PartNumber": n})
            blob += chunk
        s3.complete_multipart_upload(
            Bucket=a.bucket, Key=key, UploadId=up, MultipartUpload={"Parts": parts}
        )
        got = s3.get_object(Bucket=a.bucket, Key=key)["Body"].read()
        assert hashlib.sha256(got).digest() == hashlib.sha256(blob).digest(), (
            f"multipart reassembly differs ({len(got)} vs {len(blob)} bytes)"
        )

    def batch_delete():
        # Litestream's ONLY delete path, used by the retention sweep.
        keys = [
            c["Key"]
            for c in s3.list_objects(Bucket=a.bucket, Prefix=PREFIX).get("Contents", [])
        ]
        if not keys:
            return
        out = s3.delete_objects(
            Bucket=a.bucket, Delete={"Objects": [{"Key": k} for k in keys]}
        )
        assert not out.get("Errors"), out["Errors"]
        left = s3.list_objects(Bucket=a.bucket, Prefix=PREFIX).get("Contents", [])
        assert not left, f"{len(left)} objects survived the delete"

    try:
        check("put + get round trip", put_get)
        check("deep key creates implied prefixes", deep_key)
        check("list (flat, sorted)", list_flat)
        check("list with delimiter -> CommonPrefixes", list_delimited)
        check("multipart upload past 5 MiB", multipart)
        if not a.keep:
            check("batch delete", batch_delete)
    except EndpointConnectionError as e:
        return _fail(f"cannot reach {a.endpoint}: {e}")

    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        return 1
    print("all good -- this endpoint can serve as a litestream replica target")
    if a.keep:
        print(f"(left the {PREFIX} objects behind; --keep was set)")
    return 0


def _fail(msg: str) -> int:
    print(f"\n{msg}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
