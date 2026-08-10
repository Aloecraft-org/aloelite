# WebDAV

<div align="center">

<img src="https://raw.githubusercontent.com/Aloecraft-org/aloelite/refs/heads/main/doc/aloelite.png" style="height:96px; width:96px;"/>

**Aloelite SQLite Filesystem**

[Overview](/README.md) | [Getting Started](/doc/GETTING_STARTED.md) | [Frequently Asked Questions](/doc/FAQ.md)

[Troubleshooting](/doc/TROUBLESHOOTING.md) | [Requirements Spec](/doc/REQUIREMENTS.md) | [Encryption Spec](/doc/ENCRYPTION.md) | [Roadmap](/doc/ROADMAP.md)
</div>

The WebDAV frontend (`manager/dav.py`) serves every volume over HTTP as a
network drive. It is a **peer of FUSE, not a layer on it**: of the four
frontends only FUSE consumes POSIX syscalls, and this one consumes the ops API
through the same `DirectSessionRegistry` the browser UI uses. So it needs no
kernel mount, no `fuse3`, no `libfuse3-dev`, and no root — it runs anywhere the
manager runs, including Windows and macOS hosts where FUSE was never an option.

It implements **RFC 4918 compliance class 1**. What that costs, and what class 2
would take, is [section 4](#4-class-2-locking-assessment) — read it before
pointing Windows or macOS at this.

## Contents
- [1. Running it](#1-running-it)
- [2. What is implemented](#2-what-is-implemented)
- [2a. Conditional requests](#2a-conditional-requests)
- [3. Client notes](#3-client-notes)
- [4. Class 2 locking assessment](#4-class-2-locking-assessment)
- [5. Design notes](#5-design-notes)

---

## 1. Running it

WebDAV is **off by default** and must be asked for. It is a second,
write-capable surface on every volume, and it is the feature that tempts people
to bind past loopback, so it does not appear by accident.

```bash
aloelite-web --webdav                 # or ALOELITE_WEBDAV=1
```

The URL space is `/dav/<volume-id>/<path>`, one DAV collection per volume,
addressed by volume id like every other manager route. `GET /volumes` lists the
ids.

```bash
# Linux
sudo mount -t davfs http://127.0.0.1:8080/dav/<vid> /mnt/vault

# rclone, anywhere (no locking needed, works fully against class 1)
rclone mount :webdav:/ /mnt/vault --webdav-url http://127.0.0.1:8080/dav/<vid>

# Windows
net use Z: http://127.0.0.1:8080/dav/<vid>

# macOS: Finder -> Go -> Connect to Server (read-only, see section 3)
```

### Authentication

**Basic, and Basic only.** No cookie is read on `/dav`. That is a security
property rather than an omission: with no ambient browser credential to ride,
the CSRF header the JSON API requires has nothing to defend, and demanding one
would lock out every real DAV client — none of them can send a custom header.

| Volume | Credential |
|---|---|
| Encrypted | Basic password **is the PIN**. Username is ignored. |
| Plain | None, mirroring the JSON API's content routes. |

Proving the PIN also *unlocks* the volume, exactly as `POST /volumes/<id>/mount`
does for a browser: point a client at the URL, type the PIN, and the volume
comes up. A later client on an already-unlocked volume gets its own engine mount
row, so `GET /volumes/<id>/mounts` stays an honest audit of who is attached.

> **Basic sends the PIN on every request.** Bind loopback (the default) or put
> TLS in front. Windows additionally refuses Basic over plain HTTP unless
> `BasicAuthLevel` is raised — see section 3.

Argon2id costs ~376 ms, so per-request PIN derivation is not viable — one Finder
listing is dozens of requests. `_PinCache` maps a salted hash of the presented
PIN to the engine mount token it produced, so the KDF runs once per credential
per manager lifetime. The PIN itself is never stored.

---

## 2. What is implemented

| Method | Backing ops call | Notes |
|---|---|---|
| `OPTIONS` | — | `DAV: 1`, `MS-Author-Via: DAV`, `Accept-Ranges: bytes`. Answers before auth. |
| `PROPFIND` | `stat`, `list`, `stat_by_id` | Depth 0 and 1. Depth infinity → 403 `propfind-finite-depth`. |
| `PROPPATCH` | `set_metadata` | Dead properties only; live ones → 403. |
| `GET` / `HEAD` | `open_read` + `seek` | Single-range `206`, `416` with `Content-Range: bytes */n`. `If-None-Match` → `304`, `If-Range` guards resumes. |
| `PUT` | `open_write` (TRUNCATE) | Streamed, bounded memory. `Content-Range` → 400. `If-Match` / `If-None-Match` → `412`. |
| `MKCOL` | `create_container` | Body → 415, exists → 405, missing parent → 409. |
| `DELETE` | `remove` / `remove_recursive` | Always recursive on a collection, per RFC. |
| `COPY` / `MOVE` | `copy` / `move` | `Destination`, `Overwrite`. |
| `LOCK` / `UNLOCK` | — | **405.** Class 1. See section 4. |

### Live properties

| Property | Source | Format |
|---|---|---|
| `resourcetype` | `NodeInfo.type` | `<collection/>` or empty |
| `getcontentlength` | `NodeInfo.size` | entries only; 404 on a collection |
| `getlastmodified` | `NodeInfo.modified_at` | RFC 1123 (`...GMT`) |
| `creationdate` | `NodeInfo.created_at` | ISO 8601 (`...Z`) |
| `getetag` | `node_id` + `modified_at` | **weak** — see below |
| `displayname` | `NodeInfo.name` | |
| `getcontenttype` | extension | `httpd/unix-directory` for collections |
| `supportedlock` / `lockdiscovery` | — | present and **empty** (class 1) |

**ETags are weak (`W/"..."`) and that is honest, not lazy.** There is no content
digest to use — `content.hash` is written `NULL` by every write path today — so
the best available validator is identity plus generation. `modified_at` is
milliseconds, so two writes inside the same millisecond alias. That is precisely
the case a weak validator is permitted to be wrong about. A strong ETag needs
either the hash column populated or the nanosecond timestamps that
[HANDOFF-0.4 §4.1](/doc/HANDOFF-0.4.md) already plans.

### Dead properties

Stored in the node's NODE-6 metadata map under a `dav:` key prefix. Capped at
64 KiB per node (→ 507). `set_metadata` is a **wholesale replace**, and
`fuse.py` keeps `mode` and `symlink` in that same map, so PROPPATCH merges
rather than replaces — otherwise every file Explorer touched would silently
lose its permission bits. There is a test pinning exactly that.

---

## 2a. Conditional requests

`If-Match`, `If-None-Match` and `If-Range` are honoured on every method that
reads or changes a resource. This is the part of the concurrency story that
does **not** need locking, and it is worth understanding before reaching for
class 2: `If-Match` is the standards-correct answer to the lost update, and it
is what most people actually want when they ask for locks.

### The ETag is strong, and that is load-bearing

`_etag` builds from `content.version`, the CV-3 committed version pointer,
which advances on every commit. The earlier form used `modified_at`, and had
to be labelled weak because milliseconds alias: a tight rewrite loop really
does land two commits in one tick, and `manager/test_dav.py` pins that.

Weak was not merely imprecise, it was disabling. `If-Match` and `If-Range`
both require **strong** comparison (RFC 9110 8.8.3.2), and a weak validator
can never satisfy one — so a weak ETag silently reduces both headers to
permanent no-ops. Every `If-Match` would `412` and every resumed download
would restart from zero, with nothing in any log to say why. Exposing
`version` on `NodeInfo` (one column on a join `get_node` already does) is what
makes the whole feature possible.

Collections have no content row, so they keep the weak `W/"id-mtime"` form.
That is all a collection can honestly offer, and nothing requires better.

### What each header buys

- **`If-Match: <etag>` — the lost update.** Two clients read version 4; the
  second to write is refused with `412` instead of silently discarding the
  first one's work. Available on `PUT`, `DELETE`, `MOVE`, `COPY`, `PROPPATCH`.
- **`If-None-Match: *` — create, don't clobber.** Upload safely without a
  preceding `HEAD`, and without racing between the two requests.
- **`If-None-Match: <etag>` on GET — `304`.** Ordinary revalidation.
- **`If-Range` — the resumed-download corruption guard.** This one fixed a
  live bug rather than adding conformance. A client resuming an interrupted
  transfer sends the validator it started with; the header was previously
  ignored, so if the file had changed, the requested range was served from the
  *new* content and spliced onto the prefix the client already held. A corrupt
  file, no error raised anywhere. rclone resumes, so this was reachable in
  normal use. Now a stale validator means the `Range` is dropped and the whole
  current entity is sent — more expensive, never wrong.

The date form of `If-Range` is deliberately **not** honoured. `Last-Modified`
is second-resolution, so it cannot distinguish a same-second change, and
accepting it would reintroduce the very corruption the header is there to
prevent. A date always falls back to the full body.

### Ordering

Preconditions are evaluated in RFC 9110 13.2.2 order, and — this is the part
that is easy to get wrong — **before** any destructive step. `PUT` checks
before `open_write`, which would otherwise truncate the content the
precondition exists to protect, and `MOVE`/`COPY` check before the
`Overwrite: T` delete of the destination, so a `412` cannot leave the
destination already gone. Both are pinned by tests.

---

## 3. Client notes

| Client | Class 1 verdict |
|---|---|
| **rclone** | Full read-write. Issues no `LOCK` at all. **Best experience.** |
| **davfs2** | Read-write with `use_locks 0`. |
| **curl / gvfs / KIO** | Fine. |
| **Windows Explorer** | Mounts, browses, reads. **Writes fail.** See below. |
| **macOS Finder** | Mounts **read-only**. No workaround. See below. |

### macOS — read-only, and there is no way around it

Apple's `webdavfs` takes a `LOCK` whenever a file is opened for write, and it
decides the mount's writability from the advertised compliance class. Apple's
own description of the behaviour is unambiguous: class 1 servers are mounted as
read-only volumes, class 2 servers as read-write. Unlike Windows there is **no
client-side switch** to disable this.

If macOS read-write matters, class 2 is not optional. Until then, point macOS
users at rclone or Cyberduck/Mountain Duck.

### Windows — mounts, then fails on save

The redirector issues `LOCK` before `PUT` when a handle is opened for write, so
the failure is at *save time*, not mount time: the drive maps, browsing works,
reads work, deletes and renames largely work — and then the first save fails
with a generic Win32 error. That is arguably worse than read-only, because the
share looks fully functional until it isn't.

Two workarounds exist today, both with real costs:

1. **`SupportLocking=0`** — a `DWORD` under
   `HKLM\SYSTEM\CurrentControlSet\Services\WebClient\Parameters`, then restart
   the WebClient service. Suppresses the `LOCK` so the bare `PUT` succeeds. It
   is a documented, supported setting, but it is a per-machine admin registry
   edit that affects *every* WebDAV share on that box — not something to ask
   end users for.
2. **rclone + WinFsp** — sidesteps the redirector entirely, and with it the
   50 MB cap and the Basic-auth restriction below. Needs a user-side install.

Other Windows redirector limits, none of which the server can fix:

- **`FileSizeLimitInBytes` defaults to ~50 MB** (50,000,000). Larger transfers
  fail. Raise it under the same registry key (max `0xFFFFFFFF`, 4 GB).
- **Basic over plain HTTP is disabled by default.** Either raise `BasicAuthLevel`
  or — much better — put TLS in front, which needs no client-side change at all.

**Least-friction Windows configuration: TLS + a machine-trusted certificate +
class 2.** That is zero client-side configuration. Everything else is registry
edits.

> Evidence note: the macOS and Windows behaviours above are drawn from vendor
> documentation and long-standing field reports, not from testing against those
> clients in this repo's CI — there is no Windows or macOS runner. Treat the
> exact failure modes as well-supported but unverified here, and confirm against
> a real client before promising anything to users.

---

## 4. Class 2 locking assessment

**Verdict: viable, and cheaper than it looks — because the hard part is already
built.** The estimate is **5–8 engineer-days**, with no `SCHEMA_ERA` bump.

### 4.0 The ladder between here and there

Class 2 is not the only rung, and the intermediate ones split across two goals
that are easy to conflate. Most of the cheap work makes the current frontend
*safer*; only the last two make a desktop client mount *read-write*.

| # | Step | Goal | Cost | State |
|---|---|---|---|---|
| 1 | Conditional requests (`If-Match`/`If-None-Match`/`If-Range`) | safety | ~½ day | **done** — section 2a |
| 2 | Engine lock checks on `move`/`remove`/`set_metadata` | safety | ~1 day | **done** — ACC-11 |
| 3 | Decouple lock lifetime from the descriptor | safety | ~1–2 days | open |
| 4 | TLS | Windows | small | open |
| 5 | Class 2 lite: exclusive, depth-0, minimal `If:` | read-write | ~3–4 days | open |

Rung 1 partly substitutes for locking rather than building toward it:
optimistic concurrency is what most people are really after when they ask for
locks, and it costs no protocol state.

Rung 2 is worth doing on its own merits — locks currently guard only the five
content-write paths (`write_all`, `append`, `write_range`, `truncate`,
`open_write`), so `move`, `remove` and `set_metadata` ignore them entirely.
That is **FUSE-visible too**: today you can `rm` or `mv` a file another mount
holds open for write. Rung 3 additionally buys the descriptor abort that would
close the interrupted-`PUT` window in section 5.

**Order matters for honesty, not just speed.** Rung 5 can be built on
manager-memory locks alone, and would then advertise `DAV: 1, 2` truthfully
for DAV-vs-DAV while quietly lying about DAV-vs-FUSE and DAV-vs-web-UI. Doing
2 and 3 first puts it on engine lock rows, where `direct.py`'s
one-mount-row-per-client design makes cross-frontend exclusion fall out for
free. Same destination; one route ships a claim that is true.

One cost to price in: engine-level lock semantics from rungs 2–3 belong in
`conformance/scenarios/` so the Rust, JS/WASM and Kotlin ports inherit the
oracle instead of re-deriving it. That is real extra work, and it is also the
only way the semantics get pinned once rather than four times.

### 4.1 What the engine already gives you

The `lock` table is not a stub. `schema.sql:211` carries
`(lock_id, mount_id, node_id, read_count, write_count, expires_at, created_at)`
with `valid_lock`, `prunable_lock` and `mount_lock` views over it, and four of
the six things a DAV lock needs are already there:

| DAV lock needs | Engine today | Gap |
|---|---|---|
| A client-visible token | `lock_id` is a uuid7 → `opaquelocktoken:<uuid>` | none |
| A timeout | `expires_at` column, **already honoured** by `valid_lock` | none |
| Reaping on expiry | `prunable_lock` + `maintenance.prune_locks` | none |
| Cross-client exclusion | `validation.check_lock_held`: `node_id = :node AND mount_id <> :mount` | none |
| Refresh | — | one `UPDATE` template |
| An owner string | no column | store manager-side (see 4.3) |

**The single most important finding is the fourth row.** `manager/direct.py`
already gives every attached client *its own engine mount row* on the shared
connection. Locks are attributed per-mount. So the moment DAV locks are engine
rows, exclusion between two DAV clients is correct **for free** — and so is
exclusion between a DAV client and a **FUSE writer on the same volume**, because
that is just another `mount_id`. Multi-frontend lock coherence falls out of the
existing design rather than needing to be built.

### 4.2 What is genuinely missing

**A lock's lifetime is welded to a descriptor.** A lock row is created in exactly
one place — `operations.py:1108`, inside `open_write` — and deleted in exactly
one place, `Descriptor.close()`. There is no way to hold a lock without holding
an open write handle. A WebDAV lock is the opposite: it lives *across* requests
with nothing open. This is the actual work.

**Only the write paths check.** `check_lock_held` is consulted by `write_all`
(:557), `append` (:602), `write_range` (:678), `truncate` (:756) and `open_write`
(:1106). It is **not** consulted by `move`, `copy`, `remove`, `remove_recursive`,
`create_container` or `set_metadata`. RFC 4918 requires a locked resource to
answer 423 to `DELETE`, `MOVE` and `PROPPATCH` too, so those checks have to be
added somewhere.

**The check is flat.** `check_lock_held` matches one `node_id`. A depth-infinity
lock on a collection — which is what Explorer and Finder take on a folder — needs
a recursive check. The `recursive` template group and `enumerate_subtree` already
exist, so this is a new template rather than new machinery.

**The conformance suite pins today's behaviour on purpose.**
`conformance/scenarios/locking.yaml` exists specifically to stop a port
mistaking `read_count`/`write_count` for a specification: they are *recorded and
not enforced*, and the scenarios say so. Any shared-lock work has to update
those scenarios deliberately, as a cross-implementation decision, not as a
side effect. That is a governance cost, not a coding one, and it is the reason
to keep the first cut **exclusive-only**.

### 4.3 Recommended shape: hybrid, engine-backed exclusion

Of the three options — manager-only locks, fully engine-backed locks, or a
hybrid — take the **hybrid**:

- **The engine `lock` row is the lock.** It provides identity (`lock_id` → token),
  expiry (`expires_at`), reaping, and the cross-mount/cross-frontend exclusion
  that a manager-only table could never give you against FUSE.
- **The manager holds the DAV-only attributes** — the `owner` XML, the requested
  depth, the scope — in memory, keyed by `lock_id`. This is what avoids a schema
  change: no `lock.owner` column, no era bump. RFC 4918 explicitly tolerates a
  server losing locks (clients recover via 412), so locks not surviving a manager
  restart is a conformant, documented trade rather than a defect.

A manager-only lock table was rejected because it cannot block a FUSE writer, and
"the DAV client thinks it holds an exclusive lock while FUSE overwrites the file"
is exactly the corruption a lock exists to prevent. Fully engine-backed was
rejected only for the `owner` column, which is not worth a migration on its own —
fold it into era 2 later if it proves useful.

### 4.4 Change set

Additive throughout. **No `SCHEMA_ERA` bump.**

| File | Change |
|---|---|
| `aloelite/config/sql-templates.yaml` | `refresh_lock` (UPDATE expires_at), `get_lock`, `check_lock_held_recursive` |
| `aloelite/operations.py` | `acquire_lock` / `refresh_lock` / `release_lock` / `lock_info` — standalone, not descriptor-bound |
| `aloelite/aloelite.py` | the four `Mount` wrappers |
| `aloelite/models.py` | `LockInfo` **already exists** and already has the right shape |
| `aloelite/config/mount-api.yaml` | spec entries for the four new ops (cross-implementation contract) |
| `aloelite/operations.py` | lock checks in `move`, `remove`, `remove_recursive`, `set_metadata` |
| `conformance/scenarios/locking.yaml` | scenarios for standalone acquire/refresh/expiry |
| `manager/dav.py` | `LOCK`/`UNLOCK` handlers, `If:` header parsing, `_COMPLIANCE = "1, 2"`, real `supportedlock`/`lockdiscovery` |

The `If:` header is the one piece with no existing analogue and the easiest to
underestimate: it is a small grammar (No-tag-list and Tagged-list, state tokens
and ETags), every mutating method must honour it, and getting it wrong produces
the "file is locked and I can't unlock it" failures that make users hate WebDAV.
Budget for it explicitly.

### 4.5 Scope call for the first cut

Ship **exclusive write locks only**, advertised as such in `supportedlock`.
Windows and macOS both take exclusive write locks, so this is the whole
motivating use case. Shared locks would force the `read_count`/`write_count`
policy decision that `locking.yaml` deliberately left open — a
cross-implementation commitment binding the Kotlin and Rust ports, which should
be made on its own merits and not as a WebDAV side effect.

Depth-infinity collection locks are needed (both clients take them on folders),
so `check_lock_held_recursive` is in scope for the first cut.

---

## 5. Design notes

**Why hand-rolled rather than WsgiDAV.** WsgiDAV is mature, MIT, and actively
maintained, and it was the obvious first answer. It lost on two counts. It pulls
in `defusedxml`, `json5`, `passlib` and `bcrypt` — four new dependencies,
including a compiled one, into a project whose `pyproject.toml` argues about
dependency floors line by line and isolates even `pyfuse3` behind an extra. And
its `DAVProvider` abstraction brings its own `LockManager` and `PropertyManager`,
both of which would sit *beside* the engine's `lock` table and NODE-6 metadata
rather than on top of them — precisely the duplication section 4 exists to
avoid. The protocol layer is the easy part; the aloelite-specific semantics
(same-name sibling visibility, containers versus entries, wholesale metadata
replace) are what needed the care, and a generic provider would have papered
over them.

**Why no `defusedxml`.** Every entity-expansion and external-entity attack needs
a DTD, and no legitimate DAV body carries one. `_parse_xml` refuses any body
containing a `DOCTYPE` and caps body size; ElementTree already rejects undefined
entities. That is auditable in a way a hand-rolled entity budget is not.

**Percent-decoding happens exactly once, and which side does it differs.**
Werkzeug already decodes a routed `<path:>` parameter, so decoding it again
would make a file genuinely named `a%20b.txt` alias onto one named `a b.txt` —
two distinct names collapsing onto one node. The `Destination` header is the
mirror case: a raw URL nothing has touched, which must be decoded exactly once
in the DAV layer. Both directions are pinned by tests.

**`MOVE` with `Overwrite: T` deletes the destination first.** The engine has no
replace-in-place, and NODE-5 permits same-name siblings with one visible, so a
naive `move` onto an existing name leaves *two* children rather than replacing
one.

**Two non-atomic windows, both pinned by tests.** Neither is a WebDAV-layer
choice so much as a consequence of what the engine offers, and both are worth
knowing before pointing a flaky network at this:

- **An interrupted `PUT` commits what arrived**, replacing the previous
  content. `Descriptor.close()` commits unconditionally and there is no abort,
  so the only alternative — skipping `close()` on the error path — would strand
  the write lock and leave the resource *unwritable* rather than merely
  truncated. It matches what FUSE does with a partial write. A clean fix needs
  an engine-level descriptor abort: discard staged chunks, release the lock,
  leave the committed pointer alone. Cheap to add — the staged-chunks-above-the-
  committed-pointer machinery already exists for exactly this shape of crash.
- **`MOVE`/`COPY` with `Overwrite: T` deletes the destination first**, so a
  failure between the delete and the move loses the destination's old content.
  The engine has no replace-in-place, and the alternative (move first) is worse:
  NODE-5 permits same-name siblings, so it would leave two children rather than
  replacing one.

**Known costs, inherited rather than introduced:**

- A Depth:1 PROPFIND is one `list()` plus one `stat_by_id()` per child, because
  `DirEntry` carries no size or mtime. This is the same N+1 the JSON listing
  endpoint already pays. The fix, if it is ever needed, is a detail-carrying
  listing template in the engine — not a cache in the manager.
- Every request holds that volume's session lock for its duration, because the
  engine owns one sqlite connection per volume and adds no thread safety. A large
  GET or PUT serializes other operations on the same volume. A streaming GET
  hands lock ownership to its response generator and releases it on WSGI
  `close()`, so a client that disconnects mid-transfer cannot wedge the volume.

---

<div align="center">
Copyright Michael Godfrey 2026 | <a href="https://aloecraft.org">aloecraft.org</a>
</div>
