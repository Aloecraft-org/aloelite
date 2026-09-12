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
- [Two implementations of one format](#two-implementations-of-one-format)
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
| frontend | `ext4`, `direct`, `fuse`, `rust-fuse`, `py-cli`, `rust-cli`, `gocryptfs`, `restic` | a kernel mount and a library call are not the same code path, and neither is a second implementation of either |
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

**There is no in-process Rust `direct` row.** The Rust engine is a library
(`rust/aloelite-core`) with no Python binding, so it cannot be called from
this harness the way `aloelite.aloelite.Mount` can. The two Rust frontends
that *are* measured are the ones a user runs: the FUSE daemon
(`rust-fuse`, compared against `fuse` over an identical workload) and the
CLI (`rust-cli`, compared against `py-cli` over the shared verb contract).
Where a `direct` comparison would be most interesting — raw engine
throughput with no kernel and no process in the way — read the `put_large`
and `get_large` CLI rows instead: at multi-MiB sizes the process startup is
a small and separately-reported part of the total.

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

## Two implementations of one format

`rust/` is a second implementation of the Mount API: same schema, same
chunk addressing, same key ladder, same verb contract
(`aloelite/config/cli.yaml`, which both parse from one table). That makes a
kind of comparison most projects cannot run — not "this filesystem against
that one", but **the same design, built twice**, with everything else held
constant.

The harness treats it as one more entry on the frontend axis rather than a
special case. `rust-fuse` is an entry in `corpus.FUSE_DAEMONS`, so every
suite already reports it beside `fuse`; nothing in any suite names a
daemon. Two comparisons do not fit that matrix and get their own suites:
`cli` (both binaries, verb for verb) and `interop` (one writes, the other
reads).

### The formats really are interchangeable

`interop` writes a volume with one implementation and reads it back with
the other, both directions and both volume modes, and compares the bytes.
Four of four round trips match. The row reports a mismatch *count*, so 0 is
the pass — a throughput figure would mean nothing if the bytes disagreed.

| writer -> reader | volume | bytes match |
|---|---|---|
| `py-cli` -> `rust-cli` | plain | yes |
| `py-cli` -> `rust-cli` | convergent | yes |
| `rust-cli` -> `py-cli` | plain | yes |
| `rust-cli` -> `py-cli` | convergent | yes |

Convergent is the interesting half: it means both implementations derive the
same volume key from the same PIN and produce byte-identical ciphertext
addresses, which is the ENC-2 ladder agreeing end to end.

### The CLI: startup is most of it

One process per operation, which is what a shell script pays. 64 MiB for the
bulk verbs; `above floor` subtracts that implementation's own startup, so it
is what the engine did.

| verb | `py-cli` | `rust-cli` | ratio |
|---|---:|---:|---:|
| `--version` (startup floor) | 174 ms | **1.5 ms** | **115x** |
| `ls` | 226 ms _(51 above floor)_ | 6.4 ms _(4.9)_ | 35x _(10x)_ |
| `stat` | 232 ms _(58)_ | 5.8 ms _(4.2)_ | 40x _(14x)_ |
| `mkdir -p` | 225 ms _(51)_ | 7.0 ms _(5.5)_ | 32x _(9x)_ |
| `put` 64 MiB | 163 MiB/s | **577 MiB/s** | 3.5x |
| `get` 64 MiB | 195 MiB/s | 385 MiB/s | 2.0x |

**Startup is most of a one-shot command.** For `ls`, `stat` and `mkdir` the
Python binary spends 174 of its ~228 ms before it has looked at the volume —
interpreter plus imports. The Rust binary spends 1.5 ms. That is a 35-40x
difference end to end and about a 10x difference in the engine underneath,
and it is why the floor row is reported rather than folded in: they are
different problems with different fixes.

**For bulk transfer the engine dominates and the gap narrows**, to 3.5x on
`put` and 2.0x on `get`. These are also the closest thing here to an engine
against engine number, since at 64 MiB the process startup is 1% of the
total.

The practical reading: a script that shells out per file pays Python's
startup per file, and there the Rust binary is worth two orders of
magnitude. A process that moves a lot of bytes in one call is paying for the
engine, and there it is worth 2-3x.


### The FUSE daemons: the same handlers, different costs

`rust-fuse` and `fuse` run the identical workload over the identical kernel
path, so their rows differ only by the daemon. Every suite reports both.

| measurement | `fuse` | `rust-fuse` | |
|---|---:|---:|---|
| sequential write, plain | 79 MiB/s | **97 MiB/s** | Rust 1.2x |
| sequential write, convergent | 63 MiB/s | 49 MiB/s | Python 1.3x |
| cold sequential read, plain | 167 MiB/s | 158 MiB/s | even |
| cold sequential read, convergent | 131 MiB/s | 82 MiB/s | Python 1.6x |
| **warm** sequential read, plain | **4,701 MiB/s** | 289 MiB/s | see below |
| create, 4 KiB files | 265 /s | **341 /s** | Rust 1.3x |
| cold `stat` | 1,834 /s | **4,305 /s** | Rust 2.3x |
| `unlink` | 747 /s | **1,650 /s** | Rust 2.2x |
| `readdir` | 42 /s | **50 /s** | Rust 1.2x |
| daemon peak RSS, 1 GiB stream | 56.0 MiB | **14.1 MiB** | Rust 4.0x smaller |

Three things to take from this:

- **Metadata operations are where the Rust daemon wins**: 2.2-2.3x on `stat`
  and `unlink`, which is the per-operation overhead of the runtime rather
  than anything about the format.
- **Memory is the clearest win**: a daemon that streams a gigabyte at
  14.1 MiB against 56.0 MiB. Both are bounded — that is the streaming claim
  holding in both — but the constant differs by 4x.
- **Bulk throughput is a wash, and encryption is not.** Writes and cold
  reads are within ~20% on plain volumes, but on convergent volumes the
  Python daemon is ahead (131 vs 82 MiB/s on cold reads). One run on one
  host; before drawing a conclusion from it, run `--suite throughput` on the
  hardware you care about.

The warm-read row is not a throughput difference. It is the next section.


### One difference worth fixing: the Rust daemon disables the page cache

The largest single gap between the daemons is not throughput, it is caching,
and it is one line.

`aloelite-fuse` answers `open` with `fuser::FopenFlags::empty()`
(`rust/aloelite-fuse/src/fs.rs`). Without `FOPEN_KEEP_CACHE` the kernel
invalidates a file's page cache on every open, so **no read through the Rust
mount is ever served from cache.** The Python daemon gets
`keep_cache = True`, which is pyfuse3's default, and keeps it.

Four consecutive reads of the same 16 MiB file, no cache dropped between
them:

| read | `fuse` | `rust-fuse` |
|---:|---:|---:|
| 1st | 280 MiB/s | 257 MiB/s |
| 2nd | 3,723 MiB/s | 284 MiB/s |
| 3rd | 5,036 MiB/s | 273 MiB/s |
| 4th | 5,360 MiB/s | 266 MiB/s |

Cold, the two daemons are within 10% of each other — the Rust engine is doing
its job. Warm, the Python daemon is **20x faster**, and the Rust one is flat
because every read is still a round trip to the daemon. That is why
`rust-fuse` warm rows in the throughput table look anomalously close to its
cold rows: there is no warm path.

This is a behavioural difference, not necessarily a bug — `KEEP_CACHE` is
the flag that makes a mount cache-coherent with itself but not with
out-of-band writers, and the reference daemon's own comments treat cache
coherence as a deliberate choice. But the reference chose to keep the cache
and the port did not, which looks like an inherited default rather than a
decision. Worth an explicit one either way.

(While confirming this: the comment at `aloelite/fuse.py:30` describes
`keep_cache=False` as "the pyfuse3 default". The default is `True`, which is
what the measurements show the Python daemon actually getting.)


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

**Surviving `kill -9`, in every frontend and both implementations.** The
durability suite kills the writing process — or, for a mount, the FUSE daemon
itself — at a random point during writes, then reopens the volume and
deep-verifies every file whose write had been confirmed. Across 48 rounds
covering **6,993 confirmed files, none was missing and none failed deep
verify**:

| frontend | rounds | files confirmed | lost or corrupt | reopen p50 |
|---|---:|---:|---:|---:|
| `direct`, plain | 12 | 2,485 | **0** | 140 ms |
| `direct`, convergent | 12 | 1,785 | **0** | 238 ms |
| `fuse`, plain | 12 | 1,203 | **0** | 92 ms |
| `rust-fuse`, plain | 12 | 1,520 | **0** | 110 ms |

Reopen includes a full deep verify of the volume the crash left behind.

**Maintenance that does not scale with the volume.** Unlock and `change_pin`
touch the key ladder and nothing else. Mounting an encrypted volume took
74 ms at 1 MiB and 84 ms at 64 MiB; `change_pin` took 134 ms and 127 ms. The
cost is Argon2id at the shipped parameters (t=3, m=64 MiB, p=4) and nothing
else. An unencrypted mount is 0.5 ms, which is the contrast that shows the
79 ms is all key derivation.

## What it costs

The engine is a content-addressed chunk pool inside a transactional database.
That is a real cost on the paths where a plain file would simply be a plain
file, and the ext4 baseline row in every table is there to price it. One
run, one 4-vCPU host, sqlite 3.45.1 in WAL at `synchronous=FULL`, 1 MiB
chunks, 64 MiB streamed, median of three rounds, every row under the same
durability barrier:

| | seq write | cold seq read | create (4 KiB files) | 4 KiB `pread` p50 |
|---|---:|---:|---:|---:|
| ext4 (baseline) | 188 MiB/s | 2,399 MiB/s | 3,314 /s | 0.05 ms |
| `direct`, plain | 104 MiB/s | 1,584 MiB/s | 1,509 /s | 0.39 ms |
| `direct`, convergent | 85 MiB/s | 617 MiB/s | 1,395 /s | 0.85 ms |
| `fuse`, plain | 79 MiB/s | 167 MiB/s | 265 /s | 0.93 ms |
| `fuse`, convergent | 63 MiB/s | 131 MiB/s | 259 /s | 1.41 ms |
| `rust-fuse`, plain | 97 MiB/s | 158 MiB/s | 341 /s | 0.79 ms |
| `rust-fuse`, convergent | 49 MiB/s | 82 MiB/s | 326 /s | 1.78 ms |

Read the columns, not the cells:

- **Sequential write** costs about 1.8x raw file I/O through the library and
  2-2.4x through a mount. **Encryption is the larger cost of the two** here:
  `direct` drops 104 -> 85 MiB/s, and the mounts drop further.
- **Small-file creates** cost ~2.2x ext4 through the library and ~10-12x
  through a mount. The engine pays a transaction per create; the mount pays
  that plus the kernel round trip.
- **Random reads pay a whole chunk.** A 4 KiB `pread` and a 64 KiB `pread`
  cost almost the same, because both fetch the 1 MiB chunk containing the
  offset and, on an encrypted volume, decrypt it. Effective bandwidth at
  4 KiB is therefore about a sixteenth of the bandwidth at 64 KiB. If the
  workload is `qemu-img` against a mounted image, **chunk size is the knob
  that matters** — and it is fixed per volume at creation (CV-1), so it is a
  decision, not a tuning.
- **Cold reads through `direct` flatter themselves** and the figures say so:
  1,584 MiB/s is close to the ext4 baseline because sqlite is returning
  pages the kernel had not been asked to forget. See the caveat on
  cold-cache symmetry above.
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

Both costs are now fixed, by two different changes. What follows is the
state after them, with ext4 on the same disk as the control; the arc that
got here is in the two subsections below.

**`stat` of one entry (p50) — flat**

| entries | ext4 | `direct` | `fuse` | `rust-fuse` |
|---:|---:|---:|---:|---:|
| 1,000 | 0.0053 ms | 0.051 ms | 0.31 ms | 0.102 ms |
| 5,000 | 0.0053 ms | 0.056 ms | 0.33 ms | 0.109 ms |
| 10,000 | 0.0054 ms | 0.063 ms | 0.19 ms | 0.112 ms |
| growth | **flat** | **flat** | flat | flat |

**One full `readdir` — linear**

| entries | ext4 | `direct` | `fuse` | `rust-fuse` |
|---:|---:|---:|---:|---:|
| 1,000 | 0.34 ms | 7.7 ms | 46 ms | 13.5 ms |
| 5,000 | 1.52 ms | 39 ms | 253 ms | 66 ms |
| 10,000 | 3.16 ms | 93 ms | 487 ms | 128 ms |
| µs per entry | 0.32 | **7.7 → 9.3** | 46 → 49 | 13.5 → 12.8 |

At 10,000 entries the library is now **12x ext4 on `stat`** and **29x on
`readdir`**. It was 1,170x and ~18,000x.

### `readdir`: a window, not a correlated subquery

NODE-5 makes the greatest `node_id` per effective name the visible one, and
`directory_listing` derived that per row with a correlated scalar subquery —
for every row, re-read the container's whole child set and take the max.
That is the O(N²). As a window it is one pass:

```sql
max(n.node_id) OVER (PARTITION BY ae.from_id, ae.name)
```

| one `readdir` | before | after | |
|---|---:|---:|---|
| 1,000 entries | 375 ms | 7.7 ms | 49x |
| 5,000 entries | 10.2 s | 39 ms | 262x |
| 10,000 entries | 51.0 s | 93 ms | **550x** |

Output is byte-identical — adopted against a row-for-row differential
covering hardlinked placements carrying per-placement names (D-5) and hidden
same-name siblings, the two cases `visible` exists for. A view is not an
index, so this costs writes nothing.

### `stat`: era 3 materialises `edge.name`

The lookup predicate was `edge.from_id = ? AND coalesce(edge.name,
node.name) = ?` — spanning two tables, which no index can serve, so finding
a child by name scanned the container.

**An index on `node (name)` does not fix this**, and was measured before
being discarded. On a 3,000-entry directory:

| | `stat` | `readdir` | plan |
|---|---:|---:|---|
| no index | 1.53 ms | 3,448 ms | `SEARCH edge USING edge_from_active` |
| `node (name)` | 1.54 ms | 3,429 ms | identical — **the planner ignores it** |
| `node (name)` + `ANALYZE` | 2.57 ms | 2,688 ms | `SCAN edge` — a full table scan |

Without `ANALYZE` it is never chosen, so it would have cost write throughput
and bought nothing; with `ANALYZE` it is chosen and picks a worse shape.

What does fix it is removing the `coalesce` — era 2 introduced `edge.name`
as a D-5 *override*, where NULL meant "use the node's name". **Era 3 makes
it always hold the placement's name.** `create_edge` materialises the node's
name when no override is given, which moves that coalesce from every read to
one write, and `edge (from_id, name, to_id) WHERE archived = 0` then answers
a lookup from the index alone:

```
SEARCH edge USING COVERING INDEX edge_from_name (from_id=? AND name=?)
```

| `stat` p50 | before | after | |
|---|---:|---:|---|
| 1,000 entries | 0.54 ms | 0.051 ms | 11x |
| 5,000 entries | 3.04 ms | 0.056 ms | 54x |
| 10,000 entries | 6.32 ms | 0.063 ms | **100x** |

The era-3 migration backfills existing rows, an `edge_guard_name` trigger
keeps the invariant, and a genuine D-5 override is left alone. The index's
write cost did not show above run-to-run noise: `create_ops` measured
1,618/s against 1,470/s before, and the ingest curve 3,064–3,341 rows/s
against 2,952–3,206. One extra B-tree insert per edge is small next to the
transaction it rides in.

### What is left

The remaining ~12x on `stat` and ~29x on `readdir` is the cost of resolving
a path through SQL rather than through an in-kernel dentry cache, and it no
longer grows with directory size. Directories in the tens of thousands are
fine now; the reason to keep them smaller is ordinary taste, not a curve.

The harness will not sit through the worst of these. `dir_scale` fits the
growth exponent from the last two measurements and skips a size it projects
past a 120-second budget, recording the projection and the fitted exponent,
so a skipped row never reads as a fast one. It is what caught the per-call
re-listing: the FUSE columns were pulling away from the library column
faster than the library column grew, which a single size could not have
shown.


**Maintenance while tail latency matters.** Export and snapshot are both
fast — 256 MiB exported in 1.9 s, snapshotted in 0.65 s — and neither moved a
concurrent reader's p50 measurably (0.56 ms against a 0.52 ms control). The
tail is a different matter, and it is not uniform:

| maintenance op | foreground p50 | foreground p99 vs control |
|---|---:|---:|
| export to another file, plain | 0.56 ms | 1.27x |
| snapshot within the same file, plain | 0.71 ms | **23x** |
| export, convergent | 0.96 ms | 1.20x |
| snapshot, convergent | 0.98 ms | 1.11x |

The shape that makes sense of this is that a snapshot writes into the *same*
file it is reading, so it contends for the one write lock a WAL database has,
while an export writes elsewhere. But the convergent snapshot did not
reproduce the spike, and each cell here is a single observation — so read the
23x as "the tail can move by an order of magnitude or more", not as a
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
