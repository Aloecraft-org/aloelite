# ./bench/suites/scale.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
The three curves. Curves, not averages: the question each asks is whether a
number HOLDS as something grows, and an average over the growth is exactly
the statistic that hides the answer.

  ingest_scale   does insert rate sag as the chunk table's index deepens?
  dir_scale      do lookup and readdir stay flat as a directory fills?
  memory         does peak RSS stay flat as the stream gets longer?

The memory rows are the load-bearing ones for the streaming claim. They are
taken in a CHILD process, because ru_maxrss is a high-water mark that never
comes back down: measured in-process, the first big suite would poison every
later reading.

Surface
-------
Entry points
  ingest_scale / dir_scale / memory     the three curves
  main()                                the child-process worker (python -m)

Configurable values
  INGEST_CHUNK_DEFAULT   chunk size for the ingest curve, overridable by scale
  LOOKUP_SAMPLES         lookups timed per directory size
  CHILD_OPS              what the child worker knows how to do

Fan-out points
  CHILD_OPS is the worker's dispatch table; the parent reaches it only
  through `_child_rss`, and adding a phase means adding a key there.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from ..corpus import (
    FUSE_DAEMONS,
    VOLUME_MODES,
    block_source,
    engine_session,
    fuse_session,
    on_disk_bytes,
    rust_available,
)
from ..harness import (
    MiB,
    Row,
    Run,
    each,
    peak_rss_pid,
    peak_rss_self,
    rate,
    timed,
)

INGEST_CHUNK_DEFAULT = 4096
LOOKUP_SAMPLES = 500
# readdir is superlinear in directory size (see doc/BENCHMARKS.md), so the
# largest sizes would run for hours. Each size projects its cost from the
# previous measurement and skips itself rather than hanging the run; the skip
# is recorded with the projection, because "we did not measure it" and "it is
# fast" must not look the same in the output.
READDIR_BUDGET_S = 120.0


def ingest_scale(run: Run, scratch: Path, cfg: dict) -> None:
    """Ingest rate as the chunk table crosses each row target.

    Measured in ROWS per second as well as MiB/s, because the thing under
    test is B-tree insert cost at depth, which is per-row. The chunk size is
    deliberately small so that a million pool rows costs gigabytes rather
    than terabytes; the absolute MiB/s at this chunk size is therefore NOT
    the throughput number — that is the throughput suite's job.
    """
    chunk = cfg.get("ingest_chunk", INGEST_CHUNK_DEFAULT)
    targets = cfg["ingest_rows"]
    for mode in ("plain", "convergent"):
        spec = VOLUME_MODES[mode]
        fs_file = scratch / f"ingest-{mode}.fs"
        from aloelite.aloelite import Aloelite

        fs = Aloelite(fs_file)
        try:
            vol = fs.create_volume(
                "ingest", pin=spec["pin"], enc_mode=spec["enc_mode"], chunk_size=chunk
            )
            with fs.mount(vol.id, pin=spec["pin"]) as m:
                rows_done = 0
                for target in each("chunk rows ->", targets):
                    want = target - rows_done
                    if want <= 0:
                        continue
                    nbytes = want * chunk
                    # A fresh seed per segment: repeating bytes would dedup
                    # and the "insert rate" would be an update rate.
                    src = block_source(nbytes, "unique", block=chunk, seed=target)
                    elapsed = timed(
                        lambda t=target, s=src: _stream(m, f"/seg{t}.bin", s)
                    )
                    rows_done = target
                    run.add(
                        Row(
                            suite="ingest_scale",
                            metric="ingest_rate_at_rows",
                            unit="rows/s",
                            value=want / elapsed if elapsed else 0.0,
                            frontend="direct",
                            volume=mode,
                            n=want,
                            detail={
                                "pool_rows_after": float(target),
                                "chunk_size": float(chunk),
                                "MiB_s": rate(nbytes, elapsed),
                                "elapsed_s": elapsed,
                                "on_disk_bytes": float(on_disk_bytes(fs, fs_file)),
                            },
                            note=f"segment taking the pool to {target:,} rows",
                        )
                    )
        finally:
            fs.close()


def _stream(mount, path: str, blocks) -> None:
    with mount.open_write(path) as fh:
        for block in blocks:
            fh.write(block)


def dir_scale(run: Run, scratch: Path, cfg: dict) -> None:
    """Lookup and readdir against entries-per-directory.

    ONE DIRECTORY PER SIZE, not one directory that grows. A cumulative tree
    would leave the FUSE pass with only the largest size to measure and
    nothing to project the cost from — which is how a readdir that turned out
    to be far worse than the library's ran for half an hour before anyone
    could tell it would.

    The tree is built once through the library and then measured through both
    frontends: the same file, mounted after the builder closed it. A FUSE
    build at these sizes would cost minutes and measure the daemon's create
    path, which the smallfile suite already reports.
    """
    sizes = cfg["dir_sizes"]
    fs_file = scratch / "dirscale.fs"
    with engine_session(fs_file, "plain", name="dirs") as (fs, m):
        budget = _Budget()
        for size in each("directory entries ->", sizes):
            d = f"/d{size}"
            m.mkdir(d)
            for i in range(size):
                m.create_entry(f"{d}/e{i:07d}.txt", b"x")
            fs.db.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            _measure_dir(run, "direct", "plain", size, _EngineDir(m, d), budget)

    if not _fuse_ok():
        run.skip("dir_scale/fuse", "needs /dev/fuse, fusermount3 and pyfuse3")
        return
    mp = scratch / "dirscale-mnt"
    with fuse_session(fs_file, "plain", mp, name="dirs"):
        budget = _Budget()
        for size in each("directory entries (fuse) ->", sizes):
            _measure_dir(run, "fuse", "plain", size, _PosixDir(mp, f"d{size}"), budget)


class _EngineDir:
    def __init__(self, mount, directory: str):
        self.m = mount
        self.d = directory

    def lookup(self, i: int) -> None:
        self.m.stat(f"{self.d}/e{i:07d}.txt")

    def readdir(self) -> int:
        return len(self.m.list(self.d))


class _PosixDir:
    def __init__(self, root: Path, directory: str):
        self.root = root / directory

    def lookup(self, i: int) -> None:
        os.stat(self.root / f"e{i:07d}.txt")

    def readdir(self) -> int:
        return len(os.listdir(self.root))


class _Budget:
    """Decides whether the next readdir is worth waiting for.

    Fits the exponent from the last two measurements rather than assuming
    one. readdir is quadratic through the library and WORSE through the
    mount — the daemon re-lists the whole directory on every continuation
    call — so a hard-coded n^2 under-projects the FUSE curve badly, which is
    how a 3.5-minute listing gets waved through as a one-minute one.

    Conservative by construction: a wrong projection costs one skipped row,
    while no projection costs the run. Every skip records the number it was
    made from.
    """

    DEFAULT_EXPONENT = 2.0

    def __init__(self) -> None:
        self.points: list[tuple[int, float]] = []

    def affordable(self, size: int) -> tuple[bool, float, float]:
        """(worth running, projected seconds, the exponent it was fitted at)."""
        if not self.points:
            # Safe only because every ladder starts at its smallest size —
            # see dir_scale's note about one directory per size.
            return True, 0.0, self.DEFAULT_EXPONENT
        n0, t0 = self.points[-1]
        exponent = self._exponent()
        projected = t0 * (size / n0) ** exponent
        return projected <= READDIR_BUDGET_S, projected, exponent

    def record(self, size: int, seconds: float) -> None:
        self.points.append((size, seconds))

    def _exponent(self) -> float:
        import math

        if len(self.points) < 2:
            return self.DEFAULT_EXPONENT
        (n0, t0), (n1, t1) = self.points[-2], self.points[-1]
        if n1 <= n0 or t0 <= 0 or t1 <= t0:
            return self.DEFAULT_EXPONENT
        return max(1.0, math.log(t1 / t0) / math.log(n1 / n0))


def _measure_dir(run: Run, frontend: str, mode: str, size: int, d, budget) -> None:
    import random

    from ..harness import latency_detail

    rng = random.Random(size)
    samples = [
        timed(lambda i=rng.randrange(size): d.lookup(i))
        for _ in range(min(LOOKUP_SAMPLES, size))
    ]
    run.add(
        Row(
            suite="dir_scale",
            metric="lookup_at_dir_size",
            unit="ms",
            value=latency_detail(samples)["p50_ms"],
            frontend=frontend,
            volume=mode,
            cache="warm",
            n=len(samples),
            detail={"dir_entries": float(size), **latency_detail(samples)},
            note=f"stat of a random entry in a {size:,}-entry directory",
        )
    )

    ok, projected, exponent = budget.affordable(size)
    if not ok:
        run.skip(
            f"dir_scale/readdir/{frontend}/{size}",
            f"projected {projected:.0f}s exceeds the {READDIR_BUDGET_S:.0f}s "
            f"budget, fitted at n^{exponent:.2f} from {len(budget.points)} point(s)",
        )
        return

    entries = 0

    def _rd():
        nonlocal entries
        entries = d.readdir()

    readdir_s = timed(_rd)
    budget.record(size, readdir_s)
    run.add(
        Row(
            suite="dir_scale",
            metric="readdir_at_dir_size",
            unit="ms",
            value=readdir_s * 1e3,
            frontend=frontend,
            volume=mode,
            cache="warm",
            n=1,
            detail={
                "dir_entries": float(size),
                "entries_listed": float(entries),
                "entries_per_s": entries / readdir_s if readdir_s else 0.0,
                "ms_per_entry": readdir_s * 1e3 / entries if entries else 0.0,
            },
            note=f"one full listing of {entries:,} entries",
        )
    )


def memory(run: Run, scratch: Path, cfg: dict) -> None:
    """Peak RSS for a long stream, which should not depend on how long it is.

    Every phase runs in a child, and the child reports its OWN ru_maxrss.
    The claim under test is `memory bounded by chunk size, not by file size`,
    and the way to falsify it is to make the file much bigger than memory and
    watch the number not move.
    """
    mb = cfg["stream_mb"]
    for mode in ("plain", "convergent"):
        # Per mode, because the floors differ: unlocking an encrypted volume
        # runs Argon2id, whose memory_cost is 64 MiB by design. That shows up
        # as RSS and is not the streaming path's doing.
        for op in ("baseline", "write", "read", "export"):
            result = _child_rss(
                op, scratch / f"mem-{mode}-{op}.fs", mode, 0 if op == "baseline" else mb
            )
            if result is None:
                run.skip(f"memory/{mode}/{op}", "child worker failed")
                continue
            run.add(
                Row(
                    suite="memory",
                    metric=f"peak_rss_{op}",
                    unit="bytes",
                    value=float(result["rss"]),
                    frontend="direct",
                    volume=mode,
                    n=1,
                    detail={
                        "stream_MiB": 0.0 if op == "baseline" else float(mb),
                        "rss_MiB": result["rss"] / MiB,
                        "elapsed_s": result["elapsed"],
                        "MiB_s": rate(mb * MiB, result["elapsed"])
                        if result["elapsed"]
                        else 0.0,
                    },
                    note=(
                        "interpreter, imports and an open volume — subtract this"
                        if op == "baseline"
                        else f"{mb} MiB streamed in a child process"
                    ),
                )
            )

    if not _fuse_ok():
        run.skip("memory/fuse", "needs /dev/fuse, fusermount3 and pyfuse3")
        return
    # The daemon's memory, not the caller's: a FUSE write is bounded by what
    # the daemon holds, and VmHWM is the only place that shows up. Both
    # daemons, because "bounded by dirty bytes" is a claim each makes
    # separately and a Python floor is not a Rust floor.
    for daemon, impl in FUSE_DAEMONS.items():
        if impl == "rust" and not rust_available():
            run.skip(f"memory/{daemon}", "aloelite-fuse (rust) not built")
            continue
        mp = scratch / f"mem-mnt-{daemon}"
        fs_file = scratch / f"mem-{daemon}.fs"
        with fuse_session(fs_file, "plain", mp, impl=impl) as proc:
            target = mp / "stream.bin"
            elapsed = timed(lambda: _write_plain(target, mb))
            hwm = peak_rss_pid(proc.pid)
        run.add(
            Row(
                suite="memory",
                metric="peak_rss_write",
                unit="bytes",
                value=float(hwm),
                frontend=daemon,
                volume="plain",
                n=1,
                detail={
                    "stream_MiB": float(mb),
                    "rss_MiB": hwm / MiB,
                    "elapsed_s": elapsed,
                    "MiB_s": rate(mb * MiB, elapsed),
                },
                note="VmHWM of the daemon process, not of the writer",
            )
        )


def _write_plain(target: Path, mb: int) -> None:
    with open(target, "wb") as fh:
        for block in block_source(mb * MiB, "unique"):
            fh.write(block)
        fh.flush()
        os.fsync(fh.fileno())


def _fuse_ok() -> bool:
    from ..harness import fuse_available

    return fuse_available()


def _child_rss(op: str, fs_file: Path, mode: str, mb: int) -> dict | None:
    """Run one phase in a child and collect its peak RSS.

    ru_maxrss is a high-water mark for the life of a process: measured in
    the parent it would report the largest thing this benchmark ever did,
    not the thing being measured.
    """
    out = subprocess.run(
        [
            sys.executable,
            "-m",
            "bench.suites.scale",
            op,
            str(fs_file),
            mode,
            str(mb),
        ],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    if out.returncode != 0:
        print(out.stdout + out.stderr, file=sys.stderr)
        return None
    return json.loads(out.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------------------
# child worker
# depth: one phase per invocation, reporting its own ru_maxrss
# ---------------------------------------------------------------------------
def _child_write(fs_file: Path, mode: str, mb: int) -> float:
    with engine_session(fs_file, mode) as (_fs, m):
        return timed(
            lambda: _stream(m, "/stream.bin", block_source(mb * MiB, "unique"))
        )


def _child_read(fs_file: Path, mode: str, mb: int) -> float:
    with engine_session(fs_file, mode) as (_fs, m):
        _stream(m, "/stream.bin", block_source(mb * MiB, "unique"))

        def _drain():
            with m.open_read("/stream.bin") as fh:
                while fh.read(4 * MiB):
                    pass

        return timed(_drain)


def _child_export(fs_file: Path, mode: str, mb: int) -> float:
    from aloelite.transfer import export_volume

    from ..corpus import BENCH_PIN

    with engine_session(fs_file, mode) as (_fs, m):
        _stream(m, "/stream.bin", block_source(mb * MiB, "unique"))
        volume = m.info().volume
    pin = VOLUME_MODES[mode]["pin"]
    dest = Path(str(fs_file) + ".export")
    assert pin is None or pin == BENCH_PIN
    return timed(
        lambda: export_volume(
            str(fs_file), volume, str(dest), src_pin=pin, dest_pin=pin
        )
    )


def _child_baseline(fs_file: Path, mode: str, mb: int) -> float:
    """The floor: interpreter, imports, an open volume, no bytes moved. The
    streaming rows are only interesting as a distance from this."""
    with engine_session(fs_file, mode) as (_fs, _m):
        return 0.0


CHILD_OPS = {
    "baseline": _child_baseline,
    "write": _child_write,
    "read": _child_read,
    "export": _child_export,
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    op, fs_file, mode, mb = argv[0], Path(argv[1]), argv[2], int(argv[3])
    elapsed = CHILD_OPS[op](fs_file, mode, mb)
    print(json.dumps({"rss": peak_rss_self(), "elapsed": elapsed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
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
