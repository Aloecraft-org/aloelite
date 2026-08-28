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

## 2. Verified since the RC bump

- **CI is green on the branch** — dispatched against
  `claude/aloelite-v0.4-status-gz58qx`, all five jobs pass (`lint`,
  `test` on 3.11 and 3.12, `sqlite-floor` at pinned 3.45.0,
  `sqlite-too-old` on Debian 12). Two things worth knowing: the lint job
  runs `ruff format --check` as well as `ruff check`, and **the FUSE tests
  genuinely run on GitHub runners** — `336 passed`, zero skips, so
  `doc/COMPATIBILITY.md`'s "verified in CI" claim is real rather than
  quietly skipped. Re-dispatch with `actions_run_trigger` (the workflow is
  `workflow_dispatch`-enabled) after any further push; feature branches
  get no automatic runs.
- **WAL fallback resolved** — see §4 below; `db.py` now probes the returned
  journal mode instead of trusting an exception.
- **Benchmarks written** — `script/benchmark.py` and `doc/BENCHMARKS.md`.
- **Manager HTTP contract extracted** — `manager/api-spec.yaml` plus
  `manager/test_api_spec.py`, which projects it onto the live Flask url_map
  and fails in both drift directions (both verified by deliberately
  introducing each). This is the artifact a second-language manager
  implements against; it ships in the wheel.

## 3. Work queue for the next session

In rough priority order; none blocks the others.

1. **Open the PR** for `claude/aloelite-v0.4-status-gz58qx` when the owner
   asks (do not open unasked), and handle review feedback. It is a large
   diff; the commit sequence is the review path — each commit is one
   coherent workstream with a full message.
2. **Benchmarks on real hardware.** The numbers in `doc/BENCHMARKS.md` were
   taken in a containerized VM whose throughput varies by multiples between
   runs; the doc says so and reports ranges. The *ratios* are sound, the
   absolute figures are not. Rerun `script/benchmark.py --fuse` on a real
   machine before quoting any absolute number in release material.
3. **Manager, remaining steps.** The HTTP contract is now extracted
   (`manager/api-spec.yaml`); what is left is optional and not urgent:
   splitting `api.py` (1,100 lines) into route modules per resource, and
   the repo extraction itself, which `manager/README.md` describes as
   mechanical once someone wants it. A useful smaller piece: the spec
   records status codes per route but nothing asserts the codes, only the
   route table — extending `test_api_spec.py` to check a few high-traffic
   responses against the spec would close that gap.
4. **Other unchecked PRAGMAs** (the generalization of the WAL finding in
   §4). `foreign_keys` was the obvious suspect and has been **checked — it
   is genuinely on and violations raise `IntegrityError`**, so that one is
   clear. `busy_timeout` remains unverified; it fails soft (a busy database
   raises instead of waiting) which is visible rather than silent, so it is
   low priority. Worth a sweep if anyone touches connection setup.

Owner-side (needs PyPI credentials, not automatable):
- Review the branch; publish `0.4.0rc1`.
- **Yank 0.3.1** — still not done, carried over from the last handoff.
- Release-note points: first open migrates era-1 files in place
  (additive columns + ms→ns rescale, crash-idempotent, no other table
  data touched); era-2 files refuse to open in older builds by design.

## 4. Traps discovered this session

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
- **`PRAGMA journal_mode` reports failure by RETURN VALUE, not by raising.**
  A mode sqlite cannot honor comes back as the unchanged mode with no
  exception (`:memory:` answers `'memory'`), so `db.py`'s old
  `try/except OperationalError` around the WAL request never fired on the
  case its own comment named. Same silent-failure family as
  `unixepoch('subsec')`. The rule this codebase keeps relearning: **probe
  the result, never the exception.** Worth a grep for other PRAGMAs whose
  success is assumed rather than checked.
- **A benchmark without cache control measures the page cache.** The first
  content benchmark showed raw writes at 185–3410 MB/s across runs because
  nothing was fsynced; encrypted reads appeared *faster* than plain ones.
  Fsync writes, `POSIX_FADV_DONTNEED` before reads, report medians with
  ranges — and on a shared/virtualized host, trust ratios over absolutes.

## 5. Properties to keep protecting

Unchanged from the previous handoff and still true: content-addressed
immutable chunks, host-side encryption, the conformance suite doing
double duty (now including mount-level semantics), and honest asterisks —
`doc/COMPATIBILITY.md` is now the canonical place they live. One addition:
**the id-minting contract is now conformance data** (`ids-v1.json`); any
port must pass it byte-for-byte before it ships, and changes to
`aloelite/ids.py` must update vectors and every port together.
