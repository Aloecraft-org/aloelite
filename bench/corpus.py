# ./bench/corpus.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
The two things every suite needs: somewhere to put bytes (a backend) and
bytes with a known shape (a corpus).

A backend is the frontend-under-test reduced to the handful of operations
the suites actually time. Two exist, because two ship: the Python library
API (`direct`) and a real kernel mount (`fuse`). `ext4` is the same POSIX
backend pointed at a plain directory on the same disk, which is how a row
gets a baseline rather than an absolute number nobody can place.

Surface
-------
Entry points
  engine_session(...)      an open Aloelite + Mount for a volume mode
  fuse_session(...)        a live kernel mount, unmounted on exit
  posix_backend(path)      ext4 / fuse / gocryptfs, all the same POSIX ops
  backends_for(...)        the (frontend, volume) matrix a suite iterates
  block_source(...)        streaming corpus bytes, never materialized whole
  maildir_tree(...)        the small-file shape production actually has
  on_disk_bytes(...)       file size after a checkpoint, plus page accounting

Configurable values
  VOLUME_MODES     plain | convergent | random, and what each means
  MOUNT_WAIT_S     how long a FUSE mount gets to appear
  BENCH_PIN        the PIN encrypted benchmark volumes use
  STREAM_BLOCK     the unit of every streaming read and write here

Fan-out points
  VOLUME_MODES is the volume axis. Backend is the frontend axis and has
  exactly two implementations, EngineBackend and PosixBackend; a third
  frontend is a third entry in `backends_for` and nothing else.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from aloelite.aloelite import Aloelite, Mount

from .harness import MiB

BENCH_PIN = b"benchmark-pin-not-a-secret"
MOUNT_WAIT_S = 30.0
STREAM_BLOCK = 4 * MiB

# The volume axis. `random` sacrifices dedup for zero equality leakage, so it
# only appears in the space suite, where that trade-off is the measurement.
VOLUME_MODES: dict[str, dict] = {
    "plain": {"enc_mode": "none", "pin": None},
    "convergent": {"enc_mode": "convergent", "pin": BENCH_PIN},
    "random": {"enc_mode": "random", "pin": BENCH_PIN},
}


# ---------------------------------------------------------------------------
# backends
# ---------------------------------------------------------------------------
class Backend:
    """The operations the suites time, and nothing else.

    Paths are always '/'-rooted and relative to the backend's own root, so a
    suite writes one loop and runs it against every frontend.
    """

    name = "?"
    volume = "-"
    backing: tuple[Path, ...] = ()

    def host_files(self, path: str) -> list[Path]:
        """Host-visible files whose page cache a cold read must drop.

        Not the same as the backend's own path: a cold read through a FUSE
        mount has to forget BOTH the kernel's pages for the mounted inode and
        the daemon's pages for the .fs file behind it. Dropping one and
        calling the result cold is how a benchmark measures memory.
        """
        raise NotImplementedError

    def durable(self, path: str) -> None:
        """The barrier: when this returns, the bytes are on the platter.

        Every backend implements the SAME guarantee, and every throughput
        row includes it, because otherwise the rows are not comparable.
        ext4's default write is buffered; the engine's commit lands in a WAL
        at `synchronous=NORMAL`, which is a commit but not an fsync. Timing
        one against the other without this barrier flatters whichever side
        happens to be allowed to lie about durability.
        """
        raise NotImplementedError

    def go_cold(self, path: str) -> None:
        """Durable, then forget: the next read comes from storage."""
        from .harness import drop_cache

        self.durable(path)
        drop_cache(*self.host_files(path))

    def write(self, path: str, data: bytes) -> None:
        raise NotImplementedError

    def write_stream(self, path: str, blocks: Iterator[bytes]) -> int:
        raise NotImplementedError

    def read(self, path: str) -> bytes:
        raise NotImplementedError

    def read_stream(self, path: str) -> int:
        """Whole file, discarding bytes: returns the count read. Suites that
        measure read throughput on multi-GB files cannot hold the result."""
        raise NotImplementedError

    def pread(self, path: str, offset: int, length: int) -> bytes:
        raise NotImplementedError

    def append(self, path: str, data: bytes) -> None:
        raise NotImplementedError

    def mkdir(self, path: str) -> None:
        raise NotImplementedError

    def listdir(self, path: str) -> list[str]:
        raise NotImplementedError

    def stat_size(self, path: str) -> int:
        raise NotImplementedError

    def unlink(self, path: str) -> None:
        raise NotImplementedError


class EngineBackend(Backend):
    """The Python library API, in process, no kernel in the path."""

    name = "direct"

    def __init__(self, fs: Aloelite, mount: Mount, volume_mode: str, fs_file: Path):
        self.fs = fs
        self.m = mount
        self.volume = volume_mode
        self.fs_file = fs_file
        self.backing = _sqlite_files(fs_file)

    def host_files(self, path: str) -> list[Path]:
        return [p for p in self.backing if p.exists()]

    def durable(self, path: str) -> None:
        conn = self.fs.db.connection
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        _fsync_path(self.fs_file)

    def go_cold(self, path: str) -> None:
        """Also drops sqlite's OWN page cache, which posix_fadvise cannot
        reach. Without it a 'cold' read on the connection that just wrote the
        bytes is served from this process's memory.

        The FUSE rows cannot do this — the daemon's connection is in another
        process — so a cold FUSE read still has a warm sqlite cache behind
        it. That asymmetry favours FUSE and is stated rather than hidden.
        """
        super().go_cold(path)
        self.fs.db.connection.execute("PRAGMA shrink_memory")

    def write(self, path: str, data: bytes) -> None:
        self.m.create_entry(path, data)

    def write_stream(self, path: str, blocks: Iterator[bytes]) -> int:
        total = 0
        with self.m.open_write(path) as fh:
            for block in blocks:
                fh.write(block)
                total += len(block)
        return total

    def read(self, path: str) -> bytes:
        return self.m.read_all(path)

    def read_stream(self, path: str) -> int:
        total = 0
        with self.m.open_read(path) as fh:
            while block := fh.read(STREAM_BLOCK):
                total += len(block)
        return total

    def pread(self, path: str, offset: int, length: int) -> bytes:
        with self.m.open_read(path) as fh:
            fh.seek(offset)
            return fh.read(length)

    def append(self, path: str, data: bytes) -> None:
        self.m.append(path, data)

    def mkdir(self, path: str) -> None:
        self.m.mkdir(path, parents=True, exist_ok=True)

    def listdir(self, path: str) -> list[str]:
        return [e.name for e in self.m.list(path)]

    def stat_size(self, path: str) -> int:
        return self.m.stat(path).size or 0

    def unlink(self, path: str) -> None:
        self.m.remove(path)


class PosixBackend(Backend):
    """A directory. Serves ext4 (the baseline), a live aloelite FUSE mount,
    and a gocryptfs mount — the point being that they are byte-identical
    workloads, which is the only way the comparison means anything."""

    def __init__(
        self,
        root: Path,
        name: str,
        volume_mode: str = "-",
        backing: tuple[Path, ...] = (),
    ) -> None:
        self.root = root
        self.name = name
        self.volume = volume_mode
        self.backing = backing

    def host_files(self, path: str) -> list[Path]:
        own = self._p(path)
        return [p for p in (own, *self.backing) if p.exists()]

    def durable(self, path: str) -> None:
        """fsync the file and its directory. On a FUSE mount the fsync is
        what makes the daemon commit, so this is the mount's real durability
        point and not an approximation of it."""
        _fsync_path(self._p(path))
        _fsync_path(self.root, directory=True)

    def _p(self, path: str) -> Path:
        return self.root / path.lstrip("/")

    def write(self, path: str, data: bytes) -> None:
        target = self._p(path)
        with open(target, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())

    def write_stream(self, path: str, blocks: Iterator[bytes]) -> int:
        total = 0
        with open(self._p(path), "wb") as fh:
            for block in blocks:
                fh.write(block)
                total += len(block)
        return total

    def read(self, path: str) -> bytes:
        return self._p(path).read_bytes()

    def read_stream(self, path: str) -> int:
        total = 0
        with open(self._p(path), "rb") as fh:
            while block := fh.read(STREAM_BLOCK):
                total += len(block)
        return total

    def pread(self, path: str, offset: int, length: int) -> bytes:
        with open(self._p(path), "rb") as fh:
            fh.seek(offset)
            return fh.read(length)

    def append(self, path: str, data: bytes) -> None:
        with open(self._p(path), "ab") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())

    def mkdir(self, path: str) -> None:
        self._p(path).mkdir(parents=True, exist_ok=True)

    def listdir(self, path: str) -> list[str]:
        return os.listdir(self._p(path))

    def stat_size(self, path: str) -> int:
        return self._p(path).stat().st_size

    def unlink(self, path: str) -> None:
        self._p(path).unlink()


# ---------------------------------------------------------------------------
# sessions
# depth: volume creation, FUSE daemon lifecycle
# ---------------------------------------------------------------------------
@contextmanager
def engine_session(
    fs_file: Path, mode: str, name: str = "bench"
) -> Iterator[tuple[Aloelite, Mount]]:
    """An Aloelite handle plus a mounted session on a fresh volume."""
    spec = VOLUME_MODES[mode]
    fs = Aloelite(fs_file)
    try:
        vol = fs.create_volume(name, pin=spec["pin"], enc_mode=spec["enc_mode"])
        mount = fs.mount(vol.id, pin=spec["pin"])
        try:
            yield fs, mount
        finally:
            mount.unmount()
    finally:
        fs.close()


@contextmanager
def attach_session(
    fs_file: Path, mode: str, name: str = "bench", mount_retries: int = 40
) -> Iterator[tuple[Aloelite, Mount, int]]:
    """Open an EXISTING volume by name and mount it, retrying the mount.

    Mounting is a write — it inserts a mount row — so a client that mounts
    while another process holds the write lock gets SQLITE_BUSY once the
    busy_timeout expires. Real clients retry; a benchmark worker that did not
    would report zero throughput and call it contention. The retry count is
    returned so it can be reported rather than hidden.
    """
    import sqlite3

    spec = VOLUME_MODES[mode]
    fs = Aloelite(fs_file)
    try:
        vol = fs.resolve_volume_name(name)
        if vol is None:
            raise LookupError(f"no volume named {name!r} in {fs_file}")
        retries = 0
        while True:
            try:
                mount = fs.mount(vol, pin=spec["pin"])
                break
            except sqlite3.OperationalError:
                retries += 1
                if retries > mount_retries:
                    raise
                time.sleep(0.05 * retries)
        try:
            yield fs, mount, retries
        finally:
            mount.unmount()
    finally:
        fs.close()


@contextmanager
def fuse_session(
    fs_file: Path, mode: str, mountpoint: Path, name: str = "bench"
) -> Iterator[subprocess.Popen]:
    """A live kernel mount of a fresh volume, torn down on exit.

    The daemon is a subprocess on purpose: that is how a user runs it, its RSS
    is then separately observable (the streaming-write claim is about the
    daemon's memory, not the caller's), and a wedged mount cannot take the
    benchmark process down with it.
    """
    spec = VOLUME_MODES[mode]
    mountpoint.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    argv = [
        sys.executable,
        "-c",
        "from aloelite.fuse import main; main()",
        "-f",
        str(fs_file),
        "-v",
        name,
        "--create",
        str(mountpoint),
    ]
    if spec["pin"] is not None:
        env["ALOELITE_BENCH_PIN"] = spec["pin"].decode()
        argv += ["--pin-env", "ALOELITE_BENCH_PIN"]
    proc = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env
    )
    deadline = time.monotonic() + MOUNT_WAIT_S
    while time.monotonic() < deadline and not os.path.ismount(mountpoint):
        if proc.poll() is not None:
            out = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
            raise RuntimeError(f"aloelite-fuse exited {proc.returncode}:\n{out}")
        time.sleep(0.05)
    if not os.path.ismount(mountpoint):
        proc.terminate()
        raise RuntimeError(f"mount did not appear within {MOUNT_WAIT_S}s")
    try:
        yield proc
    finally:
        unmount(mountpoint, proc)


def unmount(mountpoint: Path, proc: subprocess.Popen | None = None) -> None:
    subprocess.run(["fusermount3", "-u", str(mountpoint)], capture_output=True)
    if proc is None:
        return
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:  # pragma: no cover - hung daemon
        subprocess.run(["fusermount3", "-uz", str(mountpoint)], capture_output=True)
        proc.kill()
        proc.wait(timeout=10)


@contextmanager
def backends_for(
    scratch: Path,
    frontends: tuple[str, ...] = ("ext4", "direct", "fuse"),
    modes: tuple[str, ...] = ("plain", "convergent"),
) -> Iterator[list[Backend]]:
    """The (frontend, volume) matrix, all live at once so every row in a
    suite is measured on the same machine in the same minute.

    ext4 has no volume axis: it appears once, as the baseline that separates
    aloelite's overhead from the hardware's.

    There is no third frontend here because there is no third frontend: the
    engine is Python, and nothing in this tree implements the Mount API in
    another language. When one exists it becomes one more branch below.
    """
    from contextlib import ExitStack

    with ExitStack() as stack:
        out: list[Backend] = []
        if "ext4" in frontends:
            base = scratch / "baseline"
            base.mkdir(parents=True, exist_ok=True)
            out.append(PosixBackend(base, "ext4"))
        for mode in modes:
            if "direct" in frontends:
                fs_file = scratch / f"direct-{mode}.fs"
                fs, mount = stack.enter_context(engine_session(fs_file, mode))
                out.append(EngineBackend(fs, mount, mode, fs_file))
            if "fuse" in frontends:
                fs_file = scratch / f"fuse-{mode}.fs"
                mp = scratch / f"mnt-{mode}"
                stack.enter_context(fuse_session(fs_file, mode, mp))
                out.append(PosixBackend(mp, "fuse", mode, _sqlite_files(fs_file)))
        yield out


# ---------------------------------------------------------------------------
# corpora
# ---------------------------------------------------------------------------
def block_source(
    nbytes: int, kind: str = "unique", block: int = STREAM_BLOCK, seed: int = 1
) -> Iterator[bytes]:
    """Stream `nbytes` of a known shape without ever holding them.

    unique   incompressible and undedupable — the honest throughput corpus
    dup      one random block repeated — what a template-cloned tree looks
             like to a content-addressed pool
    zero     what a sparse disk image is mostly made of
    """
    import random

    rng = random.Random(seed)
    written = 0
    fixed = rng.randbytes(block) if kind == "dup" else b""
    while written < nbytes:
        size = min(block, nbytes - written)
        if kind == "unique":
            chunk = rng.randbytes(size)
        elif kind == "dup":
            chunk = fixed[:size]
        elif kind == "zero":
            chunk = bytes(size)
        else:  # pragma: no cover - programming error
            raise ValueError(f"unknown corpus kind {kind!r}")
        written += size
        yield chunk


def payload(nbytes: int, kind: str = "unique", seed: int = 1) -> bytes:
    """Materialized corpus, for the sizes small enough to hold."""
    return b"".join(block_source(nbytes, kind=kind, seed=seed))


def maildir_tree(
    files: int, dirs: int, size: int = 4096, seed: int = 7
) -> list[tuple[str, bytes]]:
    """A maildir-shaped tree: many small files spread over a few directories,
    each a distinct few-KB message. This is the shape the small-file numbers
    are about, and it is not the same workload as one big file: the cost is
    per-operation, not per-byte."""
    import random

    rng = random.Random(seed)
    out = []
    for i in range(files):
        d = i % dirs
        body = rng.randbytes(size)
        out.append((f"/cur/d{d:03d}/{i:06d}.msg", body))
    return out


SPARSE_BLOCK = 1 * MiB


def sparse_image(nbytes: int, live_fraction: float = 0.05, seed: int = 11):
    """A mostly-zero disk image: `live_fraction` of the blocks carry data and
    the rest are zeros, which is what qemu hands you for a fresh guest disk.

    Blocks are SPARSE_BLOCK, chosen to match the default chunk size: a zero
    region smaller than a chunk still costs a whole distinct chunk, so a
    block size finer than the chunk would measure the generator's geometry
    rather than the pool's behaviour.
    """
    import random

    rng = random.Random(seed)
    blocks = max(1, nbytes // SPARSE_BLOCK)
    live = {rng.randrange(blocks) for _ in range(max(1, int(blocks * live_fraction)))}
    for i in range(blocks):
        yield rng.randbytes(SPARSE_BLOCK) if i in live else bytes(SPARSE_BLOCK)


# ---------------------------------------------------------------------------
# space accounting
# ---------------------------------------------------------------------------
def on_disk_bytes(fs: Aloelite, fs_file: Path) -> int:
    """The number a user sees in `ls -l`, after folding the WAL back in.

    Without the checkpoint this reports the main file while the bytes are
    still in the -wal, which understates a fresh write by however much has
    not been checkpointed yet.
    """
    fs.db.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    total = fs_file.stat().st_size
    for suffix in ("-wal", "-shm"):
        side = Path(str(fs_file) + suffix)
        if side.exists():
            total += side.stat().st_size
    return total


def page_accounting(fs: Aloelite) -> dict[str, int]:
    """page_count / freelist_count / page_size — the split between space a
    prune FREED (pages on the freelist, still inside the file) and space it
    RETURNED (file shrink, which only VACUUM does)."""
    conn = fs.db.connection
    out = {}
    for pragma in ("page_count", "freelist_count", "page_size"):
        out[pragma] = conn.execute(f"PRAGMA {pragma}").fetchone()[0]
    return out


def chunk_pool_stats(fs: Aloelite) -> dict[str, int]:
    conn = fs.db.connection
    rows, logical, stored = conn.execute(
        "SELECT count(*), coalesce(sum(length), 0),"
        " coalesce(sum(length(data)), 0) FROM content_chunk"
    ).fetchone()
    refs = conn.execute("SELECT count(*) FROM content_version").fetchone()[0]
    # logical is plaintext length; stored is what the blob actually occupies,
    # which on an encrypted volume is the one that has grown.
    return {
        "pool_rows": rows,
        "pool_logical_bytes": logical,
        "pool_stored_bytes": stored,
        "manifest_refs": refs,
    }


def _fsync_path(path: Path, directory: bool = False) -> None:
    """fsync a file or a directory. On Linux a directory is fsynced through
    an O_RDONLY fd, which is why `directory` changes nothing but the caller's
    intent — it is kept so the call sites read correctly."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _sqlite_files(fs_file: Path) -> tuple[Path, ...]:
    """The .fs file and its sidecars. The -wal matters: in WAL mode a just
    written chunk lives there until a checkpoint, so a cold read that forgot
    only the main file would still be served from cache."""
    return (fs_file, Path(str(fs_file) + "-wal"), Path(str(fs_file) + "-shm"))


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


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
