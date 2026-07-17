# Troubleshooting

<div align="center">

<img src="aloelite.png" style="height:96px; width:96px;"/>

**Aloelite SQLite Filesystem**

[Overview](/README.md) | [Getting Started](/doc/GETTING_STARTED.md) |  [Frequently Asked Questions](/doc/FAQ.md) 

**Troubleshooting (This Document)** | [Requirements Spec](/doc/REQUIREMENTS.md) | [Encryption Spec](/doc/ENCRYPTION.md)
</div>

## FUSE

### "Transport endpoint is not connected"

The FUSE session died or was detached while a consumer still held the
path. Clean up and remount:

```bash
fusermount3 -uz /path/to/mountpoint
```

Then mount again. If this happens under the manager, unmount and remount
the volume via the API or admin panel; the manager's next restart also
sweeps stale mounts automatically.

### Mount succeeds but another user/container sees an empty directory

FUSE mounts are private to the mounting user unless `allow_other` is
active, and `allow_other` requires this line, uncommented, in
`/etc/fuse.conf` **on the machine doing the mounting** (inside the
container, for the manager):

```
user_allow_other
```

This is the single most common silent failure.

### An operation returns "Operation not supported" (ENOTSUP)

A few access patterns aren't supported and are refused rather than
risking corruption — notably seeking backwards into already-written bytes
on a write-only streaming handle. Opening the file read-write (O_RDWR)
instead gives full random access.

### Running SQLite inside a mounted volume

Rollback-journal mode works; WAL does not yet (needs mmap). The recipe:

```sql
PRAGMA journal_mode=PERSIST;   -- or TRUNCATE; avoids journal unlink churn
PRAGMA busy_timeout=5000;      -- readers wait for the writer instead of erroring
```

Every page write commits a content version, so a busy database grows the
.fs file until pruned. Set retention on the db file and prune periodically:

```python
m.set_retention("/test.db", keep=1)
```
```bash
aloelite -f file.fs prune --vacuum
```

### Wrong PIN / "volume is encrypted but no PIN was given"

`aloelite-fuse` exits with a message naming the problem. Check which of
`--pin` / `--pin-file` / `--pin-env` you passed; precedence is in that
order, so a stray `--pin` flag wins over the file or env var you meant
to use.

---

## Manager in a container

The manager runs preflight checks at startup and refuses to start if the
environment can't support FUSE. Each failure prints a `[preflight] FAIL`
line naming the fix. The usual suspects:

| Preflight failure | Fix |
|---|---|
| `/dev/fuse missing` | add `--device /dev/fuse` to `docker run` |
| `CAP_SYS_ADMIN not in CapEff` | run with `--privileged` (or `--cap-add SYS_ADMIN`) |
| `/mnt has no shared propagation` | mount with `:rshared`: `-v /mnt/aloelite:/mnt:rshared` |
| `/aloelite-root missing or not writable` | add `-v /aloelite-root:/aloelite-root` |
| `fusermount3 not on PATH` | image problem — rebuild/update the image |

A `[preflight] WARN` about `user_allow_other` means consumer containers
may see empty mount directories — see the FUSE section above.

### Volume shows "mounted" but the directory is empty on the host

Propagation. The `/mnt` bind must be `:rshared` (see table above), and
the host directory must exist before the container starts. Check
readiness with:

```bash
curl -s localhost:8080/volumes/<id>/mount   # look for "ready": true
```

`mounted: true, ready: false` means the FUSE session is gone; unmount and
remount.

### Container restarted and everything shows unmounted

Expected. FUSE sessions can't survive a process restart, so the manager
clears mount state at startup and defensively unmounts any kernel-side
residue. Remount via the API or admin panel. (Auto-remount is on the
roadmap.)

---

## CLI

### "multiple volumes; pick one with -v"

The file holds more than one volume and the CLI won't guess. It lists the
candidates in the error; pass `-v NAME` or `-v ID` (ids work with or
without dashes).

### "wrong PIN" on a volume you just created

Encryption is decided at volume **creation**. If the first-ever command
against that volume name ran without a PIN, the volume was created
unencrypted, and supplying a PIN afterward is rejected (and vice versa).
Check with:

```bash
aloelite -f file.fs volumes
```

If a volume was created with the wrong encryption state, create a new one
under a different name and copy the data across.

---

## Python

### `InvalidTag` on read

Almost always a wrong key reaching the decryptor. If this appears on a
file written by an old (pre-ciphertext-addressing) version with multiple
encrypted volumes, the file may hold cross-volume aliased chunks from a
fixed bug; data written after the fix is unaffected, but affected entries
from before it cannot be repaired — restore them from a snapshot.

### `MountInvalid` mid-script

The mount was unmounted or its TTL expired (possibly from another
connection). Mounts are durable records, but a retired or expired one no
longer grants access — open a fresh one with `fs.mount(...)`. If you're
using TTLs, `Mount.renew(ttl_ms)` extends a still-valid mount.

### `LockHeld`

Another mount holds an exclusive write lock on that entry (an open write
descriptor). Locks release when the descriptor closes; abandoned locks
(crashed writer, expired mount) are reclaimed by `fs.prune()`.

### File keeps growing even though content doesn't

Every write commits a new content version, and superseded versions are
kept until pruned. For hot files set a retention policy and prune
periodically:

```python
m.set_retention("/hot.log", keep=3)
...
fs.prune_content()
```

`fs.health_check()` returning `[]` confirms the file is structurally
consistent.