# Benchmarks

<div align="center">

<img src="https://raw.githubusercontent.com/Aloecraft-org/aloelite/refs/heads/main/doc/aloelite.png" style="height:96px; width:96px;"/>

**Aloelite Single-File Filesystem**

[Overview](/README.md) | [Getting Started](/doc/GETTING_STARTED.md) | [Compatibility](/doc/COMPATIBILITY.md) | [Roadmap](/doc/ROADMAP.md)
</div>

Produced by `script/benchmark.py` (`--fuse` adds the mount section). Nothing
here is a test — none of it asserts, because throughput is a property of the
machine, not of the code.

## Read the caveat before the numbers

These were taken in a **containerized VM with a shared host cache**, and the
absolute throughput figures there are not reproducible: across runs, raw
fsynced writes ranged 172–650 MB/s and cached reads 1221–1500 MB/s. Every
throughput row therefore reports a **median with its min–max range**, and the
ranges are wide on purpose — hiding them behind a single figure would be the
misleading choice.

**What survives the noise is the ratio between rows measured in the same
run**, because they share the machine and the moment. Treat the shapes below
as the finding ("the engine costs roughly 3–5× raw file I/O on write") and
rerun on your own hardware for absolute numbers. Methodology, since it
decides whether a number means anything: writes are fsynced, and the page
cache is dropped (`POSIX_FADV_DONTNEED`) before every read — otherwise a read
benchmark reports the page cache's speed and calls it the filesystem's.

## Path resolution: the single-query CTE vs the per-segment fold

The claim from `doc/HANDOFF-0.4.md` §4.2 — *"if only one thing from this
document gets done, make it this one."* The era-2 resolver walks a whole path
in one recursive CTE; the previous one issued a query per segment. Times are
best-of-5 per depth, resolver against resolver (timing `stat()` would add
mount validation and a `get_node` to one side only).

| depth | CTE | per-segment fold | local speedup | CTE @1 ms RTT | fold @1 ms RTT | networked speedup |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 15 µs | 7 µs | **0.46×** | 1.0 ms | 1.0 ms | 0.99× |
| 4 | 21 µs | 22 µs | 1.07× | 1.0 ms | 4.0 ms | **3.9×** |
| 8 | 32 µs | 45 µs | 1.40× | 1.0 ms | 8.0 ms | **7.8×** |
| 16 | 59 µs | 90 µs | 1.53× | 1.1 ms | 16.1 ms | **15.2×** |

Two honest readings:

- **A single-segment lookup is faster with the fold** (0.46×) — the CTE has
  setup cost that one child lookup does not repay. This is why
  `resolution.resolve_segment` survives in the templates as the documented
  single-step primitive, and why its note says *"do NOT fold it over a
  path"*. The benchmark confirms the split rather than contradicting it.
- **Locally the win is real but modest** (1.4–1.5× at realistic depths). The
  decisive column is the networked one: the fold pays a round trip per
  segment, so its cost grows with depth while the CTE's does not. At a
  1 ms RTT — a modest number for a remote Postgres — a 16-deep path is
  **15× cheaper**, and that gap widens linearly with both depth and latency.
  Path resolution is woven through every path-addressed operation, so this
  is the difference between a usable remote backend and an unusable one.

## Content throughput

32 MB of incompressible random bytes, median of 5 rounds (range in parens).

| path | write | read |
|---|---|---|
| raw file (fsynced) | 649 MB/s (172–650) | 1457 MB/s (1221–1500) |
| engine, unencrypted | 130 MB/s (88–144) | 445 MB/s (257–683) |
| engine, convergent encryption | 125 MB/s (121–134) | 377 MB/s (186–451) |

The shape: the engine costs roughly **4–5× raw on write** and **3× on read**
for whole-file operations — the price of chunking into a content-addressed
pool inside a transactional database rather than appending to a file.
**Convergent encryption is close to free on write** (≈4% here, within the
noise) because the cost is dominated by chunking and sqlite, not by
ChaCha20-Poly1305; on read it costs ≈15%.

## Dedup

The same 32 MB written to two entries in a convergent volume:

| | |
|---|---|
| first write | 0.24 s, file grows to 32.2 MB |
| second write | 0.08 s, file grows by **0.000 MB** |
| pool rows | 32 — one copy of the bytes, two entries referencing it |

Convergent addressing (CV-1/CV-2) means the second write stores nothing and
costs a third of the first. This is also the property that makes remote
caching trivially correct: a cache keyed by `chunk_hash` never needs
invalidation.

## Through a kernel mount

32 MB written and read through a real FUSE mount versus a plain directory on
the same disk, page cache dropped before the read:

| | write | read |
|---|---|---|
| plain directory | 874 MB/s | 1872 MB/s |
| aloelite mount | 126 MB/s | 225 MB/s |

Roughly **7× on write and 8× on read** versus the raw filesystem — FUSE's
context-switch-per-operation overhead on top of the engine cost above. For
the workloads the mount is actually for (editors, config, source trees, git
repositories — see `doc/COMPATIBILITY.md`) this is not the binding
constraint; for bulk data movement, prefer the engine API, which skips the
kernel round trip entirely.
