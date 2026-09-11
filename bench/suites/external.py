# ./bench/suites/external.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
One outside comparator per category, so the numbers mean something to a
reader who has never heard of aloelite.

  gocryptfs   an encrypted FUSE filesystem. The closest thing to aloelite's
              mount: same kernel path, same AEAD-per-block idea, a directory
              of files instead of one database. It is the answer to "what
              does the database cost me at the mount?".
  restic      a content-addressed, encrypted backup repository. Not a
              filesystem, and that is the point: it is what people actually
              use for the dedup-and-ingest job aloelite also does, so it is
              the right yardstick for ingest rate and dedup ratio.

Both are optional. Absent, the suite records WHY it produced nothing rather
than silently reporting less.

Surface
-------
Entry points
  comparators        the whole suite
  gocryptfs_rows / restic_rows       one comparator each

Configurable values
  GOCRYPTFS_PASSWORD / RESTIC_PASSWORD    throwaway, and printed here on
                                          purpose: nothing secret is stored
  COMPARATOR_TIMEOUT_S

Fan-out points
  COMPARATORS is the dispatch table: one entry per external tool.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ..corpus import (
    PosixBackend,
    block_source,
    have,
    maildir_tree,
    payload,
    unmount,
)
from ..harness import MiB, Row, Run, summarize, timed

GOCRYPTFS_PASSWORD = "benchmark-not-a-secret"
RESTIC_PASSWORD = "benchmark-not-a-secret"
COMPARATOR_TIMEOUT_S = 900


def comparators(run: Run, scratch: Path, cfg: dict) -> None:
    for name, fn in COMPARATORS.items():
        if not have(name):
            run.skip(
                f"external/{name}",
                f"{name} not installed (apt-get install -y {name})",
            )
            continue
        fn(run, scratch / name, cfg)


# ---------------------------------------------------------------------------
# gocryptfs: encrypted FUSE throughput, against aloelite's encrypted mount
# ---------------------------------------------------------------------------
def gocryptfs_rows(run: Run, scratch: Path, cfg: dict) -> None:
    scratch.mkdir(parents=True, exist_ok=True)
    cipher = scratch / "cipher"
    mp = scratch / "plain"
    cipher.mkdir()
    mp.mkdir()

    env = dict(os.environ, GOCRYPTFS_PASSWORD=GOCRYPTFS_PASSWORD)
    init = _run(
        [
            "gocryptfs",
            "-init",
            "-q",
            "-extpass",
            "echo $GOCRYPTFS_PASSWORD",
            str(cipher),
        ],
        env,
    )
    if init.returncode != 0:
        run.skip("external/gocryptfs", f"init failed: {init.stderr.strip()[:200]}")
        return
    mount = _run(
        [
            "gocryptfs",
            "-q",
            "-extpass",
            "echo $GOCRYPTFS_PASSWORD",
            str(cipher),
            str(mp),
        ],
        env,
    )
    if mount.returncode != 0 or not os.path.ismount(mp):
        run.skip("external/gocryptfs", f"mount failed: {mount.stderr.strip()[:200]}")
        return

    try:
        backend = PosixBackend(mp, "gocryptfs", "encrypted")
        _io_rows(run, backend, cfg, version=_version("gocryptfs"))
    finally:
        unmount(mp)


def _io_rows(run: Run, backend, cfg: dict, version: str) -> None:
    """The same sequence the throughput and smallfile suites run, so the rows
    line up with aloelite's without a second definition of the workload."""
    size = cfg["seq_mb"] * MiB
    rounds = cfg["rounds"]
    writes, reads = [], []
    for r in range(rounds):
        path = f"/seq{r}.bin"
        src = block_source(size, "unique", seed=2000 + r)
        writes.append(
            timed(
                lambda p=path, s=src: (backend.write_stream(p, s), backend.durable(p))
            )
        )
        backend.go_cold(path)
        reads.append(timed(lambda p=path: backend.read_stream(p)))

    for metric, samples in (("seq_write", writes), ("seq_read", reads)):
        run.add(
            Row(
                suite="external",
                metric=metric,
                unit="MiB/s",
                value=summarize(samples, size)["median"],
                frontend=backend.name,
                volume=backend.volume,
                cache="cold",
                n=len(samples),
                detail=summarize(samples, size),
                note=version,
            )
        )

    tree = maildir_tree(cfg["smallfile_files"], cfg["smallfile_dirs"])
    dirs = sorted({Path(p).parent.as_posix() for p, _ in tree})
    for d in dirs:
        backend.mkdir(d)
    create = timed(lambda: [backend.write(p, d) for p, d in tree])
    backend.go_cold(tree[0][0])
    stat = timed(lambda: [backend.stat_size(p) for p, _ in tree])
    run.add(
        Row(
            suite="external",
            metric="create_ops",
            unit="ops/s",
            value=len(tree) / create if create else 0.0,
            frontend=backend.name,
            volume=backend.volume,
            n=len(tree),
            detail={"elapsed_s": create},
            note=version,
        )
    )
    run.add(
        Row(
            suite="external",
            metric="stat_ops",
            unit="ops/s",
            value=len(tree) / stat if stat else 0.0,
            frontend=backend.name,
            volume=backend.volume,
            cache="cold",
            n=len(tree),
            detail={"elapsed_s": stat},
            note=version,
        )
    )


# ---------------------------------------------------------------------------
# restic: ingest rate and dedup, against the engine's own ingest
# ---------------------------------------------------------------------------
def restic_rows(run: Run, scratch: Path, cfg: dict) -> None:
    """Ingest and dedup for the same corpus, restic beside aloelite.

    Not a like-for-like on features: restic writes an immutable, encrypted,
    remote-capable repository and aloelite writes a mountable filesystem.
    The comparison is worth having anyway, because the ingest-and-dedup job
    is one people currently reach for restic to do.
    """
    scratch.mkdir(parents=True, exist_ok=True)
    source = scratch / "source"
    repo = scratch / "repo"
    source.mkdir()
    version = _version("restic")

    size = cfg["space_corpus_mb"] * MiB
    template = payload(max(1, size // 8), "unique", seed=77)
    for i in range(8):
        (source / f"clone-{i}.bin").write_bytes(template)
    logical = len(template) * 8

    env = dict(os.environ, RESTIC_PASSWORD=RESTIC_PASSWORD)
    init = _run(["restic", "init", "--repo", str(repo)], env)
    if init.returncode != 0:
        run.skip("external/restic", f"init failed: {init.stderr.strip()[:200]}")
        return

    first = timed(
        lambda: _run(["restic", "backup", "--repo", str(repo), str(source)], env)
    )
    after_first = _tree_bytes(repo)
    second = timed(
        lambda: _run(["restic", "backup", "--repo", str(repo), str(source)], env)
    )
    after_second = _tree_bytes(repo)

    run.add(
        Row(
            suite="external",
            metric="ingest_rate",
            unit="MiB/s",
            value=(logical / MiB) / first if first else 0.0,
            frontend="restic",
            volume="encrypted",
            n=1,
            detail={"elapsed_s": first, "logical_bytes": float(logical)},
            note=f"{version}; first backup of the cloned corpus",
        )
    )
    run.add(
        Row(
            suite="external",
            metric="on_disk_over_logical_cloned",
            unit="ratio",
            value=after_first / logical if logical else 0.0,
            frontend="restic",
            volume="encrypted",
            n=1,
            detail={
                "repo_bytes": float(after_first),
                "logical_bytes": float(logical),
            },
            note=f"{version}; 8 copies of one template",
        )
    )
    run.add(
        Row(
            suite="external",
            metric="dedup_within_volume",
            unit="ratio",
            value=max(0.0, 1.0 - (after_second - after_first) / logical),
            frontend="restic",
            volume="encrypted",
            n=1,
            detail={
                "bytes_added": float(after_second - after_first),
                "second_backup_s": second,
            },
            note=f"{version}; the same corpus backed up a second time",
        )
    )


def _tree_bytes(root: Path) -> int:
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


def _run(argv: list[str], env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        env=env,
        timeout=COMPARATOR_TIMEOUT_S,
    )


def _version(tool: str) -> str:
    try:
        out = subprocess.run(
            [tool, "--version"], capture_output=True, text=True, timeout=30
        )
        return (out.stdout or out.stderr).strip().splitlines()[0][:80]
    except (OSError, subprocess.SubprocessError, IndexError):
        return tool


COMPARATORS = {"gocryptfs": gocryptfs_rows, "restic": restic_rows}
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
