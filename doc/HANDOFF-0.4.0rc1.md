# Handoff: 0.4.0rc1 is on the branch

Written 2026-08-26 at the end of the session that produced the era-2 work,
commits `e6e7089..8fbd336` on `claude/aloelite-v0.4-status-gz58qx`. Audience
is whoever picks up next (human or Claude). Its predecessor,
`doc/HANDOFF-0.4.md`, is now historical: every item on its suggested-order
list is done except benchmarks, and its §5 open decisions are resolved and
recorded in `doc/DECISIONS.md` (D-1..D-5). Read DECISIONS.md first — the
code implements those five decisions and this document assumes them.

## 1. Where the tree stands

`pyproject.toml` says `0.4.0rc1`. 336 tests pass (including the real-kernel
FUSE suite and an era-1→era-2 migration fixture), ruff clean, wheel builds
with all package data verified, and `script/browser_check.py` passes in real
Chromium against the restructured manager.

What landed, one line each (commit messages carry the detail):

- **Host-minted ids + high-water fence** (D-1/D-2): `aloelite/ids.py`;
  `MAX(uuid7)` read-back and the single-owning-connection model are gone;
  watermark advances inside each write txn so the stored mark exactly covers
  committed ids.
- **Schema era 2** — one migration: ownership columns, ns timestamps (×1e6,
  crash-idempotent), PI-1 narrowed to containers (hardlinks), per-placement
  edge names, NODE-2 widened (symlink/fifo/socket; devices refused),
  mount access/principal, xattr table, minting triggers removed.
- **POSIX surface**: link/mknod/xattrs/chown/utimens end to end;
  `tests/test_posix_surface.py` pins the whole surface on a live mount,
  positives and clean-refusals both.
- **Mount policy** (D-4): ro mounts refuse all mutations; one rw mount per
  subtree by default (`allow_overlap` opts in — the manager's per-client
  sessions and admin-over-tenants both do); FUSE daemon holds a renewed
  5-minute ttl so a crashed daemon's row expires.
- **Docs**: REQUIREMENTS.md is era-2-current; `doc/COMPATIBILITY.md` is the
  tested compatibility table (every checkmark cites its CI test);
  `conformance/vectors/ids-v1.json` + 16 new scenarios in
  `conformance/scenarios/` (coherence, links, specials, ownership, policy).
- **Manager split**: `manager/api.py` (HTTP contract) / `manager/ui/`
  (language-agnostic asset bundle) / `manager/engine/` (Python adapters);
  boundary rules in `manager/README.md`. All git-mv, history intact.

## 2. Work queue for the next session

In rough priority order; none blocks the others.

1. **Verify CI on the branch.** Everything passes locally, including with
   `[fuse]`, but the four workflow jobs (`lint`, `test`, `sqlite-floor` at
   pinned 3.45.0, `sqlite-too-old` on Debian 12) have not been eyeballed on
   these commits. Era 2 introduces no new sqlite features (jsonb/subsec were
   already the floor), so surprises are unlikely — check anyway, fix
   anything red.
2. **Open the PR** for `claude/aloelite-v0.4-status-gz58qx` when the owner
   asks (do not open unasked), and handle review feedback. It is a large
   diff; the commit sequence is the review path — each commit is one
   coherent workstream with a full message.
3. **Benchmarks** (old handoff §8 item 7, the one unfinished item). Useful
   for release notes: cold/warm read and write throughput vs a plain
   directory, plus the single-query resolver vs the old per-segment fold on
   a deep path.
4. **Manager, next steps** (`manager/README.md` sketches these): extract
   the HTTP route inventory into a spec file the way `mount-api.yaml` did
   for the engine; consider splitting `api.py` (1,100 lines) into route
   modules per resource; the repo split itself stays mechanical and is not
   urgent.
5. **Candidate cleanup**: `db.py` falls back WAL→PERSIST when the backing
   file sits on an aloelite mount, but the mount now passes sqlite-WAL
   two-process tests (`test_sqlite_wal_concurrent_second_process`), so the
   fallback may be obsolete. Retest nested-aloelite specifically before
   removing it.

Owner-side (needs PyPI credentials, not automatable):
- Review the branch; publish `0.4.0rc1`.
- **Yank 0.3.1** — still not done, carried over from the last handoff.
- Release-note points: first open migrates era-1 files in place
  (additive columns + ms→ns rescale, crash-idempotent, no other table
  data touched); era-2 files refuse to open in older builds by design.

## 3. Traps discovered this session

- **The moment `link()` exists, git uses it** (object finalization tries
  link-then-unlink before falling back to rename). That instantly exposed
  `path_of` fanning out across a multi-parent node's ancestors. The fix
  chooses the newest placement per level with effective names; the git
  push test now exercises this permanently. Lesson: adding a syscall
  changes which code paths applications take — rerun the application-level
  tests, not just the new feature's tests.
- **Era migrations rerun after a crash** (stamp is written last). Every
  step must be idempotent: column-adds are guarded by `pragma_table_info`,
  the ms→ns rescale by a magnitude bound (`_NS_BOUND` — ms values stay
  below 1e15 until the year 33658). Keep this discipline for era 3.
- **YAML octal is not portable** — conformance scenarios write modes in
  decimal (416 = 0o640) because bare octal parses differently across YAML
  implementations. Same family as the `on:`-is-a-boolean trap already
  documented in the runner.
- **Fixtures that double-mount now need `allow_overlap=True`** (D-4
  default). If a new test hits `mount_conflict`, that is the admission
  policy working, not a bug.
- **sed across schema.sql is dangerous** — a broad timestamp substitution
  briefly rewrote expressions inside trigger bodies that happened to be
  deleted anyway. Prefer targeted edits on schema files.

## 4. Properties to keep protecting

Unchanged from the previous handoff and still true: content-addressed
immutable chunks, host-side encryption, the conformance suite doing
double duty (now including mount-level semantics), and honest asterisks —
`doc/COMPATIBILITY.md` is now the canonical place they live. One addition:
**the id-minting contract is now conformance data** (`ids-v1.json`); any
port must pass it byte-for-byte before it ships, and changes to
`aloelite/ids.py` must update vectors and every port together.
