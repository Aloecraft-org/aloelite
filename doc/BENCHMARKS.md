# Benchmarks

<div align="center">

<img src="https://raw.githubusercontent.com/Aloecraft-org/aloelite/refs/heads/main/doc/aloelite.png" style="height:96px; width:96px;"/>

**Aloelite SQLite Filesystem**

[Overview](/README.md) | [Getting Started](/doc/GETTING_STARTED.md) | [Frequently Asked Questions](/doc/FAQ.md)

[Troubleshooting](/doc/TROUBLESHOOTING.md) | [Requirements Spec](/doc/REQUIREMENTS.md) | [Encryption Spec](/doc/ENCRYPTION.md) | **Benchmarks (This Document)** | [Roadmap](/doc/ROADMAP.md)
</div>

This document is about **what aloelite is good for**, argued from measurements
rather than from the design. The harness is `bench/`; CI runs it on every push
and publishes the numbers to the job summary and as an artifact.

```bash
python -m bench                                  # core suites, ci scale
python -m bench --group all --scale ci           # everything a runner can do
python -m bench --group all --scale full         # the real-host profile
python -m bench --suite dedup --scale smoke      # one suite, seconds
python -m bench.report bench-results/results.json   # json -> markdown
```

## Contents

- [Read this before the numbers](#read-this-before-the-numbers)
- [What is not measured, and why](#what-is-not-measured-and-why)
- [What aloelite is good for](#what-aloelite-is-good-for)
- [What it costs](#what-it-costs)
- [What to avoid](#what-to-avoid)
- [The measured rows](#the-measured-rows)
- [Method](#method)
- [Adding a suite](#adding-a-suite)

## Read this before the numbers

**Nothing in the harness asserts, and the CI job never fails on a slow
number.** Throughput is a property of the host; a benchmark that failed a
build would be reporting on the runner. What *does* fail the job is a suite
that crashes, because that is a bug in the benchmark or in the engine.

Absolute figures from a shared runner are not reproducible. On the host the
figures below were taken on, the ext4 baseline's own write throughput varied
by 1.6x *within* a single three-round measurement, and its median moved by
1.5x between runs. **What survives that noise is the ratio between rows
measured in the same run**, since they share the machine and the moment. Every throughput row therefore carries
its own min/max alongside the median, and every latency row carries p99 and
the sample count. Treat the shapes as the finding and rerun on your own
hardware for absolutes.

Every row is labelled with four things, and they are never collapsed:

| label | values | why it cannot be averaged away |
|---|---|---|
| frontend | `ext4`, `direct`, `fuse`, `gocryptfs`, `restic` | a kernel mount and a library call are not the same code path |
| volume | `plain`, `convergent`, `random` | encryption changes dedup, memory, and read cost |
| cache | `cold`, `warm` | a warm read reports the page cache's speed, not the filesystem's |
| n | sample count | a p99 over 200 samples is not a p99 |

Each run also records sqlite's version and source, `journal_mode`,
`synchronous`, `busy_timeout`, page size, chunk size, the CPU, and the
filesystem the scratch directory is on. A run whose scratch directory is a
tmpfs is measuring memory, which is why the harness records that too.

## What is not measured, and why

Some of these are runner limits and some are limits of the tree. Saying which
is which is the point of this section.

**There is no Rust frontend to measure.** The engine is Python, and nothing
in this tree implements the Mount API in another language. A port has been
assessed (on `claude/aloelite-rust-port-assessment-732bo0`) but none exists
here, so a "Rust direct" row would be fabricated rather than measured. The
frontend axis is therefore `direct` (the library API, in process) and `fuse`
(a real kernel mount), with `ext4` on the same disk as the baseline that
separates aloelite's overhead from the hardware's.

That is a gap in the tree, not in the harness. The matrix lives in one place
— `bench/corpus.py::backends_for` — and a third frontend is a third branch
there; every suite picks it up without changing, because suites iterate the
matrix and never name a frontend. The `conformance/` suite is built on the
same principle for correctness (`doc/HANDOFF-0.4.md`: its scenarios are
written so as not to be "rewritten for Postgres, SQL Server, Rust, or
Kotlin"), and this is the performance counterpart of that.

**100 GB streams need a real host.** A hosted runner has roughly 25 GB of
writable disk. `--scale full` sets the streaming size to 100 GiB; `--scale
ci` uses 1 GiB. The claim under test is that peak RSS does not depend on how
long the stream is, and a flat line from 64 MiB to 1 GiB is evidence for it —
but only the `full` profile settles the 100 GB case.

**Million-entry directories are projected, not measured.** `readdir` is
superlinear (see [What to avoid](#what-to-avoid)), so a listing of a
1,000,000-entry directory projects to days. `dir_scale` fits the growth
exponent from its last two measurements and records a skip carrying the
projection and the exponent, rather than hanging the run. Single-entry
lookups are cheap enough to measure at every size, and are — they are not
flat either, which is the other half of that finding.

**Cold cache through FUSE is only half cold.** `posix_fadvise(DONTNEED)`
evicts the kernel's pages for a file. It cannot reach the sqlite page cache
*inside* the FUSE daemon, which is a separate process. The `direct` rows
additionally get `PRAGMA shrink_memory`, which can. So a cold FUSE read is
taken with a warm engine cache behind it, and that asymmetry favours the FUSE
rows.

**SQLITE_BUSY retries inside the timeout are invisible.** The engine sets
`busy_timeout = 5000`, and Python's `sqlite3` exposes no busy-handler
callback, so retries sqlite absorbs cannot be counted from here. They show up
as latency in the throughput rows. The number the concurrency suite reports
is operations *refused* after the timeout expired — a much rarer and more
serious event.

**Kopia is not a comparator.** It is not packaged in Ubuntu, and a
benchmark that installs a binary from a vendor URL is a supply-chain
decision, not a measurement. `restic` covers the ingest-and-dedup category
and `gocryptfs` covers encrypted FUSE throughput; both come from apt.

## What aloelite is good for

One run, one 4-vCPU host, 64 MiB corpora. These are the rows that answer the
question in the heading.

| | plain | convergent | random |
|---|---:|---:|---:|
| on disk / logical, unique data | 1.004 | 1.004 | 1.004 |
| on disk / logical, 8 clones of one template | **0.128** | **0.128** | 1.004 |
| on disk / logical, 5%-live sparse image | **0.049** | **0.049** | 1.004 |
| dedup within one volume | 1.000 | 1.000 | 0.000 |
| dedup across two volumes in one file | 1.000 | **0.000** | 0.000 |

**Data that repeats.** The content pool is addressed over the ciphertext
actually stored, so identical bytes cost once. Eight entries holding one
golden image — a VM image tree, a repeated backup — land at 0.128, which is
the 1/8 floor plus sqlite's page overhead. A mostly-zero sparse image lands
at 0.049, because every zero chunk in the file is one pool row. Unique
incompressible data gets no benefit and costs 0.4% over its logical size.
For scale: `restic`, a dedicated dedup-and-encrypt backup tool, stored the
same cloned corpus at 0.125.

**`random` mode really does give up dedup.** Every ratio in its column is
the no-dedup number. That is the feature working — it buys zero equality
leakage — and it is worth seeing priced.

**Per-volume key separation, confirmed rather than asserted.** Two *plain*
volumes in one file share chunks: the pool is one table and a plain volume's
"ciphertext" is its plaintext. Two *encrypted* volumes holding byte-identical
content share **nothing** — the row reads 0.000 — because the chunk cipher is
domain-separated by volume id, so identical plaintext cannot produce
identical addresses. This is the property `doc/ENCRYPTION.md` claims, and
`dedup_across_volumes` is where it gets measured.

**Cheap versions of large files.** `write_range` carries every untouched
chunk into the new version by reference, so an edit costs the chunk it
touched, not the file it was in. Measured on a 64 MiB file with 1 MiB
chunks: a 1-byte edit adds 1.07 MB (1.02 chunks), a 4 KiB edit adds the same,
and a 1 MiB edit straddling a boundary adds 2.11 MB (2.02 chunks). The floor
is the chunk, and it does not move with file size.

**Bounded memory, whatever the stream length.** Peak RSS of a child process
streaming 1 GiB: 67.6 MiB against a 40.7 MiB floor of interpreter and
imports — 27 MiB of working set. At 64 MiB streamed the same measurement was
21 MiB. Sixteen times the data cost 6 MiB. On a convergent volume the floor
is 105 MiB, because Argon2id's `memory_cost` is 64 MiB, and streaming 1 GiB
on top of it moved RSS by **0.3 MiB**.

**Surviving `kill -9`.** The durability suite kills the writing process — or,
for the mount, the FUSE daemon itself — at a random point during writes, then
reopens the volume and deep-verifies every file whose write had been
confirmed. Across 36 rounds (12 each for `direct`/plain, `direct`/convergent
and `fuse`/plain) covering **5,370 confirmed files, none was missing and none
failed deep verify**. Reopening a volume killed mid-write took 129 ms (plain),
194 ms (convergent) and 69 ms (through a fresh mount) at p50, deep verify
included.

**Maintenance that does not scale with the volume.** Unlock and `change_pin`
touch the key ladder and nothing else. Mounting an encrypted volume took
74 ms at 1 MiB and 84 ms at 64 MiB; `change_pin` took 134 ms and 127 ms. The
cost is Argon2id at the shipped parameters (t=3, m=64 MiB, p=4) and nothing
else. An unencrypted mount is 0.37 ms, which is the contrast that shows the
74 ms is all key derivation.

## What it costs

The engine is a content-addressed chunk pool inside a transactional database.
That is a real cost on the paths where a plain file would simply be a plain
file, and the ext4 baseline row in every table is there to price it. One
run, one 4-vCPU host, 64 MiB streamed, median of three rounds, every row
under the same durability barrier:

| | seq write | cold seq read | create (4 KiB files) | 4 KiB `pread` p50 |
|---|---:|---:|---:|---:|
| ext4 (baseline) | 157 MiB/s | 479 MiB/s | 3,374 /s | 0.057 ms |
| `direct`, plain | 84 MiB/s | 1,196 MiB/s | 758 /s | 0.41 ms |
| `direct`, convergent | 77 MiB/s | 744 MiB/s | 765 /s | 0.84 ms |
| `fuse`, plain | 60 MiB/s | 181 MiB/s | 264 /s | 0.95 ms |
| `fuse`, convergent | 57 MiB/s | 103 MiB/s | 252 /s | 2.0 ms |
| gocryptfs (comparator) | 190 MiB/s | 1,070 MiB/s | — | — |

Read the columns, not the cells:

- **Sequential write** costs about 2x raw file I/O through the library and
  about 2.6x through a mount. **Encryption is close to free on top of that**
  — 84 to 77 MiB/s — because the cost is dominated by chunking and sqlite,
  not by ChaCha20-Poly1305. gocryptfs, which writes ordinary files into
  ext4, is faster than either; that gap is the database, not the crypto.
- **Small-file creates** cost ~4.5x ext4 through the library and ~13x
  through the mount. The engine pays a transaction per create; the mount
  pays that plus the kernel round trip.
- **Random reads pay a whole chunk.** A 4 KiB `pread` and a 64 KiB `pread`
  cost almost the same (0.41 ms vs 0.43 ms on `direct`/plain), because both
  fetch the 1 MiB chunk containing the offset and, on an encrypted volume,
  decrypt it. Effective bandwidth at 4 KiB is therefore about a sixteenth of
  the bandwidth at 64 KiB: 8 MiB/s against 119 MiB/s. If the workload is
  `qemu-img` against a mounted image, **chunk size is the knob that
  matters** — and it is fixed per volume at creation (CV-1), so it is a
  decision, not a tuning.
- **Cold reads through `direct` flatter themselves** and the figures say so:
  1,196 MiB/s is above the ext4 baseline because sqlite is returning pages
  the kernel had not been asked to forget. See the caveat on cold-cache
  symmetry above.
- **Unlocking an encrypted volume costs ~64 MiB of RSS**, because that is
  Argon2id's `memory_cost`. The `peak_rss_baseline` row exists so that this
  is subtracted rather than blamed on the streaming path.

## What to avoid

**Large directories.** Both `readdir` and path lookup degrade with the number
of entries in a directory, and they share one root cause: **nothing in the
schema can turn "the child of container C named X" into an index seek.**
`name` lives on the `node` table and `from_id` lives on `edge`, and an index
cannot span two tables. Every name resolution therefore seeks
`edge_from_active` on `from_id` alone — which yields *all* of the container's
children — then does a primary-key lookup on `node` for each one and filters
by name:

```sql
-- resolution.resolve_segment, and the same shape inside resolve_path's CTE
SELECT n.node_id, n.type
FROM active_edge ae JOIN node n ON n.node_id = ae.to_id
WHERE ae.from_id = :container AND n.name = :name
ORDER BY n.node_id DESC LIMIT 1
```

`EXPLAIN QUERY PLAN`: `SEARCH edge USING INDEX edge_from_active (from_id=?)`,
then `SEARCH n USING INDEX sqlite_autoindex_node_1 (node_id=?)`. So a single
`stat` costs **O(entries in the containing directory)**.

`readdir` pays that cost once per row. The `directory_listing` view resolves
NODE-5 visibility (greatest `node_id` per name wins) with a correlated scalar
subquery:

```sql
(n.node_id = (
   SELECT max(n2.node_id)
   FROM active_edge ae2 JOIN node n2 ON n2.node_id = ae2.to_id
   WHERE ae2.from_id = ae.from_id AND n2.name = n.name
)) AS visible
```

`EXPLAIN QUERY PLAN` shows it as `CORRELATED SCALAR SUBQUERY` over the same
`(from_id)`-only index, so N rows each rescan N siblings: **O(N²)**.

Measured on a 4-vCPU CI-class host, one directory per size:

| entries in one directory | `stat` one entry (p50) | `readdir` via the library | `readdir` through a FUSE mount |
|---:|---:|---:|---:|
| 100 | 0.07 ms | 3 ms | — |
| 1,000 | 0.43 ms | 0.23 s | 2.2 s |
| 5,000 | 1.98 ms | 7.7 s | **211 s** |
| 10,000 | 5.4 ms | 39.9 s | not run (projected ~840 s) |

Ten thousand entries is a mail spool, a `node_modules`, or a month of daily
files — not an abusive case.

**Through the mount it is worse again, and for a second, separate reason.**
`AloeFuse.readdir(inode, start, token)` in `aloelite/fuse.py` calls
`self.m.list(...)` — the whole O(N²) listing — and then skips the first
`start` entries. The kernel calls `readdir` repeatedly with a rising `start`
until the directory is exhausted, because one reply buffer holds only so many
entries, so the full listing is recomputed on every continuation call. That
is why the FUSE-to-library ratio is not a constant: 9.5x at 1,000 entries,
27x at 5,000. This one is fixable without touching the schema — cache the
listing for the life of the open directory handle instead of rebuilding it
per call.

The harness will not sit through the worst of these. `dir_scale` fits the
growth exponent from the last two measurements and skips a size it projects
past a 120-second budget, recording the projection and the fitted exponent,
so a skipped row never reads as a fast one.

Two candidate fixes, neither of them this change's to make: an index on
`node (name)`, which lets the planner drive the join from the name side
instead (additive, no era bump); or denormalising the name onto the edge,
which 0.4 already plans for another reason (D-5 makes `edge.name` a
per-placement override) and which would make `edge (from_id, name)` a real
covering index. Which is right is a schema question, and it belongs with the
era-2 work rather than with a benchmark.

**Maintenance while tail latency matters.** Export and snapshot are both
fast — 256 MiB exported in 2.0 s, snapshotted in 0.64 s — and neither moved a
concurrent reader's p50 measurably (0.57 ms against a 0.57 ms control). The
tail is a different matter, and it is not uniform:

| maintenance op | foreground p50 | foreground p99 vs control |
|---|---:|---:|
| export to another file, plain | 0.58 ms | 1.25x |
| snapshot within the same file, plain | 0.83 ms | **9.5x** |
| export, convergent | 1.00 ms | 1.04x |
| snapshot, convergent | 1.01 ms | 1.04x |

The shape that makes sense of this is that a snapshot writes into the *same*
file it is reading, so it contends for the one write lock a WAL database has,
while an export writes elsewhere. But the convergent snapshot did not
reproduce the spike, and each cell here is a single observation — so read the
9.5x as "the tail can move by most of an order of magnitude", not as a
constant. If a snapshot's tail matters to you, measure it on your data:
`python -m bench --suite transfer`.

Either way the p50 rows say the volume stays usable throughout, and no
foreground *read* was refused during any maintenance op: `busy_errors` was 0
in all four. Writers are a different story — see below.

**Counting on more than one writer.** SQLite gives a WAL database one
writer, and the numbers show what that costs. Three readers and one writer
on one file, 10 seconds, separate processes with their own connections:

| | alone | contended | |
|---|---:|---:|---|
| readers (aggregate of 3) | 377 MiB/s | 541 MiB/s | scaling efficiency **0.48** |
| writer | 45 MiB/s | 30 MiB/s | **0.67** of its solo rate |

Three readers reach 1.4x what one reader does, not 3x. The writer loses a
third of its rate, and — unlike the readers — it is sometimes refused
outright: **14 writes hit SQLITE_BUSY after the full 5-second
`busy_timeout`** in that ten-second window, against 0 for the readers. Reads
scale poorly but never fail; writes degrade *and* need a retry policy.
Anything that writes concurrently must handle `OperationalError`, not just
tolerate latency.

**Assuming a prune shrank the file.** Prune frees pages *inside* the
database; the file does not shrink until `VACUUM` rewrites it. Measured on a
volume churned through four versions of 64 MiB: prune freed 202 MB of pages,
and the file stayed at 269 MB until `VACUUM` (399 ms) brought it to 68 MB.
The suite reports the two separately for exactly this reason.

## The measured rows

The tables are generated, not written: `bench/report.py` renders
`results.json`, CI attaches it to the job summary and uploads it as an
artifact, and a local run can produce the same file with `--markdown`. They
are not committed, because a committed table is a number that stops being
true without anything failing.

To read them against your own hardware:

```bash
pip install -e .[fuse]
sudo apt-get install -y gocryptfs restic     # optional comparators
python -m bench --group all --scale ci --markdown BENCHMARKS-local.md
```

## Method

**The durability barrier is the same for every row.** ext4's buffered write
and the engine's WAL commit are not the same promise, and timing one against
the other would flatter whichever side was allowed to lie. Every backend
implements `durable(path)` — `fsync` on the file and its directory for a
POSIX backend, `wal_checkpoint(TRUNCATE)` plus `fsync` for the engine — and
every throughput row includes it inside the timed region. "Written" means on
the platter, identically, everywhere.

**Cold means cold, and warm is deliberate.** Reads labelled `cold` drop the
kernel page cache for every backing file first, which for a FUSE row means
both the mounted inode and the `.fs` file behind it. Reads labelled `warm`
pre-read the bytes on purpose. Metadata measurements take the cold pass
*first*, because the first pass is what populates the kernel's attribute and
dentry caches and `posix_fadvise` cannot evict those.

**Corpora are never reused across rounds.** Writing the same bytes twice into
a convergent volume deduplicates, so a second round would measure the speed
of storing nothing. Each round uses a fresh seed and a fresh path.

**Peak RSS is measured in a child process.** `ru_maxrss` is a high-water mark
that never comes back down, so measured in the parent it would report the
largest thing the benchmark ever did. Each phase runs as `python -m
bench.suites.scale <phase>` and reports its own. The FUSE daemon's memory
comes from its `VmHWM` in `/proc`, since the writer's RSS says nothing about
the daemon's.

**Concurrency uses processes, not threads.** The question is what sqlite's
locking does; threads in one interpreter would add the GIL's answer to it.
Each worker owns its own connection, which is the shape WAL supports and the
shape a manager process has. Workers retry a busy mount with backoff and
report the retry count, because a worker that died at mount time would report
zero throughput and call it contention.

**A file is "confirmed" only after the engine said so.** The crash writer
appends to a journal *after* the write commits and fsyncs the journal before
continuing. A crash between the two loses a journal line for a file that
exists, which is harmless. The reverse — a journal line for a file that is
gone — is the failure the suite counts, and the volume is created before the
crash window so that a round measures content writes rather than bootstrap.

## Adding a suite

1. Write `def my_suite(run, scratch, cfg)` in a module under `bench/suites/`.
   Take every size from `cfg`; a hard-coded size makes the suite unrunnable
   at another scale.
2. `run.add(Row(...))` per measurement, with `frontend`, `volume` and `cache`
   filled in. `run.skip(name, why)` when something is unavailable — a suite
   that silently produces fewer rows is worse than one that says why.
3. Register it in `bench/suites/__init__.py::SUITES` and put it in a group.
4. Give it a heading in `bench/report.py::SECTIONS`, and list the `detail`
   keys worth a column in `DETAIL_COLUMNS`. An unregistered suite still
   renders, under its own name, at the end.
