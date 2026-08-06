# Handoff: 0.4 release candidate

Written 2026-08-06 at the end of the session that produced commits
`a3883b2..47ac8bd` on `claude/aloelite-sqlite-floor-fixes-2i5gr6`. Audience is
whoever (human or Claude Code) picks up the 0.4 work. It covers where the code
stands, what was decided and why, what is deliberately still open, and the
traps that cost real time this session.

Read `doc/REQUIREMENTS.md` and `doc/ENCRYPTION.md` first if you have not; this
document assumes their vocabulary (NODE-*, CV-*, ACC-*, ENC-*, PI-*).

---

## 1. Where the tree stands

Branch `claude/aloelite-sqlite-floor-fixes-2i5gr6`, 13 commits ahead of the
0.3.1rc4 tag point. 239 tests pass, ruff clean.

`pyproject.toml` still says `version = "0.3.1rc4"`. **It was never bumped.**
The 0.3.2 release (below) is unfinished business, not a completed step.

### What landed

**Engine — the 2026-08-03 sqlite incident.** Root cause was version-dependent
SQL: `unixepoch('subsec')` needs sqlite >= 3.42 and returns NULL (not an
error) below it, and `jsonb()` needs >= 3.45. On Debian 12's sqlite 3.40 that
produced a NOT NULL violation at mount and, worse, silently minted
zero-timestamped uuid7s.

- `aloelite/db.py` probes **both** capabilities at open (`MIN_SQLITE = (3, 45)`)
  and raises `errors.Unsupported` with an actionable message. Probe the
  capability, never the version string, and probe `'subsec'` explicitly — its
  failure mode is a silent NULL that no exception handler can catch.
- `aloelite/_sqlite.py` is the **single** sqlite import site (prefers
  `pysqlite3`, falls back to stdlib). This is correctness, not style:
  `pysqlite3.OperationalError is not sqlite3.OperationalError`, verified. Any
  new module that touches sqlite must import from the shim.
- `PRAGMA user_version` carries `SCHEMA_ERA` (currently 1). On open, an older
  or unstamped file has **every view and trigger dropped and recreated** from
  the current `schema.sql`; a newer-era file is refused. Tables are never
  touched. This ends the `CREATE ... IF NOT EXISTS` fossilization that shipped
  `'subsec'` triggers to hosts that could not run them.
- `created_at` is host-supplied through `create_mount`, `create_lock`,
  `create_volume`, `create_node`; templates `meta.version` is 2.
- NODE-2's type vocabulary moved from a table `CHECK` to the `node_guard_type`
  triggers, **specifically so the era refresh can widen it** without a
  per-file table rebuild. Remember this when adding types in era 2.
- Floors: `cryptography>=44`, `pydantic>=2`, plus a `bundled-sqlite` extra.

**Engine — FUSE coherence.** Two independent per-file-handle snapshots made
reads incoherent; together they broke `git push` onto a mount ("premature end
of pack file") because `index-pack` reads a pack back through a second fd
while still writing it.

- `Descriptor` used to freeze `(version, size)` at open. It now re-reads the
  committed pointer per `read()` and on END-relative `seek()`.
- `fuse.py`'s dirty-extent state moved from per-handle `_RwHandle` to a
  refcounted per-inode `_OpenFile` (`AloeFuse._files`).
- `forget()` implemented; the inode map was an unbounded leak on long mounts.
- `manager/direct.py` `detach()` snapshots and restores the connection cipher,
  because `ops.unmount` tears down the connection-wide cipher iff the closing
  mount is the connection's most recent one — with per-client mounts sharing
  one connection, detach order decided whether other clients kept working.

**Manager / web.** Text editor, paste bin, vendored Bootstrap + Alpine (no
CDN), `Cache-Control: no-store` on content, per-client sessions.

### Verification assets — use these, they earn their keep

- `tests/test_fuse_mount.py` — real kernel mounts, self-skips without
  `/dev/fuse` / `fusermount3` / pyfuse3. Includes a 120-object `git push`
  through `receive.unpackLimit` plus `git fsck --strict`.
- `script/browser_check.py` — drives the admin UI in real Chromium
  (Playwright) through the two-browser session scenario. **This caught two
  races in code that flask-client tests called green.** Run it after any UI
  change; see the header for invocation.
- CI jobs: `lint`, `test` (now with `[fuse]`), `sqlite-floor` (full suite at
  sqlite 3.45.0 exactly, via `LD_LIBRARY_PATH`), `sqlite-too-old` (Debian 12
  container; asserts the probe refuses, then that `bundled-sqlite` rescues it).

---

## 2. Finish 0.3.2 first

0.4 should not start until this ships. It is small and entirely owner-side.

1. Bump `pyproject.toml` to `0.3.2`. **Ship a final, not an rc.**
   `pip install aloelite` skips pre-releases, so a stale `0.3.1` final on PyPI
   would keep winning against any `0.3.2rcN`.
2. Publish, then **yank `0.3.1`** on PyPI. It predates the July calling-
   convention unification (its `aloelite-fuse` still takes positional
   `db volume mountpoint`) and version-sorts above every `0.3.1rc*`. Yanking
   keeps it installable when pinned exactly, which is the desired behavior.
3. Note in the release text that first open of an existing file performs the
   era-1 migration (triggers/views rewritten, `user_version` stamped). It
   touches no table data. Rollback is safe: pre-era builds ignore
   `user_version` entirely.

Owner action required: PyPI credentials.

Out of scope for this repo but tracked: `aloelite-rs` should enable rusqlite's
`bundled` feature; the pyalt/dart-node smoke test asserts
`sqlite_version_info >= (3,37)` and should move to the jsonb floor or run the
same probe.

---

## 3. What 0.4 is for

0.4 is the **schema era 2** release. Its purpose is to get breaking structural
changes done in one migration, before the language ports and alternate
backends multiply the cost of every future change.

Two goals drive the shape, and they are the owner's, not inferred:

1. **Native operation on other backends — Postgres first, SQL Server/Azure a
   bonus. MariaDB is explicitly dropped.** Not export/import: aloelite should
   *run* on Postgres so deployments inherit its replication, PITR, and HA.
2. **One product, two serializations.** Folding a file into Postgres,
   exporting a single volume back out as a file, failing over to a Postgres
   copy when a local file disappears, and mounting a large remote volume over
   a plain database connection should all just look like aloelite. The schema
   is the product; the `.sqlite` file and the Postgres database are two
   serializations of it. Do not build a "postgres mode" that reads as a
   separate product.

POSIX completeness is **not** a 0.4 goal. Of the four frontends (FUSE, Android
SAF/Kotlin, WebDAV, web manager) only FUSE consumes POSIX syscalls; the rest
consume the ops API. Keep the parts of POSIX that are schema-shaped and cheap
(they are needed for multitenancy anyway) and defer the syscall surface.

Multitenancy is a nice-to-have. A half-step is acceptable.

---

## 4. Era-2 work items

Land these as **one** migration bumping `SCHEMA_ERA` to 2. The era machinery
exists; do not spend five migrations on this.

### 4.1 Ownership and time columns (do regardless of anything else)

`ALTER TABLE ADD COLUMN` is cheap forever in sqlite; these are additive.

- `node.uid`, `node.gid`, `node.mode`. Today `mode` lives in the NODE-6
  metadata JSON as an octal string and uid/gid are faked from `os.getuid()` in
  `fuse.py:_attr`. **These same columns serve POSIX ownership and multitenant
  ownership** — that convergence is why they are worth doing now even though
  POSIX is deferred.
- Timestamps to **nanoseconds**, and add `atime`/`ctime`. The column type does
  not change (INTEGER holds ns fine); this is a value migration (× 1e6) plus a
  spec change, so it is cheap now and a coordinated cross-implementation
  migration later. `fuse.py:_attr` currently does `modified_at * 1_000_000`,
  losing ns at the source.
- `node.nlink`, and replace PI-1's partial unique index
  (`edge_active_placement ... WHERE archived = 0`) with a portable expression.
  **This single change serves both hardlinks and backend portability** —
  though note that dropping MariaDB removed the portability urgency, since
  Postgres and SQL Server both have filtered/partial indexes.

### 4.2 Single-query path resolution (highest-value non-schema item)

`aloelite/resolve.py` folds `resolution.resolve_segment` over path components:
**one query per segment.** A ten-deep path is ten round trips. Locally free;
over a network connection to Postgres it is the difference between usable and
unusable, and it is woven through every path-addressed operation.

Replace with a recursive CTE resolving a whole path in one query. There is
already a `recursive` template group. Works on all three backends, speeds up
local operation too, and doing it before the ports means nobody reimplements
the slow version. **If only one thing from this document gets done, make it
this one.**

### 4.3 Mount policy (cheap multitenancy half-step)

Subtree mounts already exist — `ops.mount(volume, at=...)` anchors at any
`mount_point` NodeId, which is the admin-sees-home-directories primitive.
What is missing is identity and policy:

- Access mode on the mount row (ro / rw). Roughly a day including enforcement
  in ops.
- Optional: principal/tenant column, for later ACLs and for policy multiwriter
  ("at most one rw mount per volume or subtree, unlimited ro") — admission
  control instead of consensus, which is the right trade for this system.

**Before shipping any of this, verify that path resolution cannot escape above
`mount_point` via `..`.** A grep found no `".."` handling in `resolve.py` or
`path.py`; that means the behavior is undetermined rather than proven safe.
This is the security boundary for multitenancy and it is currently unaudited.
Write the test first.

### 4.4 Backend portability groundwork

Not full Postgres support in 0.4 — groundwork so 0.5 is not another breaking
change.

- **Keep portable column types deliberately**: TEXT uuids, INTEGER epochs,
  BLOB. Not `uuid`/`timestamptz`/`bytea`. Slightly less efficient; "the same
  schema everywhere" is the property being sold.
- Metadata storage is the one place the dialects genuinely diverge: `jsonb` is
  native on Postgres, but SQL Server has only nvarchar + `JSON_VALUE`/
  `OPENJSON`. Decide the mapping once and write it down.
- Do not create the dialect variants or the `db.py` backend abstraction yet.
  Do avoid adding new sqlite-only constructs.

---

## 5. Open decisions — do not resolve these unilaterally

The owner explicitly wants to chew on these. Present options; do not pick.

**5.1 Host-minted vs SQL-minted ids.** Earlier advice in this session said
host-minted was *mandatory* for portability. **That advice was conditioned on
MariaDB**, which has no `INSTEAD OF` triggers and no partial indexes. With
Postgres + SQL Server only, both support `INSTEAD OF` triggers on views and
filtered indexes, so the insert-view idiom survives and this is now a genuine
choice. The honest case each way:

- *Host-minted*: one implementation instead of three dialects; id generation
  is exactly where silent divergence is catastrophic (the zero-timestamp uuid7
  bug came from SQL-side generation failing quietly); recent Postgres added a
  native `uuidv7()`, which would recreate the same version-floor trap that
  just cost a release. It also removes the `MAX(uuid7)` read-back in
  `db.create_monotonic`, **which is the sole reason for the single-owning-
  connection model** — a constraint that costs little on a local file and a
  lot when several app servers share a Postgres instance.
- *SQL-minted*: the schema stays self-defending, so raw external INSERTs still
  produce valid rows. Note this argument is weaker for the stated use case:
  folding a file into Postgres copies existing ids, it never generates new
  ones.

**5.2 Monotonic watermark vs stateless ids.** Arguably more consequential than
5.1. `volume.wm_ts`/`wm_seq` is a read-modify-write on one row per volume for
every node and edge creation. Free with a single writer; a hot row and a
serialization point on a shared Postgres backend (needs `SELECT ... FOR UPDATE`
or an advisory lock, and every create queues behind it). If ids become
host-minted the read-back reason disappears and only ordering remains, which
stateless uuid7s approximate. The two decisions are entangled; present them
together.

**5.3 Sequencing of POSIX syscall work** (mknod, xattrs, byte-range locks,
sparse/fallocate, mmap). Deferred, not cancelled. If it is ever revived, adopt
**pjdfstest** (~8,700 tests) as the scoreboard rather than treating "POSIX
compliance" as a vibe. Note that `mmap MAP_SHARED` is the deep one and it is
why `db.py` falls back WAL→PERSIST on an aloelite mount — i.e. "run sqlite on
aloelite" is currently rollback-journal-only.

---

## 6. Properties worth protecting

These are not accidents and future work should preserve them.

- **Content-addressed immutable chunks (CV-1/CV-2) make remote caching
  trivially correct** — a cache keyed by `chunk_hash` never needs
  invalidation. Combined with the ranged-read descriptor (which already
  fetches only the chunks a read touches), the "mount a huge remote volume
  without downloading it" story is closer than it looks. Do not introduce
  mutable chunks.
- **Encryption is host-side**, so a Postgres backend never sees plaintext. The
  remote/hosted case needs no new trust model.
- **The conformance suite is now doing double duty** — cross-language *and*
  cross-backend. Every scenario written in `conformance/scenarios/` is a test
  not rewritten for Postgres, SQL Server, Rust, or Kotlin. Mount-level
  semantics (the coherence guarantees from `tests/test_fuse_mount.py`) still
  live only in pytest and should be ported there.
- **Some caveats are load-bearing.** The single-writer model is what gives
  atomicity and copy-it-anywhere portability; convergent encryption leaking
  equality is a stated trade. The goal is zero *surprising* asterisks, not
  zero asterisks. A tested compatibility table ("verified in CI: git, sqlite
  in mode X, rsync, tar, editors; known-unsupported: mmap MAP_SHARED,
  byte-range locks") removes more adoption friction per hour than any further
  syscall work, and it is still unwritten.

---

## 7. Traps that cost time this session

- **The kernel page cache will lie to you.** The original FUSE bug reported a
  57344-byte short read; the daemon was actually returning **zero** bytes and
  the reader was being served its own writer's cached pages. Any mounted-FS
  assertion that must prove daemon behavior has to drop the cache first —
  `os.posix_fadvise(fd, 0, 0, POSIX_FADV_DONTNEED)`. Without it, tests pass
  while the code is broken.
- **Flask test clients are not browsers.** Per-client sessions passed every
  API test and were unusable in a real browser, because the browser was not
  reliably storing/sending the cookie. Sessions are now explicit bearer tokens
  (`X-Aloelite-Token`, localStorage) with the cookie as a compatibility
  duplicate. Verify UI changes with `script/browser_check.py`.
- **Bootstrap modal transitions are async.** Showing a modal while another is
  mid-hide stacks them (an empty explorer over the PIN field), and resolving a
  promise before `hidden.bs.modal` lets a fast next action overwrite the
  pending resolver, silently losing an operation. Both were real bugs found by
  driving the UI.
- Never assume a CI-green suite covers a layer it never loads. `libfuse3-dev`
  was installed for months while `[fuse]` never was, so `aloelite/fuse.py` had
  zero coverage — which is why the coherence bug reached production.

---

## 8. Suggested order

1. Ship 0.3.2 (§2). Owner-side; unblocks everything.
2. `..` escape audit + test (§4.3). Small, security-relevant, informs the
   mount-policy design.
3. Single-query path resolution (§4.2). Highest value, not schema-breaking,
   can land independently.
4. Get decisions on §5.1 and §5.2 from the owner.
5. Era-2 migration (§4.1, §4.3, plus whatever §5 resolves to) as one commit
   bumping `SCHEMA_ERA` to 2, with tests mirroring `tests/test_schema_era.py`.
6. Port mount-level semantics into `conformance/scenarios/`.
7. Compatibility table + benchmarks.
8. 0.4.0rc1.

Do not start 5 before 4. The whole point of era 2 is to break things once.
