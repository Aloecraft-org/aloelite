# ./bench/suites/robustness.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
What happens when the volume is not yours alone, and what happens when the
process dies mid-write.

  concurrency   readers and a writer on one file, each with its own
                connection (WAL's supported shape), measured alone and then
                under contention — plus the SQLITE_BUSY count, which is the
                number that says whether the contention was absorbed or
                merely survived.
  durability    a kill -9 loop. The contract under test is the one the README
                states: a write whose close() returned is committed, and a
                crashed daemon loses only what was still in flight. The
                measurement is the count of files that were CONFIRMED and are
                then missing or fail deep verify. The target is zero, and a
                non-zero result is a bug report, not a slow number.

Surface
-------
Entry points
  concurrency / durability     the two suites
  main()                       the worker, reached as `python -m`

Configurable values
  FILE_BYTES        one file in the durability loop
  KILL_DELAY_RANGE  when in the child's life the kill lands
  WORKER_OPS        what the concurrency workers do

Fan-out points
  WORKER_OPS is the worker dispatch table. `durability` branches on frontend
  in _round; nothing else below chooses a shape.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path

from ..corpus import (
    VOLUME_MODES,
    attach_session,
    block_source,
    engine_session,
    fuse_session,
    unmount,
)
from ..harness import MiB, Row, Run, each, fuse_available, latency_detail

FILE_BYTES = 128 * 1024
KILL_DELAY_RANGE = (0.15, 1.2)
_REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# concurrency
# ---------------------------------------------------------------------------
def concurrency(run: Run, scratch: Path, cfg: dict) -> None:
    """Alone, then together. The ratio between the two is the answer.

    Processes, not threads: the question is what sqlite's locking does, and
    threads in one interpreter would add the GIL's answer to sqlite's.
    """
    seconds = cfg["concurrency_seconds"]
    readers = cfg["readers"]
    for mode in ("plain", "convergent"):
        fs_file = scratch / f"conc-{mode}.fs"
        with engine_session(fs_file, mode, name="shared") as (_fs, m):
            with m.open_write("/payload.bin") as fh:
                for block in block_source(64 * MiB, "unique", seed=8):
                    fh.write(block)

        alone_r = _spawn_workers(fs_file, mode, "reader", 1, seconds)
        alone_w = _spawn_workers(fs_file, mode, "writer", 1, seconds)
        both = _spawn_workers(fs_file, mode, "reader", readers, seconds, writers=1)

        for role, solo, mixed, workers in (
            ("reader", alone_r, both["reader"], readers),
            ("writer", alone_w, both["writer"], 1),
        ):
            run.add(
                Row(
                    suite="concurrency",
                    metric=f"{role}_throughput_solo",
                    unit="MiB/s",
                    value=solo["MiB_s"],
                    frontend="direct",
                    volume=mode,
                    n=int(solo["ops"]),
                    detail=solo,
                    note=f"one {role}, nothing else touching the file",
                )
            )
            # AGGREGATE across the workers of this role, so the reader row is
            # what the file delivers in total. Divided out per worker as well:
            # `n` readers reaching 1.5x one reader is poor scaling, and a row
            # that only showed the aggregate would read as a speed-up.
            per_worker = mixed["MiB_s"] / workers if workers else 0.0
            run.add(
                Row(
                    suite="concurrency",
                    metric=f"{role}_throughput_contended",
                    unit="MiB/s",
                    value=mixed["MiB_s"],
                    frontend="direct",
                    volume=mode,
                    n=int(mixed["ops"]),
                    detail={
                        **mixed,
                        "workers": float(workers),
                        "MiB_s_per_worker": per_worker,
                        "aggregate_vs_solo": (
                            mixed["MiB_s"] / solo["MiB_s"] if solo["MiB_s"] else 0.0
                        ),
                        "scaling_efficiency": (
                            per_worker / solo["MiB_s"] if solo["MiB_s"] else 0.0
                        ),
                    },
                    note=(
                        f"{readers} readers + 1 writer on one file; "
                        f"aggregate over {workers} {role}(s)"
                    ),
                )
            )
        run.add(
            Row(
                suite="concurrency",
                metric="sqlite_busy_errors",
                unit="count",
                value=float(both["reader"]["busy"] + both["writer"]["busy"]),
                frontend="direct",
                volume=mode,
                n=int(both["reader"]["ops"] + both["writer"]["ops"]),
                detail={
                    "reader_busy": both["reader"]["busy"],
                    "writer_busy": both["writer"]["busy"],
                    "busy_timeout_ms": 5000.0,
                },
                note=(
                    "operations refused AFTER the 5s busy_timeout expired. "
                    "Retries inside the timeout are invisible from Python — "
                    "sqlite3 exposes no busy-handler callback — so they show "
                    "up as latency in the rows above, not here."
                ),
            )
        )


def _spawn_workers(
    fs_file: Path, mode: str, role: str, count: int, seconds: float, writers: int = 0
) -> dict:
    """Run `count` workers of `role` (plus `writers` writers) concurrently and
    return their aggregate, keyed by role when the mix is heterogeneous."""
    procs = []
    for i in range(count):
        procs.append((role, _worker(fs_file, mode, role, seconds, i)))
    for i in range(writers):
        procs.append(("writer", _worker(fs_file, mode, "writer", seconds, 100 + i)))

    totals: dict[str, dict] = {}
    failed = 0
    for kind, proc in procs:
        out, err = proc.communicate()
        acc = totals.setdefault(kind, {"ops": 0.0, "bytes": 0.0, "busy": 0.0, "s": 0.0})
        try:
            got = json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            # A worker that could not even start is not a zero-throughput
            # datapoint; say so rather than averaging it in.
            failed += 1
            print(err, file=sys.stderr)
            continue
        acc["ops"] += got["ops"]
        acc["bytes"] += got["bytes"]
        acc["busy"] += got["busy"]
        acc["mount_retries"] = acc.get("mount_retries", 0.0) + got.get(
            "mount_retries", 0
        )
        acc["s"] = max(acc["s"], got["elapsed"])
    for acc in totals.values():
        acc["MiB_s"] = (acc["bytes"] / MiB) / acc["s"] if acc["s"] else 0.0
        acc["workers_failed"] = float(failed)
    return totals if writers else totals.get(role, {"MiB_s": 0.0, "ops": 0.0})


def _worker(fs_file: Path, mode: str, role: str, seconds: float, seed: int):
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "bench.suites.robustness",
            role,
            str(fs_file),
            mode,
            str(seconds),
            str(seed),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(_REPO_ROOT),
    )


# ---------------------------------------------------------------------------
# durability
# ---------------------------------------------------------------------------
def durability(run: Run, scratch: Path, cfg: dict) -> None:
    """kill -9 during writes, `kill_rounds` times.

    A file is CONFIRMED when the writer's close() (or fsync, through a mount)
    returned and the writer then recorded it in a journal it fsynced. The
    journal is written AFTER the commit on purpose: a crash between the two
    loses a journal line for a file that exists, which is harmless. The
    reverse — a journal line for a file that does not — is the failure this
    suite exists to count.
    """
    targets = [("direct", "plain"), ("direct", "convergent")]
    if fuse_available():
        targets.append(("fuse", "plain"))
    else:
        run.skip("durability/fuse", "needs /dev/fuse, fusermount3 and pyfuse3")

    rounds = cfg["kill_rounds"]
    for frontend, mode in targets:
        confirmed = lost = corrupt = 0
        reopen_samples: list[float] = []
        for r in each(f"{frontend}/{mode} kill round", range(1, rounds + 1)):
            got = _round(scratch / f"kill-{frontend}-{mode}-{r}", frontend, mode, r)
            confirmed += got["confirmed"]
            lost += got["lost"]
            corrupt += got["corrupt"]
            reopen_samples.append(got["reopen_s"])

        run.add(
            Row(
                suite="durability",
                metric="confirmed_then_lost",
                unit="count",
                value=float(lost + corrupt),
                frontend=frontend,
                volume=mode,
                n=confirmed,
                detail={
                    "rounds": float(rounds),
                    "files_confirmed": float(confirmed),
                    "missing_after_crash": float(lost),
                    "failed_verify_after_crash": float(corrupt),
                },
                note="target is zero; anything else is a durability bug",
            )
        )
        run.add(
            Row(
                suite="durability",
                metric="reopen_after_crash",
                unit="ms",
                value=latency_detail(reopen_samples)["p50_ms"],
                frontend=frontend,
                volume=mode,
                n=len(reopen_samples),
                detail=latency_detail(reopen_samples),
                note="open + mount + deep verify of a volume killed mid-write",
            )
        )


def _round(workdir: Path, frontend: str, mode: str, seed: int) -> dict:
    """One crash: start a writer, SIGKILL it (or its daemon), then audit."""
    workdir.mkdir(parents=True, exist_ok=True)
    fs_file = workdir / "vol.fs"
    journal = workdir / "journal.txt"
    rng = random.Random(seed)

    # Bootstrap the volume here, so the crash window covers CONTENT writes
    # rather than volume creation. A kill during bootstrap leaves no volume
    # to audit and would score as a round with nothing in it.
    with engine_session(fs_file, mode, name="crash") as (_fs, _m):
        pass

    if frontend == "fuse":
        mp = workdir / "mnt"
        with fuse_session(fs_file, mode, mp, name="crash") as daemon:
            writer = _crash_writer(fs_file, mode, journal, "fuse", mp)
            time.sleep(rng.uniform(*KILL_DELAY_RANGE))
            # Kill the DAEMON: the writer is an ordinary application, and the
            # claim is about what survives when the filesystem process dies.
            os.kill(daemon.pid, signal.SIGKILL)
            writer.kill()
            writer.wait(timeout=30)
        unmount(mp)
    else:
        writer = _crash_writer(fs_file, mode, journal, "direct", None)
        time.sleep(rng.uniform(*KILL_DELAY_RANGE))
        os.kill(writer.pid, signal.SIGKILL)
        writer.wait(timeout=30)

    return _audit(fs_file, mode, journal)


def _crash_writer(fs_file: Path, mode: str, journal: Path, frontend: str, mp):
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "bench.suites.robustness",
            "crash-writer",
            str(fs_file),
            mode,
            str(journal),
            frontend,
            str(mp) if mp is not None else "-",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(_REPO_ROOT),
    )


def _audit(fs_file: Path, mode: str, journal: Path) -> dict:
    """Reopen the killed volume and check every journalled file."""
    from aloelite.aloelite import Aloelite

    claims = []
    if journal.exists():
        for line in journal.read_text().splitlines():
            index, digest = line.split(None, 1)
            claims.append((int(index), digest.strip()))

    lost = corrupt = 0
    spec = VOLUME_MODES[mode]
    start = time.perf_counter()
    fs = Aloelite(fs_file)
    try:
        vol = fs.resolve_volume_name("crash")
        if vol is None:
            # No volume at all. Every journal line is then a broken promise;
            # an empty journal means the crash landed before anything was
            # confirmed, which is a valid round with nothing to check.
            return {
                "confirmed": len(claims),
                "lost": len(claims),
                "corrupt": 0,
                "reopen_s": time.perf_counter() - start,
            }
        with fs.mount(vol, pin=spec["pin"]) as m:
            for index, digest in claims:
                path = f"/f{index:06d}.bin"
                try:
                    got = hashlib.sha256(m.read_all(path)).hexdigest()
                except Exception:
                    lost += 1
                    continue
                if got != digest:
                    corrupt += 1
            report = m.verify(deep=True)
            if not report.ok:
                corrupt += len(report.problems)
    finally:
        fs.close()
    return {
        "confirmed": len(claims),
        "lost": lost,
        "corrupt": corrupt,
        "reopen_s": time.perf_counter() - start,
    }


# ---------------------------------------------------------------------------
# workers
# depth: the child side of both suites
# ---------------------------------------------------------------------------
def _content(index: int) -> bytes:
    return random.Random(index).randbytes(FILE_BYTES)


def _w_reader(fs_file: Path, mode: str, seconds: float, seed: int) -> dict:
    import sqlite3

    rng = random.Random(seed)
    ops = nbytes = busy = 0
    with attach_session(fs_file, mode, "shared") as (_fs, m, mount_retries):
        start = time.perf_counter()
        size = m.stat("/payload.bin").size or 0
        while time.perf_counter() - start < seconds:
            off = rng.randrange(0, max(1, size - MiB))
            try:
                with m.open_read("/payload.bin") as fh:
                    fh.seek(off)
                    nbytes += len(fh.read(MiB))
                ops += 1
            except sqlite3.OperationalError:
                busy += 1
    return {
        "ops": ops,
        "bytes": nbytes,
        "busy": busy,
        "mount_retries": mount_retries,
        "elapsed": time.perf_counter() - start,
    }


def _w_writer(fs_file: Path, mode: str, seconds: float, seed: int) -> dict:
    import sqlite3

    ops = nbytes = busy = 0
    with attach_session(fs_file, mode, "shared") as (_fs, m, mount_retries):
        start = time.perf_counter()
        i = 0
        while time.perf_counter() - start < seconds:
            try:
                m.create_entry(f"/w{seed}-{i:06d}.bin", _content(seed * 1000 + i))
                nbytes += FILE_BYTES
                ops += 1
            except sqlite3.OperationalError:
                busy += 1
            i += 1
    return {
        "ops": ops,
        "bytes": nbytes,
        "busy": busy,
        "mount_retries": mount_retries,
        "elapsed": time.perf_counter() - start,
    }


def _w_crash_writer(fs_file: Path, mode: str, journal: Path, frontend: str, mp) -> None:
    """Write, confirm, journal, repeat — until something kills this process.

    The journal line goes down AFTER the write is committed and is itself
    fsynced, so every line is a promise the engine already made.
    """
    jfd = os.open(journal, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)

    def _confirm(index: int, data: bytes) -> None:
        os.write(jfd, f"{index} {hashlib.sha256(data).hexdigest()}\n".encode())
        os.fsync(jfd)

    if frontend == "fuse":
        root = Path(mp)
        for index in range(1, 1_000_000):
            data = _content(index)
            target = root / f"f{index:06d}.bin"
            with open(target, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            _confirm(index, data)
        return

    with attach_session(fs_file, mode, name="crash") as (_fs, m, _retries):
        for index in range(1, 1_000_000):
            data = _content(index)
            m.create_entry(f"/f{index:06d}.bin", data)
            _confirm(index, data)


WORKER_OPS = {
    "reader": _w_reader,
    "writer": _w_writer,
    "crash-writer": _w_crash_writer,
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    role = argv[0]
    if role == "crash-writer":
        fs_file, mode, journal, frontend, mp = argv[1:6]
        _w_crash_writer(
            Path(fs_file), mode, Path(journal), frontend, None if mp == "-" else mp
        )
        return 0
    fs_file, mode, seconds, seed = argv[1:5]
    got = WORKER_OPS[role](Path(fs_file), mode, float(seconds), int(seed))
    print(json.dumps(got))
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
