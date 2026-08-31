# Using Aloelite as a Backup Target

<div align="center">

<img src="https://raw.githubusercontent.com/Aloecraft-org/aloelite/refs/heads/main/doc/aloelite.png" style="height:96px; width:96px;"/>

**Aloelite SQLite Filesystem**

[Overview](/README.md) | [S3 Frontend](/doc/S3.md) | [WebDAV](/doc/WEBDAV.md) | [Windows](/doc/WINDOWS.md)
</div>

Point a backup tool at an aloelite volume over S3. Written to be actionable
without reading the rest of the documentation: everything needed to configure
a client is here, and the reasoning lives in [S3.md](/doc/S3.md).

---

## What the endpoint is

An aloelite manager with `ALOELITE_S3=1` serves an S3 subset at its root.
**A bucket is a volume**, addressed by the volume's *name*.

```
http://<host>:<port>/<bucket>/<key>
```

It implements exactly what [litestream][ls] 0.3.13 calls: `ListObjects` (V1),
`GetObject`, `PutObject`, multipart upload, and batch `DeleteObjects`. There
is no `CreateBucket`, no `CopyObject`, no versioning, no ACLs.

[ls]: https://litestream.io

## Four things that will bite, in order of likelihood

**1. `force-path-style: true` is required.** Litestream does not infer
path-style addressing from `endpoint`. `s3.ParseHost` sets it only for hosts
it recognises as a known provider; a bare `s3://bucket/prefix` URL falls
through to `forcePathStyle = false`, and the SDK then addresses
`http://bucket.host:port/key`, which needs wildcard DNS you almost certainly
do not have. Symptom: DNS resolution failures naming a host that is your
bucket name glued to your endpoint.

**2. The bucket must already exist, and its name is the volume's name.**
There is no `CreateBucket`. Create it through the manager's own API first
(below). A name with a space or a slash will not work as a bucket.

**3. ETags are opaque, not MD5.** `"<node-id>-<version>"`. Litestream does not
care. A tool that verifies `ETag == MD5(body)` will. See
[S3.md](/doc/S3.md#etags-are-opaque-and-why) for why this is deliberate and
not fixable by choice.

**4. A trailing newline in the secret breaks the signature.** Writing the key
with `Set-Content` or `echo` appends one, and SigV4 will then fail as
`SignatureDoesNotMatch` with nothing pointing at whitespace. Trim it on the
client side; the server strips it when reading `ALOELITE_S3_SECRET_FILE`.

## Standing up the target

```bash
export ALOELITE_S3=1
export ALOELITE_S3_ACCESS_KEY=AKIALOCALBACKUP
export ALOELITE_S3_SECRET_FILE=/etc/aloelite/s3-secret     # not _KEY, if you can
export ALOELITE_S3_BUCKETS=backups                         # optional scope
aloelite-web --host 0.0.0.0 --port 7081 --root /var/lib/aloelite --insecure
```

On Windows, see [WINDOWS.md](/doc/WINDOWS.md) — the scheduled-task installer
in `script/windows/` does this and the drive mapping together.

`--insecure` is needed only when `--webdav` is also on and the bind address is
not loopback: WebDAV's Basic auth carries the volume PIN, so aloelite refuses
to serve it in the clear off loopback. An **unencrypted** volume issues no
Basic challenge at all, so on one there is no PIN to leak. Object data is
still cleartext, so use a VPN interface or terminate TLS in front.

Create the bucket:

```bash
curl -X POST http://127.0.0.1:7081/volumes \
  -H 'Content-Type: application/json' \
  -d '{"name": "backups", "encrypted": false}'
```

## Configuring litestream

```yaml
dbs:
  - path: /var/lib/yourapp/app.sqlite
    replicas:
      # Keep the existing AWS replica. This is an ADDITIONAL destination, not
      # a replacement -- an aloelite instance on your own hardware is not an
      # independent failure domain the way S3 is.
      - name:     aws
        url:      s3://your-real-bucket/app
        # ... existing config ...

      - name:     aloelite
        url:      s3://backups/app
        endpoint: http://<aloelite-host>:7081
        region:   us-east-1
        force-path-style: true               # REQUIRED -- see above
        access-key-id:     AKIALOCALBACKUP
        secret-access-key: <the secret, no trailing newline>
        sync-interval:     10s
        snapshot-interval: 24h
        retention:         720h
```

Litestream replicates to every configured replica independently, so a
failing aloelite replica does not affect the AWS one.

## Verifying it before trusting it

**From the machine that will do the backing up**, not from the aloelite host —
the difference is the firewall and the bind address, which is where this
fails:

```bash
python script/s3_smoke.py --endpoint http://<aloelite-host>:7081 \
    --bucket backups --access-key AKIALOCALBACKUP --secret-key "$(cat secret)"
```

Six checks: put/get, a deep key's implied prefixes, flat listing, listing with
`delimiter=/`, a multipart upload past the 5 MiB part size, and a batch
delete. `script/s3_smoke.py` is in the repository, not in the installed
package — fetch it from the branch or a checkout. It needs `botocore`.

Then the real acceptance test, which is the only one that proves the backup is
a backup:

```bash
litestream replicate -config litestream.yml     # let it run
litestream restore -config litestream.yml -o /tmp/restored.sqlite /path/to/app.sqlite
sqlite3 /tmp/restored.sqlite 'PRAGMA integrity_check;'
```

A restore that produces a byte-identical database is the bar. Anything less is
a replication that has not been shown to reverse.

## Operational limits worth planning around

- **One volume is one SQLite file with one writer.** Writes to *different*
  volumes are fully parallel; writes to the same volume serialise. The
  streaming writer commits per ~1 MiB chunk rather than per upload, so
  concurrent uploads interleave rather than block for a whole transfer. At
  litestream's volume (a `sync-interval` of seconds, small WAL segments) this
  is not a constraint. At high concurrency into one bucket it becomes one.
- **Multipart parts stage in memory** and do not survive a manager restart. An
  interrupted upload is abandoned and the client retries the whole object.
- **Request bodies are buffered** to authenticate them (SigV4 covers a payload
  hash), capped at 64 MiB — well above the 5 MiB part size an SDK uses.
- **Dedup will not help much here.** WAL segments are deltas, and litestream
  compresses them, which destroys chunk-level similarity. Do not size storage
  assuming deduplication.
- **This target does not back itself up.** If the aloelite instance is the
  only copy of something, it needs its own replication — litestream pointed at
  the `.sqlite` file works, and is the same tool.

## Security posture

- **The S3 credential is not the volume PIN.** A key can be scoped to a set of
  buckets with `ALOELITE_S3_BUCKETS`, so one endpoint can serve several jobs
  without each holding the others' data.
- **A backup key can currently read back everything it can write.** A
  write-only (drop-box) credential is designed but not implemented — see
  [S3.md](/doc/S3.md#planned-a-write-only-drop-box-credential). Until then,
  treat the key as read/write on its buckets.
- **Volume metadata is not encrypted.** Aloelite encrypts file *contents*;
  names, sizes, timestamps and directory structure are plaintext in the
  `.sqlite` file. For litestream that metadata is generation ids and segment
  offsets, which leak little — but do not assume a volume is opaque at rest.
