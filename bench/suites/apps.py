# ./bench/suites/apps.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
Real applications whose data directory lives on a mount. The other suites
time I/O shapes; these run the program that produces the shape, so what
they report is what that program's user would see: transactions per
second, how long recovery takes, and how big the volume got.

  postgres   a PostgreSQL cluster whose PGDATA is on the mount. initdb,
             `pgbench -i`, the TPC-B-like mix, a read-only mix, then the
             space the volume took against the bytes in PGDATA (before and
             after prune + VACUUM), then a kill loop: SIGKILL the daemon
             (or, on ext4, the postmaster) mid-pgbench, remount, restart,
             and pg_amcheck every relation. ext4 on the same disk is the
             baseline in every table.

Surface
-------
Entry points
  postgres(run, scratch, cfg)     the suite

Configurable values
  PG_FRONTENDS     which frontends host a cluster (only mounts can)
  PG_PORT          the port number that names each cluster's socket
  PG_SETTINGS      server settings applied to every cluster, ext4 included
  PG_BIN_ENV       $ALOELITE_BENCH_PG_BIN, a directory holding initdb & co.
  PG_BIN_GLOBS     where the binaries are looked for when not on PATH
  PG_USER_ENV      $ALOELITE_BENCH_PG_USER, who runs postgres when we are root
  PG_START_WAIT_S  how long a start (including crash recovery) may take
  KILL_DELAY_RANGE when in a pgbench run the kill lands
  cfg keys         pg_scale, pg_clients, pg_seconds, pg_kill_rounds and
                   pg_modes (the volume axis) come from harness.SCALES

Fan-out points
  PG_FRONTENDS x cfg["pg_modes"] is the matrix; ext4 appears once. `_cell` is the
  one function that branches on frontend: how the data directory comes into
  being, and what a crash kills.
"""

from __future__ import annotations

import glob
import os
import pwd
import random
import re
import shutil
import signal
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ..corpus import FUSE_DAEMONS, fuse_session, rust_available
from ..harness import Row, Run, each, fuse_available, latency_detail

PG_FRONTENDS = ("ext4", "fuse", "rust-fuse")
PG_PORT = 55432
# Defaults except where a default would make the comparison unfair or the
# run unrepeatable. fsync stays ON: a cluster that does not fsync is not
# measuring the filesystem. autovacuum is off so a background vacuum cannot
# land inside one cell's timed window and not another's.
PG_SETTINGS = {
    "listen_addresses": "''",
    "autovacuum": "off",
    "fsync": "on",
    "synchronous_commit": "on",
    "full_page_writes": "on",
    "max_connections": "32",
}
PG_BIN_ENV = "ALOELITE_BENCH_PG_BIN"
PG_BIN_GLOBS = ("/usr/lib/postgresql/*/bin", "/usr/pgsql-*/bin")
PG_USER_ENV = "ALOELITE_BENCH_PG_USER"
PG_START_WAIT_S = 300
KILL_DELAY_RANGE = (1.0, 3.0)

_PG_TOOLS = ("initdb", "pg_ctl", "pgbench", "pg_amcheck", "psql")


# ---------------------------------------------------------------------------
# the suite
# ---------------------------------------------------------------------------
def postgres(run: Run, scratch: Path, cfg: dict) -> None:
    bindir = _pg_bindir()
    if bindir is None:
        run.skip(
            "postgres",
            f"PostgreSQL binaries not found (on PATH, in ${PG_BIN_ENV}, or under "
            f"{', '.join(PG_BIN_GLOBS)}); `apt-get install postgresql`",
        )
        return
    try:
        as_user = _pg_runner()
    except LookupError as exc:
        run.skip("postgres", str(exc))
        return

    frontends = [f for f in PG_FRONTENDS if f == "ext4" or fuse_available()]
    if not fuse_available():
        run.skip("postgres:fuse", "no FUSE (needs /dev/fuse, fusermount3, pyfuse3)")
    if "rust-fuse" in frontends and not rust_available():
        frontends.remove("rust-fuse")
        run.skip("postgres:rust-fuse", "`cargo build --release --bins` in rust/")

    if as_user.pw_uid != os.geteuid():
        # Another user has to walk down to PGDATA; open only what we made.
        for d in (scratch.parent, scratch):
            if d.stat().st_uid == os.geteuid():
                d.chmod(d.stat().st_mode | 0o711)
    if not _reachable(scratch, as_user):
        run.skip(
            "postgres",
            f"{as_user.pw_name} cannot reach {scratch}; pass --scratch somewhere "
            "every user can traverse",
        )
        return
    version = _run([str(bindir / "postgres"), "--version"]).stdout.strip()
    print(f"   {version}; postgres runs as {as_user.pw_name}", flush=True)

    cells = ["ext4/n/a"] + [
        f"{f}/{m}" for f in frontends if f != "ext4" for m in cfg["pg_modes"]
    ]
    failed = []
    for cell in each("postgres", cells):
        frontend, _, mode = cell.partition("/")
        workdir = scratch / cell.replace("/", "-")
        try:
            _cell(run, workdir, frontend, mode, cfg, bindir, as_user)
        except Exception as exc:
            # One cell's failure is that frontend's finding, not a reason to
            # lose the others' rows. The suite still fails at the end: a
            # cluster that cannot run on a mount is a bug, not a datapoint.
            run.skip(f"postgres:{cell}", f"{exc.__class__.__name__}: {exc}")
            failed.append(cell)
        finally:
            _unmount_quietly(workdir / "mnt")
    if failed:
        raise RuntimeError(f"postgres failed on {', '.join(failed)}")


def _cell(run, workdir, frontend, mode, cfg, bindir, as_user) -> None:
    """One frontend x volume: bring up a cluster, time it, crash it."""
    workdir.mkdir(parents=True)
    workdir.chmod(0o777)  # pgbench's per-transaction logs land here
    fs_file = workdir / "vol.fs"
    mp = workdir / "mnt"
    pg = _Cluster(bindir, as_user, workdir)
    label = {"suite": "postgres", "frontend": frontend, "volume": mode}

    with _hosting(frontend, mode, fs_file, mp, workdir, as_user) as (root, _daemon):
        pg.datadir = root / "pgdata"
        _make_datadir(pg.datadir, as_user)
        initdb_s = pg.initdb()
        run.add(Row(metric="pg_initdb", unit="s", value=initdb_s, **label))

        pg.start()
        init = pg.bench_init(cfg["pg_scale"])
        run.add(
            Row(
                metric="pg_bench_init",
                unit="s",
                value=init.pop("total_s"),
                detail=init,
                note=f"pgbench -i -s {cfg['pg_scale']}; detail is pgbench's own split",
                **label,
            )
        )
        for metric, select_only in (("pg_tpcb_tps", False), ("pg_select_tps", True)):
            tps, lat, failed = pg.bench(
                cfg["pg_clients"], cfg["pg_seconds"], select_only
            )
            run.add(
                Row(
                    metric=metric,
                    unit="tps",
                    value=tps,
                    n=len(lat),
                    detail={**latency_detail(lat), "failed": float(failed)},
                    note=f"{cfg['pg_clients']} clients, {cfg['pg_seconds']}s",
                    **label,
                )
            )
        pg.psql("CHECKPOINT")
        pg.stop()
        logical = pg.du(apparent=True)
        if frontend == "ext4":
            backing = pg.du(apparent=False)

    if frontend == "ext4":
        run.add(_space_row(label, logical, backing))
    else:
        run.add(_space_row(label, logical, *_volume_bytes(fs_file, mode)))

    _kill_loop(run, label, cfg, frontend, mode, fs_file, mp, workdir, pg, as_user)


def _kill_loop(run, label, cfg, frontend, mode, fs_file, mp, workdir, pg, as_user):
    """SIGKILL mid-pgbench, restart, recover, check every relation.

    On a mount the DAEMON dies: the claim under test is that what Postgres
    fsynced survived the filesystem process dying, which is the closest a
    benchmark gets to pulling the plug. ext4 cannot lose its kernel, so its
    baseline kills the postmaster instead and measures recovery alone.

    Runs after `_volume_bytes`, so every file already carries keep-last-1;
    that changes what a round costs in space, not what it can lose.
    """
    rng = random.Random(4242)
    recover, corrupt, refused = [], 0, 0
    for _ in range(cfg["pg_kill_rounds"]):
        with _hosting(frontend, mode, fs_file, mp, workdir, as_user) as (root, daemon):
            pg.datadir = root / "pgdata"
            pg.start()
            load = pg.bench_background(cfg["pg_clients"])
            time.sleep(rng.uniform(*KILL_DELAY_RANGE))
            # The daemon first (its death is the event under test), then the
            # whole cluster: without its filesystem it cannot shut down, and
            # the next start must find no survivors.
            _kill_cluster(pg.postmaster_pid, first=daemon)
            load.kill()
            load.wait(timeout=30)
        # No reaping between rounds: the next daemon retires the killed
        # one's mount row itself, because its recorded holder is gone (#15).

        # A real crash leaves postmaster.pid behind; postgres copes with it.
        with _hosting(frontend, mode, fs_file, mp, workdir, as_user) as (root, _d):
            pg.datadir = root / "pgdata"
            try:
                recover.append(pg.start())
            except RuntimeError:
                refused += 1
                continue
            corrupt += pg.amcheck()
            pg.stop()
    detail = latency_detail(recover)
    detail.update(
        {"refused_starts": float(refused), "rounds": float(cfg["pg_kill_rounds"])}
    )
    run.add(
        Row(
            metric="pg_crash_corrupt",
            unit="count",
            value=float(corrupt + refused),
            n=cfg["pg_kill_rounds"],
            detail=detail,
            note="pg_amcheck findings plus refused restarts; target is zero",
            **label,
        )
    )
    if recover:
        run.add(
            Row(
                metric="pg_crash_recovery",
                unit="ms",
                value=detail["p50_ms"],
                n=len(recover),
                detail=latency_detail(recover),
                note="pg_ctl start -w after the kill, crash recovery included",
                **label,
            )
        )


def _space_row(label, logical: int, backing: int, *reclaimed: int) -> Row:
    """`reclaimed` is (after prune as configured, after keep-last-1)."""
    detail = {"pgdata_bytes": float(logical), "backing_bytes": float(backing)}
    for key, after in zip(("after_prune", "after_keep1"), reclaimed):
        detail[f"{key}_bytes"] = float(after)
        detail[f"{key}_ratio"] = after / logical if logical else 0.0
    return Row(
        metric="pg_space_amplification",
        unit="ratio",
        value=backing / logical if logical else 0.0,
        detail=detail,
        note="bytes on the host disk per byte in PGDATA, after the bench",
        **label,
    )


# ---------------------------------------------------------------------------
# depth: where the data directory lives
# ---------------------------------------------------------------------------
@contextmanager
def _hosting(frontend, mode, fs_file, mp, workdir, as_user) -> Iterator[tuple]:
    """(directory PGDATA goes under, daemon or None): a plain directory, or
    a live mount and the process serving it.

    `--create` is idempotent, so the same call remounts the volume a killed
    daemon left behind. When postgres runs as another user (we are root;
    initdb refuses root) the daemon stays ours and the mount gets
    allow_other, the shape of a root daemon serving a container's uid.
    """
    if frontend == "ext4":
        root = workdir / "ext4"
        root.mkdir(exist_ok=True)
        root.chmod(0o777)
        yield root, None
        return
    other = as_user.pw_uid != os.geteuid()
    with fuse_session(
        fs_file, mode, mp, name="pg", impl=FUSE_DAEMONS[frontend], allow_other=other
    ) as daemon:
        if other:
            mp.chmod(0o777)
        yield mp, daemon


def _unmount_quietly(mp: Path) -> None:
    """A cell that raised mid-crash can leave a dead mount behind."""
    if os.path.ismount(mp):
        subprocess.run(["fusermount3", "-uz", str(mp)], capture_output=True)


def _make_datadir(datadir: Path, as_user) -> None:
    """PGDATA, owned by the cluster's user before initdb sees it.

    Made here rather than by initdb because initdb creates only the data
    directory itself, and its parent is the mount root, which the harness
    (root, when postgres runs as another user) owns. Everything below is
    created by postgres and owned by it, on every frontend.
    """
    datadir.mkdir()
    os.chown(datadir, as_user.pw_uid, as_user.pw_gid)
    datadir.chmod(0o700)


def _volume_bytes(fs_file: Path, mode: str) -> tuple[int, int, int]:
    """The volume's size after the bench; after prune + VACUUM as it stands;
    and after the same with every file under PGDATA set to keep one version.

    The second answers what a maintenance pass returns under the default
    retention, which is NULL: keep every superseded version (CV-6). Each
    fsync postgres issues commits one, so that number is the history. The
    third is the floor a keep-last-1 policy would reach, which is what
    tells history apart from live data. The daemon is unmounted, so this
    handle is alone.
    """
    from aloelite.aloelite import Aloelite
    from aloelite.types import NodeType

    from ..corpus import VOLUME_MODES, on_disk_bytes

    def reclaim() -> int:
        fs.prune(volume)
        fs.prune_content(volume)
        fs.db.connection.execute("VACUUM")
        return on_disk_bytes(fs, fs_file)

    fs = Aloelite(fs_file)
    try:
        before = on_disk_bytes(fs, fs_file)
        volume = fs.resolve_volume_name("pg")
        as_is = reclaim()
        with fs.mount(volume, pin=VOLUME_MODES[mode]["pin"]) as m:
            stack = ["/"]
            while stack:
                here = stack.pop()
                for e in m.list(here):
                    path = f"{here.rstrip('/')}/{e.name}"
                    if e.type == NodeType.CONTAINER:
                        stack.append(path)
                    elif e.type == NodeType.ENTRY:
                        m.set_retention(path, 1)
        keep1 = reclaim()
    finally:
        fs.close()
    return before, as_is, keep1


# ---------------------------------------------------------------------------
# depth: one cluster, driven through its own command-line tools
# ---------------------------------------------------------------------------
class _Cluster:
    def __init__(self, bindir: Path, as_user, workdir: Path) -> None:
        self.bindir = bindir
        self.as_user = as_user
        self.workdir = workdir
        self.datadir: Path = workdir
        # A short, private socket directory: a Unix socket path is capped at
        # 107 bytes, and a scratch directory can be longer than that.
        self.sockdir = Path(tempfile.mkdtemp(prefix="aloepg-"))
        self.sockdir.chmod(0o777)
        self.postmaster_pid = 0

    def _cmd(self, tool: str, *args: str) -> list[str]:
        argv = [str(self.bindir / tool), *args]
        if self.as_user.pw_uid != os.geteuid():
            argv = ["runuser", "-u", self.as_user.pw_name, "--", *argv]
        return argv

    def _conn(self) -> list[str]:
        return ["-h", str(self.sockdir), "-p", str(PG_PORT), "-U", "bench"]

    def initdb(self) -> float:
        start = time.perf_counter()
        _run(self._cmd("initdb", "-D", str(self.datadir), "-A", "trust", "-U", "bench"))
        return time.perf_counter() - start

    def start(self) -> float:
        opts = " ".join(f"-c {k}={v}" for k, v in PG_SETTINGS.items())
        opts += f" -k {self.sockdir} -p {PG_PORT}"
        log = self.workdir / "postgres.log"
        start = time.perf_counter()
        done = subprocess.run(
            self._cmd(
                "pg_ctl",
                "-D",
                str(self.datadir),
                "-l",
                str(log),
                "-o",
                opts,
                "-t",
                str(PG_START_WAIT_S),
                "-w",
                "start",
            ),
            capture_output=True,
            text=True,
        )
        elapsed = time.perf_counter() - start
        if done.returncode != 0:
            tail = log.read_text(errors="replace")[-2000:] if log.exists() else ""
            raise RuntimeError(
                f"pg_ctl start failed:\n{done.stdout}{done.stderr}\n{tail}"
            )
        pidfile = (self.datadir / "postmaster.pid").read_text().split()
        self.postmaster_pid = int(pidfile[0])
        return elapsed

    def stop(self) -> None:
        _run(self._cmd("pg_ctl", "-D", str(self.datadir), "-m", "fast", "-w", "stop"))

    def du(self, apparent: bool) -> int:
        """Bytes under PGDATA, read as the cluster's user (the mount is
        theirs alone). Apparent is what postgres wrote; otherwise what the
        disk allocated, which is the ext4 baseline's backing size."""
        argv = ["du", "-s", "-B1", *(["--apparent-size"] if apparent else [])]
        argv.append(str(self.datadir))
        if self.as_user.pw_uid != os.geteuid():
            argv = ["runuser", "-u", self.as_user.pw_name, "--", *argv]
        return int(_run(argv).stdout.split()[0])

    def psql(self, sql: str) -> str:
        return _run(self._cmd("psql", *self._conn(), "-qAtc", sql, "postgres")).stdout

    def bench_init(self, scale: int) -> dict[str, float]:
        out = _run(
            self._cmd("pgbench", *self._conn(), "-i", "-s", str(scale), "postgres")
        )
        return _parse_init(out.stdout + out.stderr)

    def bench(self, clients: int, seconds: float, select_only: bool):
        """tps, per-transaction latencies in seconds, failed count."""
        logdir = self.workdir / ("select" if select_only else "tpcb")
        shutil.rmtree(logdir, ignore_errors=True)
        logdir.mkdir()
        logdir.chmod(0o777)
        args = [
            *self._conn(),
            "-c",
            str(clients),
            "-j",
            str(clients),
            "-T",
            str(int(seconds)),
            "-l",
            f"--log-prefix={logdir}/tx",
            "postgres",
        ]
        if select_only:
            args.insert(0, "-S")
        out = _run(self._cmd("pgbench", *args), cwd=self.workdir)
        text = out.stdout + out.stderr
        tps = float(re.search(r"tps = ([\d.]+)", text).group(1))
        failed = re.search(r"number of failed transactions: (\d+)", text)
        return tps, _tx_latencies(logdir), int(failed.group(1)) if failed else 0

    def bench_background(self, clients: int) -> subprocess.Popen:
        """A pgbench that runs until it is killed, as the crash's victim."""
        return subprocess.Popen(
            self._cmd(
                "pgbench", *self._conn(), "-c", str(clients), "-T", "3600", "postgres"
            ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def amcheck(self) -> int:
        """Findings from pg_amcheck over every heap and btree, 0 when clean."""
        done = subprocess.run(
            self._cmd(
                "pg_amcheck",
                *self._conn(),
                "--install-missing",
                "--heapallindexed",
                "--all",
            ),
            capture_output=True,
            text=True,
        )
        if done.returncode == 0:
            return 0
        findings = [ln for ln in done.stdout.splitlines() if ln.strip()]
        return max(1, len(findings))


def _kill_cluster(postmaster: int, first: subprocess.Popen | None = None) -> None:
    """SIGKILL `first` (if any), the postmaster, and every process it started.

    The children are listed before anything dies: once the postmaster is
    gone they are re-parented, and `pgrep -P` would no longer find them.
    """
    children = subprocess.run(
        ["pgrep", "-P", str(postmaster)], capture_output=True, text=True
    ).stdout.split()
    if first is not None:
        os.kill(first.pid, signal.SIGKILL)
    for pid in [postmaster, *map(int, children)]:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            os.kill(postmaster, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)


def _parse_init(text: str) -> dict[str, float]:
    """`done in 1.23 s (drop tables 0.00 s, create tables 0.01 s, ...)`."""
    m = re.search(r"done in ([\d.]+) s \(([^)]*)\)", text)
    if m is None:
        raise RuntimeError(f"unrecognised pgbench -i output:\n{text[-2000:]}")
    out = {"total_s": float(m.group(1))}
    for step, secs in re.findall(r"([a-z -]+?) ([\d.]+) s", m.group(2)):
        out[step.strip().replace(" ", "_").replace("-", "_") + "_s"] = float(secs)
    return out


def _tx_latencies(logdir: Path) -> list[float]:
    """pgbench -l: `client tx_no latency_us script epoch_s epoch_us` per line."""
    samples = []
    for path in logdir.glob("tx*"):
        for line in path.read_text().splitlines():
            fields = line.split()
            if len(fields) >= 3 and fields[2].isdigit():
                samples.append(int(fields[2]) / 1e6)
    return samples


# ---------------------------------------------------------------------------
# depth: finding the binaries and the user that runs them
# ---------------------------------------------------------------------------
def _pg_bindir() -> Path | None:
    candidates = []
    if os.environ.get(PG_BIN_ENV):
        candidates.append(Path(os.environ[PG_BIN_ENV]))
    on_path = shutil.which("initdb")
    if on_path:
        candidates.append(Path(on_path).resolve().parent)
    for pattern in PG_BIN_GLOBS:
        # Highest major version first: 16 before 9.6 needs a numeric sort.
        found = glob.glob(pattern)
        found.sort(key=lambda p: [int(x) for x in re.findall(r"\d+", p)], reverse=True)
        candidates += [Path(p) for p in found]
    for d in candidates:
        if all((d / t).is_file() for t in (*_PG_TOOLS, "postgres")):
            return d
    return None


def _pg_runner():
    """Who runs postgres. Ourselves, unless we are root, which initdb refuses."""
    if os.geteuid() != 0:
        return pwd.getpwuid(os.geteuid())
    name = os.environ.get(PG_USER_ENV, "postgres")
    try:
        return pwd.getpwnam(name)
    except KeyError:
        raise LookupError(
            f"running as root, and initdb refuses root; no user {name!r} "
            f"to run postgres as (set ${PG_USER_ENV})"
        ) from None


def _reachable(path: Path, as_user) -> bool:
    if as_user.pw_uid == os.geteuid():
        return True
    probe = ["runuser", "-u", as_user.pw_name, "--", "test", "-x", str(path)]
    return subprocess.run(probe, capture_output=True).returncode == 0


def _run(argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    done = subprocess.run(argv, capture_output=True, text=True, cwd=cwd)
    if done.returncode != 0:
        raise RuntimeError(
            f"{' '.join(argv)} exited {done.returncode}:\n{done.stdout[-2000:]}"
            f"{done.stderr[-2000:]}"
        )
    return done


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
