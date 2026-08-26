"""Benchmarks for the numbers that back 0.4's design claims.

Not a test: results vary by machine, so nothing here asserts. Run it when a
release needs figures, or when a change might have moved one of these.

    python script/benchmark.py                  # everything except FUSE
    python script/benchmark.py --fuse           # add mount throughput
    python script/benchmark.py --only resolve   # one section

Sections:

  resolve   The claim from HANDOFF-0.4 §4.2 — "if only one thing from this
            document gets done, make it this one". Compares the era-2
            single-query CTE against the per-segment fold it replaced, at
            increasing depth, both locally and with a simulated per-query
            network latency. Local numbers understate the win by design;
            the latency column is the real argument.

  content   Engine write/read throughput against raw file I/O on the same
            disk, unencrypted and convergent-encrypted, so the cost of the
            chunk pool and of encryption are separated.

  dedup     What convergent addressing buys when the same bytes arrive
            twice (CV-1/CV-2).

  mount     (--fuse) The same content path through a real kernel mount
            against a plain directory — what a user actually feels.
"""

from __future__ import annotations

import argparse
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aloelite.aloelite import Aloelite  # noqa: E402
from aloelite.resolve import resolve as cte_resolve  # noqa: E402
from aloelite.resolve import split_path  # noqa: E402

MB = 1 << 20


def _fmt_rate(nbytes: int, seconds: float) -> str:
    return f"{nbytes / MB / seconds:8.1f} MB/s" if seconds > 0 else "       inf"


def _fmt_us(seconds: float) -> str:
    return f"{seconds * 1e6:9.1f} us"


def _best_of(fn, rounds: int = 5) -> float:
    """Minimum wall time over `rounds` — the least noise-contaminated sample,
    which is what you want when comparing two implementations of one thing."""
    return min(_timed(fn) for _ in range(rounds))


def _timed(fn) -> float:
    start = time.perf_counter()
    fn()
    return time.perf_counter() - start


def _drop_cache(path: Path) -> None:
    """Ask the kernel to forget this file's pages, so the next read measures
    storage (or the FUSE daemon) instead of memory. The same discipline
    tests/test_fuse_mount.py documents: without it, a read benchmark reports
    the page cache's speed and calls it the filesystem's."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    finally:
        os.close(fd)


def _spread(samples: list[float], nbytes: int) -> str:
    """median rate, with the min/max range — throughput on a shared or
    virtualized host varies by multiples, and a single figure hides that."""
    rates = sorted(nbytes / MB / s for s in samples if s > 0)
    if not rates:
        return "n/a"
    med = statistics.median(rates)
    return f"{med:7.1f} MB/s  ({rates[0]:.0f}-{rates[-1]:.0f})"


# ---------------------------------------------------------------------------
# resolve: one query vs one query per segment
# ---------------------------------------------------------------------------
def _fold_resolve(db, root: str, path: str) -> str:
    """The pre-0.4 resolver, reconstructed from the template that still backs
    single-step lookups. This is also what a naive port writes first, which
    is the other reason to publish the number."""
    node = root
    for seg in split_path(path):
        row = db.one("resolution.resolve_segment", {"container": node, "name": seg})
        if row is None:
            raise KeyError(seg)
        node = row["node_id"]
    return node


def bench_resolve(depths=(1, 4, 8, 16), latency_ms: float = 1.0) -> None:
    print("\n== resolve: single-query CTE vs per-segment fold ==")
    print(f"   (simulated latency column: {latency_ms:.1f} ms per round trip)")
    print(
        f"\n{'depth':>6} {'CTE':>13} {'fold':>13} {'speedup':>9}"
        f" {'CTE+lat':>11} {'fold+lat':>11} {'speedup':>9}"
    )
    with tempfile.TemporaryDirectory() as td:
        fs = Aloelite(Path(td) / "bench.fs")
        try:
            vol = fs.create_volume("bench", enc_mode="none").id
            m = fs.mount(vol)
            db = fs.db
            root = m.info().mount_point

            for depth in depths:
                path = "/" + "/".join(f"d{i}" for i in range(depth))
                built = ""
                for i in range(depth):
                    built += f"/d{i}"
                    m.create_container(built)

                # Resolver against resolver: both start at the mount point and
                # end at the node. Timing m.stat() instead would add mount
                # validation and get_node to one side only.
                cte = _best_of(lambda p=path: cte_resolve(db, root, p))
                fold = _best_of(lambda p=path: _fold_resolve(db, root, p))
                # a fold pays the latency once per segment; the CTE once total
                lat = latency_ms / 1000.0
                cte_net = cte + lat
                fold_net = fold + depth * lat
                print(
                    f"{depth:>6} {_fmt_us(cte)} {_fmt_us(fold)}"
                    f" {fold / cte:8.2f}x {cte_net * 1e3:9.1f} ms"
                    f" {fold_net * 1e3:9.1f} ms {fold_net / cte_net:8.2f}x"
                )
        finally:
            fs.close()


# ---------------------------------------------------------------------------
# content throughput
# ---------------------------------------------------------------------------
def bench_content(size_mb: int = 32, rounds: int = 5) -> None:
    print(f"\n== content: {size_mb} MB through the engine vs raw file I/O ==")
    print(
        f"   median of {rounds} rounds (range in parens); writes are fsynced and\n"
        "   caches dropped before reads, so these are storage numbers, not\n"
        "   page-cache numbers.\n"
    )
    payload = os.urandom(size_mb * MB)  # incompressible, undedupable
    n = len(payload)

    def _fsync_write(path: Path) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        raw = tmp / "raw.bin"
        writes, reads = [], []
        for _ in range(rounds):
            writes.append(_timed(lambda: _fsync_write(raw)))
            _drop_cache(raw)
            reads.append(_timed(lambda: raw.read_bytes()))
        print(f"  raw file            write {_spread(writes, n)}")
        print(f"                      read  {_spread(reads, n)}")

        for label, kwargs in [
            ("engine (plain)", {"enc_mode": "none"}),
            (
                "engine (convergent)",
                {"pin": b"benchmark-pin", "enc_mode": "convergent"},
            ),
        ]:
            writes, reads = [], []
            for i in range(rounds):
                ref = tmp / f"{label.split()[1]}-{i}.fs"
                fs = Aloelite(ref)
                try:
                    vol = fs.create_volume("bench", **kwargs).id
                    m = fs.mount(vol, pin=kwargs.get("pin"))
                    writes.append(_timed(lambda: m.create_entry("/big.bin", payload)))
                    fs.db.connection.execute("PRAGMA wal_checkpoint(FULL)")
                    _drop_cache(ref)
                    got = None

                    def _read():
                        nonlocal got
                        got = m.read_all("/big.bin")

                    reads.append(_timed(_read))
                    assert got == payload, f"{label}: round-trip mismatch"
                finally:
                    fs.close()
                ref.unlink(missing_ok=True)
            print(f"  {label:<19} write {_spread(writes, n)}")
            print(f"                      read  {_spread(reads, n)}")


# ---------------------------------------------------------------------------
# dedup
# ---------------------------------------------------------------------------
def bench_dedup(size_mb: int = 32) -> None:
    print(f"\n== dedup: the same {size_mb} MB written twice (convergent) ==\n")
    payload = os.urandom(size_mb * MB)
    with tempfile.TemporaryDirectory() as td:
        ref = Path(td) / "dedup.fs"
        fs = Aloelite(ref)
        try:
            vol = fs.create_volume("bench", pin=b"pin", enc_mode="convergent").id
            m = fs.mount(vol, pin=b"pin")
            first = _timed(lambda: m.create_entry("/a.bin", payload))
            size_after_first = ref.stat().st_size
            second = _timed(lambda: m.create_entry("/b.bin", payload))
            size_after_second = ref.stat().st_size
            chunks = fs.db.connection.execute(
                "SELECT count(*) FROM content_chunk"
            ).fetchone()[0]
        finally:
            fs.close()
    grew = size_after_second - size_after_first
    print(
        f"  first  write        {first:6.2f} s   "
        f"file now {size_after_first / MB:8.1f} MB"
    )
    print(f"  second write        {second:6.2f} s   file grew {grew / MB:8.3f} MB")
    print(f"  pool rows           {chunks} (one copy of the bytes, two entries)")


# ---------------------------------------------------------------------------
# mount throughput (needs /dev/fuse)
# ---------------------------------------------------------------------------
def bench_mount(size_mb: int = 32) -> None:
    print(f"\n== mount: {size_mb} MB through a kernel mount vs a plain directory ==\n")
    if not os.path.exists("/dev/fuse") or shutil.which("fusermount3") is None:
        print("  skipped: needs /dev/fuse and fusermount3")
        return
    payload = os.urandom(size_mb * MB)
    n = len(payload)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        plain = tmp / "plain"
        plain.mkdir()
        w = _timed(lambda: (plain / "f.bin").write_bytes(payload))
        r = _timed(lambda: (plain / "f.bin").read_bytes())
        print(f"  plain directory     write {_fmt_rate(n, w)}   read {_fmt_rate(n, r)}")

        mp = tmp / "mnt"
        mp.mkdir()
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from aloelite.fuse import main; main()",
                "-f",
                str(tmp / "mount.fs"),
                "-v",
                "bench",
                "--create",
                str(mp),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not os.path.ismount(mp):
            if proc.poll() is not None:
                print("  mount failed:", proc.stdout.read().decode(errors="replace"))
                return
            time.sleep(0.1)
        try:
            target = mp / "f.bin"
            w = _timed(lambda: target.write_bytes(payload))
            # drop the page cache so the read measures the daemon, not memory
            fd = os.open(target, os.O_RDONLY)
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            os.close(fd)
            r = _timed(lambda: target.read_bytes())
            print(
                f"  aloelite mount      write {_fmt_rate(n, w)}"
                f"   read {_fmt_rate(n, r)}"
            )
        finally:
            subprocess.run(["fusermount3", "-u", str(mp)], capture_output=True)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", choices=["resolve", "content", "dedup", "mount"])
    ap.add_argument("--fuse", action="store_true", help="include the mount section")
    ap.add_argument("--size-mb", type=int, default=32)
    ap.add_argument("--latency-ms", type=float, default=1.0)
    args = ap.parse_args()

    import platform

    print(
        f"aloelite benchmark — python {platform.python_version()} "
        f"on {platform.platform()}"
    )
    sections = {
        "resolve": lambda: bench_resolve(latency_ms=args.latency_ms),
        "content": lambda: bench_content(args.size_mb),
        "dedup": lambda: bench_dedup(args.size_mb),
        "mount": lambda: bench_mount(args.size_mb),
    }
    if args.only:
        sections[args.only]()
        return
    for name in ("resolve", "content", "dedup"):
        sections[name]()
    if args.fuse:
        sections["mount"]()


if __name__ == "__main__":
    main()
