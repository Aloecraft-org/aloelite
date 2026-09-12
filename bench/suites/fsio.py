# ./bench/suites/fsio.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
The four I/O shapes a user actually has, each against every frontend and
both volume modes, with ext4 on the same disk as the row that separates
aloelite's overhead from the hardware's.

Surface
-------
Entry points
  throughput        sequential write / cold read / warm read, MiB/s
  smallfile         create / stat / readdir / unlink ops/s on a maildir tree
  random_read       pread p50/p99 at arbitrary offsets in a large file
  append_latency    per-record append commit latency, rsyslog-shaped

Configurable values
  RECORD_BYTES      one syslog-ish line
  PREAD_SIZES       the read widths random_read sweeps

Fan-out points
  Every entry point here is one key in bench.suites.SUITES. The frontend and
  volume axes come from corpus.backends_for; nothing below chooses them.
"""

from __future__ import annotations

from pathlib import Path

from ..corpus import backends_for, block_source, maildir_tree, payload
from ..harness import (
    MiB,
    Row,
    Run,
    latency_detail,
    rate,
    summarize,
    timed,
    warm_cache,
)

RECORD_BYTES = 220
PREAD_SIZES = (4096, 65536)


def throughput(run: Run, scratch: Path, cfg: dict) -> None:
    """Sequential MiB/s, streamed in blocks so the corpus is never held whole.

    Each round writes a DIFFERENT corpus to a DIFFERENT path. Rewriting the
    same bytes into a convergent volume would dedup, and the second round
    would report the speed of storing nothing.
    """
    size = cfg["seq_mb"] * MiB
    rounds = cfg["rounds"]
    with backends_for(scratch / "throughput") as backends:
        for b in backends:
            writes, cold_reads, warm_reads = [], [], []
            for r in range(rounds):
                path = f"/seq{r}.bin"
                src = block_source(size, kind="unique", seed=1000 + r)
                # The barrier is inside the timed region: "written" means on
                # the platter, identically for every backend (see
                # Backend.durable).
                writes.append(
                    timed(lambda p=path, s=src: (b.write_stream(p, s), b.durable(p)))
                )

                b.go_cold(path)
                cold_reads.append(timed(lambda p=path: b.read_stream(p)))

                warm_cache(*b.host_files(path))
                warm_reads.append(timed(lambda p=path: b.read_stream(p)))
            for metric, samples, cache in (
                ("seq_write", writes, "cold"),
                ("seq_read", cold_reads, "cold"),
                ("seq_read", warm_reads, "warm"),
            ):
                run.add(
                    Row(
                        suite="throughput",
                        metric=metric,
                        unit="MiB/s",
                        value=summarize(samples, size)["median"],
                        frontend=b.name,
                        volume=b.volume,
                        cache=cache,
                        n=len(samples),
                        detail=summarize(samples, size),
                        note=f"{cfg['seq_mb']} MiB, incompressible",
                    )
                )


def smallfile(run: Run, scratch: Path, cfg: dict) -> None:
    """Maildir-shaped: many ~4 KB files over a few directories. The cost here
    is per-operation, not per-byte, which is why it is a separate measurement
    from throughput and why it is the one that matches a mail spool, a git
    checkout, or a node_modules tree."""
    tree = maildir_tree(cfg["smallfile_files"], cfg["smallfile_dirs"])
    dirs = sorted({Path(p).parent.as_posix() for p, _ in tree})
    with backends_for(scratch / "smallfile") as backends:
        for b in backends:
            for d in dirs:
                b.mkdir(d)

            create = timed(lambda: [b.write(p, d) for p, d in tree])

            # Cold BEFORE warm, or the labels lie: the first pass is what
            # populates the kernel's attribute and dentry caches, and
            # posix_fadvise evicts data pages only — it cannot reach them.
            # Measuring warm first and calling the second pass cold reports
            # the attribute cache and names it storage.
            b.go_cold(tree[0][0])
            cold_stat = timed(lambda: [b.stat_size(p) for p, _ in tree])
            warm_stat = timed(lambda: [b.stat_size(p) for p, _ in tree])
            readdir = timed(lambda: [b.listdir(d) for d in dirs])

            unlink = timed(lambda: [b.unlink(p) for p, _ in tree])

            n = len(tree)
            per_readdir = n / len(dirs)
            for metric, seconds, count, cache, extra in (
                ("create_ops", create, n, "-", {}),
                ("stat_ops", cold_stat, n, "cold", {}),
                ("stat_ops", warm_stat, n, "warm", {}),
                (
                    "readdir_ops",
                    readdir,
                    len(dirs),
                    "warm",
                    {"entries_per_s": n / readdir if readdir else 0.0},
                ),
                ("unlink_ops", unlink, n, "-", {}),
            ):
                run.add(
                    Row(
                        suite="smallfile",
                        metric=metric,
                        unit="ops/s",
                        value=count / seconds if seconds > 0 else float("inf"),
                        frontend=b.name,
                        volume=b.volume,
                        cache=cache,
                        n=count,
                        detail={"elapsed_s": seconds, **extra},
                        note=(
                            f"{n} files / {len(dirs)} dirs, 4 KiB each"
                            + (f", {per_readdir:.0f} entries/dir" if extra else "")
                        ),
                    )
                )


def random_read(run: Run, scratch: Path, cfg: dict) -> None:
    """pread at arbitrary offsets in one large file — the qemu-img-from-mount
    shape, and the one where chunk size shows up directly: a 4 KiB read still
    costs a whole chunk fetch and, on an encrypted volume, a whole chunk
    decrypt."""
    import random

    size = cfg["random_file_mb"] * MiB
    reads = cfg["random_reads"]
    rng = random.Random(20260911)
    with backends_for(scratch / "random") as backends:
        for b in backends:
            path = "/image.raw"
            b.write_stream(path, block_source(size, kind="unique", seed=42))
            b.durable(path)
            for width in PREAD_SIZES:
                offsets = [rng.randrange(0, size - width) for _ in range(reads)]
                b.go_cold(path)
                samples = [
                    timed(lambda o=o, w=width: b.pread(path, o, w)) for o in offsets
                ]
                detail = latency_detail(samples)
                detail["read_MiB_s"] = rate(reads * width, sum(samples))
                run.add(
                    Row(
                        suite="random",
                        metric=f"pread_{width // 1024}k",
                        unit="ms",
                        value=detail["p50_ms"],
                        frontend=b.name,
                        volume=b.volume,
                        cache="cold",
                        n=reads,
                        detail=detail,
                        note=f"{reads} reads in a {cfg['random_file_mb']} MiB file",
                    )
                )


def append_latency(run: Run, scratch: Path, cfg: dict) -> None:
    """An rsyslog-shaped writer: small records appended one at a time, each
    one durable before the next. The measurement is the commit, not the
    bytes — a log writer's complaint is never bandwidth.

    On the engine an append rewrites the tail chunk, so the per-record cost
    rises with how full that chunk is. That is a real property of a
    fixed-chunk content pool and the reason this row exists separately.
    """
    records = cfg["append_records"]
    line = payload(RECORD_BYTES, kind="unique", seed=3)
    with backends_for(scratch / "append") as backends:
        for b in backends:
            path = "/messages.log"
            b.write(path, b"")
            samples = [timed(lambda: b.append(path, line)) for _ in range(records)]
            detail = latency_detail(samples)
            detail["records_per_s"] = records / sum(samples) if sum(samples) else 0.0
            run.add(
                Row(
                    suite="append",
                    metric="append_commit",
                    unit="ms",
                    value=detail["p50_ms"],
                    frontend=b.name,
                    volume=b.volume,
                    cache="warm",
                    n=records,
                    detail=detail,
                    note=f"{records} x {RECORD_BYTES} B, durable per record",
                )
            )


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
