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

It implements **RFC 4918 compliance class 2** — `LOCK`/`UNLOCK`, Depth 0 and
Depth infinity, exclusive write locks. That is what makes macOS Finder mount
read-write instead of read-only, and what stops the Windows redirector failing
at first save. [Section 4](#4-class-2-locking-assessment) records how it is
built and what was deliberately left out.

## Contents
- [1. Running it](#1-running-it)
- [2. What is implemented](#2-what-is-implemented)
- [1a. TLS](#tls)
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

`DAV: 1, 2` is advertised, so Finder and Explorer mount read-write.

The URL space is `/dav/<volume-id>/<path>`, one DAV collection per volume,
addressed by volume id like every other manager route. `GET /volumes` lists the
ids.

```bash
# Linux
sudo mount -t davfs http://127.0.0.1:8080/dav/<vid> /mnt/vault

# rclone, anywhere (ignores locking entirely; fine either way)
rclone mount :webdav:/ /mnt/vault --webdav-url http://127.0.0.1:8080/dav/<vid>

# Windows (TLS required off loopback; the cert must be machine-trusted)
net use Z: https://host.example.org:8080/dav/<vid>

# macOS: Finder -> Go -> Connect to Server
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

> **Basic sends the PIN on every request.** Bind loopback (the default) or
> serve TLS — see below. Serving WebDAV on a non-loopback address without TLS
> is **refused**, not warned about.

Argon2id costs ~376 ms, so per-request PIN derivation is not viable — one Finder
listing is dozens of requests. `_PinCache` maps a salted hash of the presented
PIN to the engine mount token it produced, so the KDF runs once per credential
per manager lifetime. The PIN itself is never stored.

<a id="tls"></a>
### TLS

```bash
aloelite-web --webdav --tls-self-signed              # generated once, reused
aloelite-web --webdav --tls-cert C.pem --tls-key K.pem
```

TLS matters more here than for a typical local app, because the Basic password
**is the volume PIN**: over plain HTTP it crosses the network in base64 on
every request, and one Finder listing is dozens of requests.

So the guard is a **refusal, not a warning**. `--webdav` on a non-loopback
address with no TLS exits with a message listing the four ways out
(`--tls-self-signed`, `--tls-cert`/`--tls-key`, `--host 127.0.0.1`, or
`--insecure`). `--insecure` exists because terminating TLS in a reverse proxy
is a legitimate and common deployment — but it has to be said out loud. Plain
HTTP without `--webdav` is untouched; the JSON API's own exposure is a separate
concern with its own warning.

**What `--tls-self-signed` does and does not buy.** It encrypts the connection,
which is the whole point for the PIN. It does not *authenticate* the server to
a client that has not been told to trust it, and clients differ sharply:

| Client | Reaction to a self-signed certificate |
|---|---|
| Browsers | interstitial, click-through |
| rclone | works with `--no-check-certificate` |
| Windows redirector | **refuses outright**, no override, until imported into Trusted Root |
| macOS Finder | wants it trusted in Keychain |

The certificate is generated once into `<root>/tls` and **reused** across
restarts. Werkzeug's `ssl_context="adhoc"` mints a fresh one per start, which
shows every client a new untrusted identity every time and makes trusting it
once impossible. The SHA-256 fingerprint is printed at startup — that is the
value to compare when trusting it elsewhere, and without comparing it "just
trust it" is indistinguishable from trusting an interceptor.

Two details chosen against real client limits: validity is **825 days**,
because macOS rejects longer-lived server certificates even when manually
trusted; and the SAN list covers `localhost`, the machine hostname, `.local`,
`127.0.0.1`, `::1` and the bind address, because a certificate is validated
against the name the *client* typed, not the address the server bound. A
`0.0.0.0` bind is expanded rather than embedded — it is not a connectable name.
Changing `--host` to something the existing certificate does not cover
regenerates it, rather than failing later with an error naming the wrong
problem.

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
| `LOCK` / `UNLOCK` | `lock` / `unlock` + `davlock` | Exclusive write, Depth 0 and infinity. Refresh via body-less `LOCK`. Shared → 409. |

### Live properties

| Property | Source | Format |
|---|---|---|
| `resourcetype` | `NodeInfo.type` | `<collection/>` or empty |
| `getcontentlength` | `NodeInfo.size` | entries only; 404 on a collection |
| `getlastmodified` | `NodeInfo.modified_at` | RFC 1123 (`...GMT`) |
| `creationdate` | `NodeInfo.created_at` | ISO 8601 (`...Z`) |
| `getetag` | `node_id` + `content.version` | **strong** for entries, weak for collections — see [2a](#2a-conditional-requests) |
| `displayname` | `NodeInfo.name` | |
| `getcontenttype` | extension | `httpd/unix-directory` for collections |
| `supportedlock` / `lockdiscovery` | `davlock` registry | real state; empty `lockdiscovery` = not locked |

**ETags are strong for entries**, built from `content.version` rather than from
`modified_at`. Why that matters — and why the weak form the first cut shipped
would have quietly disabled `If-Match` and `If-Range` entirely — is
[section 2a](#2a-conditional-requests). Collections have no content row and keep
the weak `W/"..."` form.

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

### macOS — read-only under class 1, which is why class 2 was built

Apple's `webdavfs` takes a `LOCK` whenever a file is opened for write, and it
decides the mount's writability from the advertised compliance class. Apple's
own description of the behaviour is unambiguous: class 1 servers are mounted as
read-only volumes, class 2 servers as read-write. Unlike Windows there is **no
client-side switch** to disable this.

Class 2 is now advertised, so Finder should mount read-write. That inference is
from Apple's documented rule rather than from a Mac in CI — see the evidence
note at the end of this section.

### Windows — the save-time failure class 2 removes

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
  or — much better — serve TLS, which needs no client-side change at all.
  **TLS is now built in** (see [section 1](#tls)), so this one is solved
  server-side: `--tls-cert`/`--tls-key` with a certificate the machine already
  trusts requires nothing on the client. Note that `--tls-self-signed` does
  **not** clear this hurdle by itself — the redirector refuses an untrusted
  certificate outright, with no click-through, so a self-signed cert must be
  imported into the machine's Trusted Root store first.

**Least-friction Windows configuration: TLS + a machine-trusted certificate +
class 2.** All three now exist — TLS ([1a](#tls)), class 2 ([4](#4-class-2-locking)),
and a trusted certificate is the one piece that is site-specific. `SupportLocking=0`
should no longer be needed, since the `LOCK` the redirector issues before `PUT`
is now answered. The 50 MB `FileSizeLimitInBytes` cap is unrelated and remains.

> Evidence note: the macOS and Windows behaviours above are drawn from vendor
> documentation and long-standing field reports, not from testing against those
> clients in this repo's CI — there is no Windows or macOS runner. Treat the
> exact failure modes as well-supported but unverified here, and confirm against
> a real client before promising anything to users.

---

## 4. Class 2 locking

**Built.** The original estimate was 5-8 engineer-days with no `SCHEMA_ERA`
bump; it landed across five increments (the ladder below) and needed no era
bump, as predicted.

### 4.a How it is actually built: locks live in two places

This is the one thing to understand before changing any of it, and it corrects
an assumption in the original assessment.

That assessment said `direct.py`'s one-mount-row-per-client design would make
cross-client exclusion work for free. **That is wrong for the DAV path.** Every
DAV client on a volume shares ONE engine mount: an unencrypted volume serves
them all from the primary mount, and an encrypted one caches the engine mount
token against *the PIN*, not the client, so two Windows machines with the same
PIN are one mount as far as the engine is concerned. Since `check_lock_held`
excludes the caller's own mount, **engine locks give zero DAV-vs-DAV
exclusion**. Relying on them alone would have produced a lock that looks
correct and silently permits the exact conflict it was taken to prevent.

So the hybrid the assessment recommended is not merely preferable, it is
required:

| Layer | Excludes | Holds |
|---|---|---|
| `manager/davlock.py` | DAV vs DAV | token, `<owner>`, Depth, timeout |
| `ops.lock` (engine) | DAV vs FUSE vs browser UI | the engine lock row |

The manager registry is authoritative for DAV; the engine lock is taken
alongside it and released with it. If the engine refuses, the `LOCK` fails too
— that is a real cross-frontend conflict, not an implementation detail.

**Locks are in memory and die with the process.** RFC 4918 6.2 permits exactly
that, clients already handle a lock vanishing, and the alternative is a
`lock.owner` column and a schema era bump for state whose natural lifetime is
minutes. The timeout does the same job across a restart.

### 4.b What was deliberately left out

- **Shared locks → 409.** ACC-7 makes the engine exclusive-only and
  `read_count`/`write_count` are recorded-not-enforced *on purpose*. Granting a
  shared lock would promise what nothing underneath can keep, and would force
  the multi-reader policy decision that binds the Rust and Kotlin ports.
- **Lock-null resources.** `LOCK` on an unmapped URL creates an empty resource
  (RFC 4918 7.3) rather than a lock-null resource, which is what the RFC
  prefers and what clients expect from `save as`.
- **`If:` header evaluation is used for lock authorisation and precondition
  matching, but tagged lists are resolved against the request-URI's state**
  rather than fetching each tagged resource. Every real client tags with the
  request-URI or omits the tag.

### 4.0 The ladder between here and there

Class 2 is not the only rung, and the intermediate ones split across two goals
that are easy to conflate. Most of the cheap work makes the current frontend
*safer*; only the last two make a desktop client mount *read-write*.

| # | Step | Goal | Cost | State |
|---|---|---|---|---|
| 1 | Conditional requests (`If-Match`/`If-None-Match`/`If-Range`) | safety | ~½ day | **done** — section 2a |
| 2 | Engine lock checks on `move`/`remove`/`set_metadata` | safety | ~1 day | **done** — ACC-11 |
| 3a | Descriptor `abort` + scope-exit semantics | safety | ~1 day | **done** — `streaming.abort` |
| 3b | Lock as a first-class object (no descriptor) | class 2 | ~1–2 days | **done** — `locking.*` |
| 4 | TLS | Windows | small | **done** — [1a](#tls) |
| 5 | Class 2: exclusive locks, Depth 0 + infinity, `If:` | read-write | ~2–4 days | **done** — [4.a](#4a-how-it-is-actually-built-locks-live-in-two-places) |

Rung 1 partly substitutes for locking rather than building toward it:
optimistic concurrency is what most people are really after when they ask for
locks, and it costs no protocol state.

Rung 3 split in two once the code was in front of it. **3a** (abort) turned out
to be independent of lock decoupling and closed the interrupted-`PUT` data loss
on its own, so it shipped first. **3b** — a lock that can exist with no open
descriptor, carrying an owner and a timeout across requests — is the part rung 5
actually needs, and is still open. `open_write` currently mints the lock itself
and `close()`/`abort()` release it; a WebDAV `LOCK` needs `lock`/`unlock`/`renew`
as operations in their own right, plus an `open_write` that accepts an existing
token instead of always minting one.

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

Two of the four items here have since been built (rungs 2 and 3b); they are
kept, struck through, because what they cost is the useful part of the
estimate.

~~**A lock's lifetime is welded to a descriptor.**~~ **Done.** `lock`,
`unlock` and `renew_lock` are operations in their own right, and `open_write`
takes an optional existing token. A supplied lock outlives the descriptor —
`close()`/`abort()` release only a lock they minted — so `LOCK`, `PUT`, `PUT`,
`UNLOCK` across four requests holds throughout, which is pinned by conformance.
Renewal keeps the lock id, giving a stable WebDAV lock token, and a ttl'd lock
that is never renewed expires and is reclaimed by `prune`, so a client that
vanishes cannot wedge a node.

~~**Only the write paths check.**~~ **Done (ACC-11).** `move`, `remove`,
`remove_recursive` and `set_metadata` now consult the lock, with destruction
checked transitively over the subtree. `LockHeld` already mapped to `423`, so
`DELETE`, `MOVE` and `PROPPATCH` answer correctly today.

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

**An interrupted `PUT` is now atomic** (was not, in the first cut). A client
that vanishes mid-transfer leaves the previous content whole; the partial bytes
are never visible to any reader.

The fix was `Descriptor.abort()` plus commit-on-clean-exit/abort-on-exception
in `Descriptor.__exit__`, and the reason it could land without disturbing FUSE
is that `aloelite/fuse.py` never uses `with` on a writer — it parks the
descriptor in `self._open` and calls `close()` from `flush()`/`release()`. So a
POSIX write still commits whatever it wrote, partial or not, which is what
applications expect from a local filesystem; only the whole-file-transfer
callers (`cli.py` upload, `manager/api.py` upload, DAV `PUT`) abort. The commit
decision lives at the call site, which is what lets one `Descriptor` serve both
frontends. See `streaming.abort` in `config/mount-api.yaml` for the contract
the other three ports inherit.

**One non-atomic window remains, pinned by a test:**

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
