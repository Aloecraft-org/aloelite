# ./manager/test_s3.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
S3 frontend tests, driven by a REAL SDK client over a REAL socket.

The point of this frontend is that an SDK talks to it, so the tests use
botocore rather than hand-built requests: botocore signs, chunks, retries and
parses exactly as it does against AWS, which is the only thing that proves
compatibility. A hand-rolled request signed by our own sigv4 module would be
testing the verifier against itself.

A live server on an ephemeral port rather than werkzeug's test client, because
the test client bypasses the WSGI server and botocore has no way to speak to
it. The engine, the store and the registry are all real; only the supervisor
is absent, as in test_dav.py.
"""

from __future__ import annotations

import hashlib
import threading

import pytest

from manager.api import create_app
from manager.engine.direct import FRONTEND_DIRECT, DirectSessionRegistry
from manager.engine.store import JsonVolumeStore, VolumeRecord
from manager.sigv4 import Credentials

botocore = pytest.importorskip("botocore", reason="botocore drives the S3 tests")

from botocore.config import Config  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from botocore.session import get_session  # noqa: E402

KEY = "AKIAALOELITETEST"
SECRET = "s3cr3t-not-the-volume-pin"
BUCKET = "backups"


def _record(vid="v1", name=BUCKET, encrypted=False) -> VolumeRecord:
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
def s3(tmp_path):
    """A live manager with S3 enabled, plus a botocore client pointed at it."""
    from werkzeug.serving import make_server

    store = JsonVolumeStore(str(tmp_path / "store.json"))
    registry = DirectSessionRegistry()
    rec = _record()
    registry.unlock(rec, None, str(tmp_path / "vol.sqlite"))  # creates the volume
    store.put(rec)

    app = create_app(
        store,
        supervisor=None,
        registry=registry,
        auth_mode="off",
        s3=True,
        s3_credentials={KEY: Credentials(SECRET)},
    )
    server = make_server("127.0.0.1", 0, app, threaded=True)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    client = get_session().create_client(
        "s3",
        endpoint_url=f"http://127.0.0.1:{port}",
        aws_access_key_id=KEY,
        aws_secret_access_key=SECRET,
        region_name="us-east-1",
        # Path style, because that is what a deployment must configure
        # litestream for; see the addressing note in manager/s3.py.
        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 1}),
    )
    try:
        yield client
    finally:
        server.shutdown()
        thread.join(timeout=5)
        registry.lock(rec)
        store.close()


# -- objects ---------------------------------------------------------------


def test_put_then_get_roundtrips(s3):
    body = b"the quick brown fox" * 100
    s3.put_object(Bucket=BUCKET, Key="generations/abc/snapshot", Body=body)
    got = s3.get_object(Bucket=BUCKET, Key="generations/abc/snapshot")
    assert got["Body"].read() == body


def test_last_modified_is_plausible_not_merely_well_formed(s3):
    """The third outing for one bug, so it gets the guard the others have.

    Era 2 moved timestamps from milliseconds to nanoseconds. Every frontend
    written before that change renders Last-Modified by dividing to seconds,
    and every one of them has divided by the wrong constant: operations.py
    (_now_ms), dav.py (_rfc1123/_iso8601), and now s3.py. The failure is loud
    here only because datetime overflows at year 56 million — divide too hard
    instead of too little and the answer is 1970-01-01, which is well-formed,
    parses cleanly, and passes every assertion that checks shape.

    So assert the VALUE. A unit error is never subtle in magnitude.
    """
    from datetime import datetime, timedelta, timezone

    floor = datetime.now(tz=timezone.utc) - timedelta(minutes=5)
    s3.put_object(Bucket=BUCKET, Key="fresh", Body=b"now")
    got = s3.get_object(Bucket=BUCKET, Key="fresh")

    stamp = got["ResponseMetadata"]["HTTPHeaders"]["last-modified"]
    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    ceiling = datetime.now(tz=timezone.utc) + timedelta(minutes=5)
    assert floor <= parsed <= ceiling, (
        f"Last-Modified is {parsed.isoformat()}, which is not within minutes "
        "of now — the engine timestamp was scaled by the wrong unit"
    )


def test_put_creates_implied_parents(s3):
    """S3 has no directories; a deep key must not need its prefixes created."""
    s3.put_object(Bucket=BUCKET, Key="a/b/c/d/e.wal.lz4", Body=b"x")
    assert s3.get_object(Bucket=BUCKET, Key="a/b/c/d/e.wal.lz4")["Body"].read() == b"x"


def test_zero_length_object(s3):
    s3.put_object(Bucket=BUCKET, Key="empty", Body=b"")
    got = s3.get_object(Bucket=BUCKET, Key="empty")
    assert got["Body"].read() == b""
    assert got["ContentLength"] == 0


def test_get_missing_key_is_nosuchkey(s3):
    with pytest.raises(ClientError) as e:
        s3.get_object(Bucket=BUCKET, Key="nope")
    assert e.value.response["Error"]["Code"] == "NoSuchKey"


def test_unknown_bucket_is_nosuchbucket(s3):
    with pytest.raises(ClientError) as e:
        s3.get_object(Bucket="not-a-volume", Key="k")
    assert e.value.response["Error"]["Code"] == "NoSuchBucket"


def test_overwrite_replaces_and_moves_the_etag(s3):
    first = s3.put_object(Bucket=BUCKET, Key="k", Body=b"one")["ETag"]
    second = s3.put_object(Bucket=BUCKET, Key="k", Body=b"two")["ETag"]
    assert first != second, "the etag must track the committed version"
    assert s3.get_object(Bucket=BUCKET, Key="k")["Body"].read() == b"two"


# -- listing (V1, which is what litestream calls) ---------------------------


def test_list_objects_v1_returns_keys_sorted(s3):
    for k in ("b.txt", "a.txt", "c.txt"):
        s3.put_object(Bucket=BUCKET, Key=k, Body=b"x")
    out = s3.list_objects(Bucket=BUCKET)
    assert [c["Key"] for c in out["Contents"]] == ["a.txt", "b.txt", "c.txt"]


def test_list_with_prefix_and_delimiter_gives_common_prefixes(s3):
    """How litestream enumerates generations: prefix + delimiter, reading
    CommonPrefixes rather than keys."""
    for g in ("g1", "g2"):
        s3.put_object(Bucket=BUCKET, Key=f"generations/{g}/snapshot", Body=b"x")
        s3.put_object(Bucket=BUCKET, Key=f"generations/{g}/wal/0.wal", Body=b"y")
    out = s3.list_objects(Bucket=BUCKET, Prefix="generations/", Delimiter="/")
    assert sorted(p["Prefix"] for p in out["CommonPrefixes"]) == [
        "generations/g1/",
        "generations/g2/",
    ]


def test_list_without_delimiter_is_flat_and_recursive(s3):
    for g in ("g1", "g2"):
        s3.put_object(Bucket=BUCKET, Key=f"generations/{g}/wal/0.wal", Body=b"y")
    out = s3.list_objects(Bucket=BUCKET, Prefix="generations/")
    assert sorted(c["Key"] for c in out["Contents"]) == [
        "generations/g1/wal/0.wal",
        "generations/g2/wal/0.wal",
    ]


def test_list_prefix_excludes_siblings(s3):
    s3.put_object(Bucket=BUCKET, Key="keep/a", Body=b"x")
    s3.put_object(Bucket=BUCKET, Key="other/b", Body=b"x")
    out = s3.list_objects(Bucket=BUCKET, Prefix="keep/")
    assert [c["Key"] for c in out["Contents"]] == ["keep/a"]


def test_list_paginates_with_marker(s3):
    for i in range(5):
        s3.put_object(Bucket=BUCKET, Key=f"k{i}", Body=b"x")
    first = s3.list_objects(Bucket=BUCKET, MaxKeys=2)
    assert first["IsTruncated"] is True
    assert [c["Key"] for c in first["Contents"]] == ["k0", "k1"]
    second = s3.list_objects(Bucket=BUCKET, MaxKeys=2, Marker="k1")
    assert [c["Key"] for c in second["Contents"]] == ["k2", "k3"]


def test_list_of_empty_bucket_has_no_contents(s3):
    out = s3.list_objects(Bucket=BUCKET)
    assert out.get("Contents", []) == []
    assert out["IsTruncated"] is False


# -- deletion (batch is litestream's only delete path) ----------------------


def test_delete_objects_batch(s3):
    for i in range(3):
        s3.put_object(Bucket=BUCKET, Key=f"d{i}", Body=b"x")
    out = s3.delete_objects(
        Bucket=BUCKET, Delete={"Objects": [{"Key": f"d{i}"} for i in range(3)]}
    )
    assert sorted(d["Key"] for d in out["Deleted"]) == ["d0", "d1", "d2"]
    assert out.get("Errors", []) == []
    assert s3.list_objects(Bucket=BUCKET).get("Contents", []) == []


def test_delete_of_absent_key_reports_success(s3):
    """S3 treats deleting a missing key as success. Litestream sweeps
    generations it may only have partly written, so a 404 here would fail a
    retry that ought to be a no-op."""
    out = s3.delete_objects(Bucket=BUCKET, Delete={"Objects": [{"Key": "ghost"}]})
    assert [d["Key"] for d in out["Deleted"]] == ["ghost"]
    assert out.get("Errors", []) == []


# -- multipart (required: s3manager sends snapshots this way) ---------------


def test_multipart_roundtrip(s3):
    key = "generations/abc/snapshot.lz4"
    up = s3.create_multipart_upload(Bucket=BUCKET, Key=key)
    upload_id = up["UploadId"]
    part_a, part_b = b"A" * (1024 * 1024), b"B" * 4096
    tags = []
    for number, chunk in ((1, part_a), (2, part_b)):
        r = s3.upload_part(
            Bucket=BUCKET, Key=key, PartNumber=number, UploadId=upload_id, Body=chunk
        )
        tags.append({"ETag": r["ETag"], "PartNumber": number})
    s3.complete_multipart_upload(
        Bucket=BUCKET, Key=key, UploadId=upload_id, MultipartUpload={"Parts": tags}
    )
    got = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    assert got == part_a + part_b
    assert (
        hashlib.sha256(got).hexdigest() == hashlib.sha256(part_a + part_b).hexdigest()
    )


def test_multipart_abort_leaves_no_object(s3):
    key = "aborted"
    up = s3.create_multipart_upload(Bucket=BUCKET, Key=key)
    s3.upload_part(
        Bucket=BUCKET, Key=key, PartNumber=1, UploadId=up["UploadId"], Body=b"x" * 16
    )
    s3.abort_multipart_upload(Bucket=BUCKET, Key=key, UploadId=up["UploadId"])
    with pytest.raises(ClientError) as e:
        s3.get_object(Bucket=BUCKET, Key=key)
    assert e.value.response["Error"]["Code"] == "NoSuchKey"


def test_complete_with_unknown_upload_id_is_refused(s3):
    with pytest.raises(ClientError) as e:
        s3.complete_multipart_upload(
            Bucket=BUCKET,
            Key="k",
            UploadId="does-not-exist",
            MultipartUpload={"Parts": [{"ETag": '"x"', "PartNumber": 1}]},
        )
    assert e.value.response["Error"]["Code"] == "NoSuchUpload"


# -- authentication ---------------------------------------------------------


def test_wrong_secret_is_rejected(tmp_path, s3):
    bad = get_session().create_client(
        "s3",
        endpoint_url=s3.meta.endpoint_url,
        aws_access_key_id=KEY,
        aws_secret_access_key="wrong-secret",
        region_name="us-east-1",
        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 1}),
    )
    with pytest.raises(ClientError) as e:
        bad.list_objects(Bucket=BUCKET)
    assert e.value.response["Error"]["Code"] == "SignatureDoesNotMatch"


def test_unknown_access_key_is_rejected(s3):
    bad = get_session().create_client(
        "s3",
        endpoint_url=s3.meta.endpoint_url,
        aws_access_key_id="AKIANOBODY",
        aws_secret_access_key=SECRET,
        region_name="us-east-1",
        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 1}),
    )
    with pytest.raises(ClientError) as e:
        bad.list_objects(Bucket=BUCKET)
    assert e.value.response["Error"]["Code"] == "InvalidAccessKeyId"


def test_credentials_can_be_scoped_to_buckets(tmp_path):
    """A key scoped to other buckets must not reach this one, so one endpoint
    can serve several jobs without each holding the others' data."""
    from werkzeug.serving import make_server

    store = JsonVolumeStore(str(tmp_path / "s.json"))
    registry = DirectSessionRegistry()
    rec = _record()
    registry.unlock(rec, None, str(tmp_path / "v.sqlite"))
    store.put(rec)
    app = create_app(
        store,
        supervisor=None,
        registry=registry,
        auth_mode="off",
        s3=True,
        s3_credentials={KEY: Credentials(SECRET, buckets={"somewhere-else"})},
    )
    server = make_server("127.0.0.1", 0, app, threaded=True)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        c = get_session().create_client(
            "s3",
            endpoint_url=f"http://127.0.0.1:{server.server_port}",
            aws_access_key_id=KEY,
            aws_secret_access_key=SECRET,
            region_name="us-east-1",
            config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 1}),
        )
        with pytest.raises(ClientError) as e:
            c.list_objects(Bucket=BUCKET)
        assert e.value.response["Error"]["Code"] == "AccessDenied"
    finally:
        server.shutdown()
        t.join(timeout=5)
        registry.lock(rec)
        store.close()


def test_s3_is_off_unless_asked_for(tmp_path):
    store = JsonVolumeStore(str(tmp_path / "s.json"))
    registry = DirectSessionRegistry()
    app = create_app(store, supervisor=None, registry=registry, auth_mode="off")
    try:
        assert app.config["S3"] is False
        # No S3 surface at all: the bucket route simply is not registered.
        assert not any(r.endpoint.startswith("s3.") for r in app.url_map.iter_rules())
    finally:
        store.close()


def test_enabled_without_credentials_refuses_to_start(tmp_path):
    """An S3 surface that authenticates nobody is worse than none."""
    store = JsonVolumeStore(str(tmp_path / "s.json"))
    try:
        with pytest.raises(RuntimeError, match="no credentials"):
            create_app(
                store,
                supervisor=None,
                registry=DirectSessionRegistry(),
                auth_mode="off",
                s3=True,
                s3_credentials={},
            )
    finally:
        store.close()


# -- reserved names ---------------------------------------------------------


def test_manager_routes_are_not_shadowed_by_the_s3_blueprint(s3):
    """The frontend mounts at the root; the manager's own paths must still
    win, and a volume named one of them must say so rather than 404."""
    import urllib.request

    with urllib.request.urlopen(s3.meta.endpoint_url + "/health") as r:
        assert r.status in (200, 503)  # the manager's health endpoint, not S3


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
