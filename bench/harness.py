# ./bench/harness.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
Measurement primitives shared by every suite: what a result IS, what a run
records about the machine that produced it, and how a number is taken.

Nothing here asserts. Throughput is a property of the host, so a benchmark
that failed a build would only ever be reporting on the runner. CI publishes
these numbers; it does not gate on them.

Surface
-------
Entry points
  Run.begin / Run.add / Run.write     one run's metadata + rows -> results.json
  Row                                 one measured number, fully labelled
  timed / repeat / rate / summarize   how a sample is taken and reduced
  drop_cache / warm_cache             the cold-vs-warm discipline
  peak_rss_self / peak_rss_pid        bounded-memory evidence
  describe_host / describe_volume     the metadata every row is read against

Configurable values
  SCALES            the three size profiles (smoke / ci / full)
  DEFAULT_ROUNDS    samples per throughput measurement
  PERCENTILES       which latency quantiles get reported

Fan-out points
  SCALES keys are the only profile names; bench.suites.SUITES is the
  matching registry of what can be run. A new profile is a key here and
  nothing else.
"""

from __future__ import annotations

import json
import os
import platform
import resource
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

MiB = 1 << 20
GiB = 1 << 30

DEFAULT_ROUNDS = 3
PERCENTILES = (50, 99)

# Size profiles. `full` is what produces the published figures on a real host;
# `ci` is what fits a 2-vCPU / ~25 GB GitHub runner inside a sane wall clock;
# `smoke` exists so a pull request can prove the harness still runs at all
# without spending ten minutes to do it.
#
# Every suite reads its sizes from here. A number hard-coded in a suite is a
# bug: it makes that suite unrunnable at a different scale.
SCALES: dict[str, dict[str, Any]] = {
    "smoke": {
        "rounds": 1,
        "seq_mb": 8,
        "smallfile_files": 200,
        "smallfile_dirs": 4,
        "random_reads": 200,
        "random_file_mb": 16,
        "append_records": 200,
        "space_corpus_mb": 8,
        "dir_sizes": (100, 1_000),
        "ingest_chunk": 4096,
        "ingest_rows": (5_000, 10_000),
        "stream_mb": 64,
        "verify_mb": 16,
        "kill_rounds": 3,
        "concurrency_seconds": 2.0,
        "readers": 2,
    },
    "ci": {
        "rounds": 3,
        "seq_mb": 64,
        "smallfile_files": 2_000,
        "smallfile_dirs": 16,
        "random_reads": 2_000,
        "random_file_mb": 256,
        "append_records": 2_000,
        "space_corpus_mb": 64,
        "dir_sizes": (1_000, 5_000, 10_000),
        "ingest_chunk": 4096,
        "ingest_rows": (100_000, 250_000, 500_000),
        "stream_mb": 1_024,
        "verify_mb": 256,
        "kill_rounds": 12,
        "concurrency_seconds": 10.0,
        "readers": 3,
    },
    "full": {
        "rounds": 5,
        "seq_mb": 4_096,
        "smallfile_files": 100_000,
        "smallfile_dirs": 256,
        "random_reads": 20_000,
        "random_file_mb": 16_384,
        "append_records": 100_000,
        "space_corpus_mb": 1_024,
        "dir_sizes": (1_000, 10_000, 100_000, 1_000_000),
        "ingest_chunk": 4096,
        "ingest_rows": (1_000_000, 5_000_000, 10_000_000),
        "stream_mb": 102_400,
        "verify_mb": 10_240,
        "kill_rounds": 200,
        "concurrency_seconds": 120.0,
        "readers": 8,
    },
}


# ---------------------------------------------------------------------------
# what a result is
# ---------------------------------------------------------------------------
@dataclass
class Row:
    """One measured number and every label needed to read it honestly.

    The label fields are not decoration: a throughput figure without its
    frontend, its volume mode and its cache state is unattributable, and two
    such figures averaged together are worse than neither.
    """

    suite: str
    metric: str
    unit: str  # MiB/s | ops/s | ms | ratio | bytes | count | s
    value: float
    frontend: str = "-"  # ext4 | direct | fuse | gocryptfs | restic
    volume: str = "-"  # plain | convergent | random | n/a
    cache: str = "-"  # cold | warm | n/a
    n: int = 1  # samples behind `value`
    detail: dict[str, float] = field(default_factory=dict)  # p50/p99/min/max/...
    note: str = ""

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.metric, self.frontend, self.volume, self.cache)


@dataclass
class Run:
    """One invocation: the host description every row is read against, plus
    the rows. Written as JSON so report.py can render it and so two runs can
    be diffed without re-running either."""

    scale: str
    host: dict[str, Any]
    started_at: str
    rows: list[Row] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    finished_at: str = ""

    @classmethod
    def begin(cls, scale: str, host: dict[str, Any]) -> "Run":
        return cls(
            scale=scale,
            host=host,
            started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )

    def add(self, row: Row) -> Row:
        self.rows.append(row)
        print(f"  {_fmt_row(row)}", flush=True)
        return row

    def skip(self, suite: str, why: str) -> None:
        self.skipped[suite] = why
        print(f"  [skipped] {suite}: {why}", flush=True)

    def write(self, path: Path) -> None:
        self.finished_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2, sort_keys=False) + "\n")


def _fmt_row(r: Row) -> str:
    labels = " ".join(x for x in (r.frontend, r.volume, r.cache) if x != "-")
    detail = ""
    if r.detail:
        detail = "  [" + " ".join(f"{k}={v:.4g}" for k, v in r.detail.items()) + "]"
    return f"{r.metric:<28} {r.value:12.4g} {r.unit:<7} {labels:<26}{detail}"


# ---------------------------------------------------------------------------
# how a number is taken
# depth: timing, reduction, cache control
# ---------------------------------------------------------------------------
def timed(fn: Callable[[], Any]) -> float:
    """Wall seconds for one call. perf_counter, not process time: a FUSE read
    spends most of its life in another process and all of it in the user's."""
    start = time.perf_counter()
    fn()
    return time.perf_counter() - start


def repeat(fn: Callable[[], Any], rounds: int, setup: Callable[[], Any] | None = None):
    """`rounds` samples, with `setup` run before each and NOT timed."""
    samples = []
    for _ in range(rounds):
        if setup is not None:
            setup()
        samples.append(timed(fn))
    return samples


def rate(nbytes: int, seconds: float) -> float:
    return (nbytes / MiB) / seconds if seconds > 0 else float("inf")


def summarize(samples: Sequence[float], nbytes: int | None = None) -> dict[str, float]:
    """min/median/max, as rates when nbytes is given and as milliseconds when
    it is not. Reported alongside every aggregate because on a shared runner
    the spread is frequently wider than the difference being measured."""
    if not samples:
        return {}
    if nbytes is not None:
        rates = sorted(rate(nbytes, s) for s in samples)
        return {
            "min": rates[0],
            "median": statistics.median(rates),
            "max": rates[-1],
        }
    ms = sorted(s * 1e3 for s in samples)
    return {"min_ms": ms[0], "median_ms": statistics.median(ms), "max_ms": ms[-1]}


def percentile(samples: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile. Deliberately not an interpolating estimator:
    at the sample counts a CI run can afford, interpolation invents a p99 that
    no operation actually experienced. Rows carry `n` so a p99 taken over 200
    samples can be read for what it is."""
    if not samples:
        return float("nan")
    ordered = sorted(samples)
    rank = max(1, min(len(ordered), int(round(pct / 100.0 * len(ordered) + 0.5))))
    return ordered[rank - 1]


def latency_detail(samples: Sequence[float]) -> dict[str, float]:
    """p50/p99 (plus the tail) in milliseconds — the shape for anything a
    caller waits on: pread, append commit, a foreground op during export."""
    if not samples:
        return {}
    out = {f"p{p}_ms": percentile(samples, p) * 1e3 for p in PERCENTILES}
    out["max_ms"] = max(samples) * 1e3
    out["mean_ms"] = statistics.fmean(samples) * 1e3
    return out


def drop_cache(*paths: Path) -> None:
    """Ask the kernel to forget these files' pages, so the next read measures
    storage (or the FUSE daemon) rather than memory. Without this a read
    benchmark reports the page cache's speed and calls it the filesystem's.

    Best-effort by design: POSIX_FADV_DONTNEED is advisory, and on a FUSE
    mount it only evicts what the kernel holds for that inode. Rows measured
    this way are labelled cold; rows that deliberately skip it are warm."""
    for path in paths:
        if not path.exists():
            continue
        if path.is_dir():
            drop_cache(*sorted(p for p in path.rglob("*") if p.is_file()))
            continue
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            continue
        try:
            os.fsync(fd)
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        except OSError:
            pass
        finally:
            os.close(fd)


def warm_cache(*paths: Path) -> None:
    """The counterpart: read the bytes once so the following measurement is
    explicitly a warm-cache number rather than an accidental one."""
    for path in paths:
        if path.is_file():
            with path.open("rb") as fh:
                while fh.read(4 * MiB):
                    pass


def peak_rss_self() -> int:
    """Bytes. ru_maxrss is KiB on Linux, bytes on macOS; this repo's CI is
    Linux and the conversion is stated rather than guessed."""
    kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return kb * 1024 if sys.platform.startswith("linux") else kb


def peak_rss_pid(pid: int) -> int:
    """VmHWM for another process — the FUSE daemon, whose memory is the whole
    point of the streaming-write claim and which ru_maxrss cannot see."""
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


# ---------------------------------------------------------------------------
# what a run records about its host
# depth: /proc and pragma scraping
# ---------------------------------------------------------------------------
def describe_host(scratch: Path, scale: str) -> dict[str, Any]:
    """Everything needed to decide whether two runs are comparable. sqlite
    version, journal mode and chunk size are here because a number taken under
    a different one of any of them is a different number."""
    from aloelite import _sqlite

    host: dict[str, Any] = {
        "scale": scale,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "cpu_model": _cpu_model(),
        "mem_total_gib": round(_mem_total() / GiB, 1),
        "sqlite_version": _sqlite.sqlite3.sqlite_version,
        "sqlite_source": (
            "bundled (pysqlite3)"
            if getattr(_sqlite.sqlite3, "__name__", "") != "sqlite3"
            else "stdlib / host libsqlite3"
        ),
        "aloelite_version": _aloelite_version(),
        "git_commit": _git_commit(),
        "scratch_fs": _fstype(scratch),
        "scratch_free_gib": round(shutil.disk_usage(scratch).free / GiB, 1),
        "fuse_available": fuse_available(),
    }
    host.update(describe_volume(scratch))
    return host


def describe_volume(scratch: Path) -> dict[str, Any]:
    """journal_mode and chunk_size as the engine actually sets them, read off
    a throwaway volume rather than copied from the source — the point of
    recording them is to catch the day they differ."""
    from aloelite.aloelite import Aloelite

    probe = scratch / "_probe.fs"
    try:
        with Aloelite(probe) as fs:
            vol = fs.create_volume("probe", enc_mode="none")
            conn = fs.db.connection
            journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            # 0=OFF 1=NORMAL 2=FULL 3=EXTRA. NORMAL in WAL means a commit is
            # not an fsync, which is why every throughput row carries its own
            # barrier instead of trusting the commit.
            sync = conn.execute("PRAGMA synchronous").fetchone()[0]
            busy_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            chunk = fs.db.chunk_size_of(str(vol.id))
        return {
            "journal_mode": journal,
            "synchronous": {0: "OFF", 1: "NORMAL", 2: "FULL", 3: "EXTRA"}.get(
                sync, str(sync)
            ),
            "busy_timeout_ms": busy_ms,
            "page_size": page_size,
            "chunk_size": chunk,
        }
    finally:
        for suffix in ("", "-wal", "-shm"):
            Path(str(probe) + suffix).unlink(missing_ok=True)


def fuse_available() -> bool:
    return (
        os.path.exists("/dev/fuse")
        and shutil.which("fusermount3") is not None
        and _importable("pyfuse3")
    )


def _importable(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def _mem_total() -> int:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def _fstype(path: Path) -> str:
    """Which filesystem the scratch dir is on. A tmpfs scratch would make
    every 'on-disk' number a memory number, so this is load-bearing."""
    best, best_len = "unknown", -1
    target = str(path.resolve())
    try:
        for line in Path("/proc/mounts").read_text().splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            point, fstype = parts[1], parts[2]
            if target == point or target.startswith(point.rstrip("/") + "/"):
                if len(point) > best_len:
                    best, best_len = fstype, len(point)
    except OSError:
        pass
    return best


def _aloelite_version() -> str:
    """Installed distribution version. The package exports no __version__,
    so metadata is the only source that is not a second copy of the number."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("aloelite")
    except PackageNotFoundError:
        return "unknown"


def _git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def section(title: str) -> None:
    print(f"\n== {title} ==", flush=True)


def each(label: str, items: Iterable[Any]) -> Iterable[Any]:
    """Progress for the long suites — a CI log that prints nothing for four
    minutes is indistinguishable from a hung one."""
    for item in items:
        print(f"   .. {label} {item}", flush=True)
        yield item


# Copyright Michael Godfrey 2026 | aloecraft.org <michael@aloecraft.org>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
