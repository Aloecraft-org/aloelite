# ./tests/test_posix_surface.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
POSIX-surface tests on a live kernel mount: the behaviors the compatibility
table cites. Where `tests/test_fuse_mount.py` pins the handle-coherence
contract, this module pins the syscall surface *around* plain read/write —
what works, and the exact shape of what degrades.

The load-bearing background: pyfuse3 exposes no lock, fallocate, lseek, or
copy_file_range handlers, so those syscalls never reach the daemon. That is
not the same as "they fail" — the kernel arbitrates POSIX/BSD locks locally
per mount when the filesystem provides no lock ops, the page cache gives
mmap MAP_SHARED same-mount coherence, and glibc emulates posix_fallocate
with zero writes on EOPNOTSUPP. These tests prove that per-mount behavior
and document its boundary: none of it coordinates across *separate* mounts
of the same volume.

Probed 2026-08-26 on Linux 6.18 / pyfuse3 3.5.0; results recorded here as
assertions so a regression (kernel, pyfuse3, or our fuse.py) is caught, not
re-discovered.

Self-skipping exactly like test_fuse_mount.py: needs /dev/fuse,
fusermount3, and pyfuse3.
"""

from __future__ import annotations

import fcntl
import mmap
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.fuse

if not os.path.exists("/dev/fuse"):  # pragma: no cover - environment guard
    pytest.skip("/dev/fuse not available", allow_module_level=True)
if shutil.which("fusermount3") is None:  # pragma: no cover - environment guard
    pytest.skip("fusermount3 not installed", allow_module_level=True)
pytest.importorskip("pyfuse3", reason="pyfuse3 not installed (aloelite[fuse])")

_MOUNT_WAIT_S = 15.0


@pytest.fixture
def mnt(tmp_path: Path):
    """A live aloelite-fuse mount on a fresh volume file; unmounts on exit."""
    mp = tmp_path / "mnt"
    mp.mkdir()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from aloelite.fuse import main; main()",
            "-f",
            str(tmp_path / "test.fs"),
            "-v",
            "data",
            "--create",
            str(mp),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    deadline = time.monotonic() + _MOUNT_WAIT_S
    while time.monotonic() < deadline:
        if os.path.ismount(mp):
            break
        if proc.poll() is not None:
            out = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
            pytest.fail(f"aloelite-fuse exited {proc.returncode} before mount:\n{out}")
        time.sleep(0.1)
    else:
        proc.terminate()
        pytest.fail(f"mount did not appear within {_MOUNT_WAIT_S}s")
    try:
        yield mp
    finally:
        subprocess.run(["fusermount3", "-u", str(mp)], capture_output=True)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - hung daemon
            subprocess.run(["fusermount3", "-uz", str(mp)], capture_output=True)
            proc.kill()
            proc.wait(timeout=10)


def _run_child(code: str) -> str:
    r = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout.strip()


def test_fcntl_range_locks_arbitrate_within_mount(mnt: Path):
    """Kernel-local POSIX locks: a second process is denied the held range
    and granted a disjoint one. Scope: one mount; separate mounts of the
    same volume are NOT coordinated."""
    f = mnt / "lockfile"
    f.write_bytes(b"x" * 200)
    fd = os.open(f, os.O_RDWR)
    try:
        fcntl.lockf(fd, fcntl.LOCK_EX, 10, 0)
        out = _run_child(
            f"""
import fcntl, os
fd = os.open({str(f)!r}, os.O_RDWR)
try:
    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 10, 0)
    print("conflict:acquired")
except OSError:
    print("conflict:denied")
fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 10, 100)
print("disjoint:acquired")
"""
        )
        assert out.splitlines() == ["conflict:denied", "disjoint:acquired"]
    finally:
        os.close(fd)


def test_flock_arbitrates_within_mount(mnt: Path):
    f = mnt / "flockfile"
    f.write_bytes(b"x")
    fd = os.open(f, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        out = _run_child(
            f"""
import fcntl, os
fd = os.open({str(f)!r}, os.O_RDWR)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    print("acquired")
except OSError:
    print("denied")
"""
        )
        assert out == "denied"
    finally:
        os.close(fd)


def test_mmap_shared_write_reaches_daemon(mnt: Path):
    """MAP_SHARED dirty pages flushed by msync must be served back by the
    daemon itself — page cache dropped before the verifying read."""
    f = mnt / "mmapfile"
    f.write_bytes(b"\0" * 4096)
    fd = os.open(f, os.O_RDWR)
    mm = mmap.mmap(fd, 4096, mmap.MAP_SHARED)
    mm[0:5] = b"HELLO"
    mm.flush()
    mm.close()
    os.close(fd)

    fd = os.open(f, os.O_RDONLY)
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        assert os.read(fd, 5) == b"HELLO"
    finally:
        os.close(fd)


def test_mmap_shared_cross_process_coherent(mnt: Path):
    """Two processes mapping the same file on the same mount share pages —
    a write is visible to the other map with no msync. This is what sqlite
    WAL's -shm coordination needs."""
    f = mnt / "mmapfile2"
    f.write_bytes(b"\0" * 4096)
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"""
import mmap, os, time
fd = os.open({str(f)!r}, os.O_RDWR)
mm = mmap.mmap(fd, 4096, mmap.MAP_SHARED)
mm[0:5] = b"WORLD"
time.sleep(1.0)
""",
        ]
    )
    try:
        fd = os.open(f, os.O_RDWR)
        mm = mmap.mmap(fd, 4096, mmap.MAP_SHARED)
        deadline = time.monotonic() + 5.0
        seen = b""
        while time.monotonic() < deadline:
            seen = bytes(mm[0:5])
            if seen == b"WORLD":
                break
            time.sleep(0.05)
        mm.close()
        os.close(fd)
        assert seen == b"WORLD"
    finally:
        child.wait(timeout=15)


@pytest.mark.parametrize("mode", ["delete", "wal"])
def test_sqlite_database_on_mount(mnt: Path, mode: str):
    """sqlite runs on a mount in rollback AND wal journal modes: write,
    reopen, integrity_check. WAL exercises the -shm MAP_SHARED path."""
    db = mnt / f"probe-{mode}.db"
    conn = sqlite3.connect(db, timeout=10)
    got = conn.execute(f"PRAGMA journal_mode={mode}").fetchone()[0]
    assert got == mode
    conn.execute("CREATE TABLE t(x)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(100)])
    conn.commit()
    conn.close()

    conn = sqlite3.connect(db, timeout=10)
    assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 100
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()


def test_sqlite_wal_concurrent_second_process(mnt: Path):
    """A second process reads a WAL database while the first still holds it
    open — the cross-process shared-memory coordination sqlite WAL needs."""
    db = mnt / "probe-wal2.db"
    conn = sqlite3.connect(db, timeout=10)
    assert conn.execute("PRAGMA journal_mode=wal").fetchone()[0] == "wal"
    conn.execute("CREATE TABLE t(x)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(50)])
    conn.commit()
    try:
        out = _run_child(
            f"""
import sqlite3
c = sqlite3.connect({str(db)!r}, timeout=10)
print(c.execute("SELECT count(*) FROM t").fetchone()[0])
print(c.execute("PRAGMA integrity_check").fetchone()[0])
"""
        )
        assert out.splitlines() == ["50", "ok"]
    finally:
        conn.close()


def test_seek_past_eof_write_zero_fills(mnt: Path):
    """A write beyond EOF extends the file and the gap reads as zeros from
    the daemon (page cache dropped). Storage-level sparseness is a separate
    property; the chunk pool dedups zero chunks regardless."""
    f = mnt / "sparsefile"
    off = 1 << 20
    fd = os.open(f, os.O_RDWR | os.O_CREAT)
    try:
        os.lseek(fd, off, os.SEEK_SET)
        os.write(fd, b"END")
    finally:
        os.close(fd)
    assert f.stat().st_size == off + 3

    fd = os.open(f, os.O_RDONLY)
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        assert os.read(fd, 4096) == b"\0" * 4096
        os.lseek(fd, off, os.SEEK_SET)
        assert os.read(fd, 3) == b"END"
    finally:
        os.close(fd)


def test_posix_fallocate_extends_via_glibc_fallback(mnt: Path):
    """No fallocate handler exists in pyfuse3, so glibc emulates
    posix_fallocate with explicit zero writes. Slow but correct; pin that
    it succeeds and the size is real."""
    f = mnt / "fallocfile"
    f.write_bytes(b"x")
    fd = os.open(f, os.O_RDWR)
    try:
        os.posix_fallocate(fd, 0, 1 << 16)
        assert os.fstat(fd).st_size == 1 << 16
    finally:
        os.close(fd)


def test_unsupported_surface_fails_loudly_not_silently(mnt: Path):
    """The declined syscalls return clean errnos, not corruption or hangs.
    When one of these starts passing, the feature landed — move it up into
    a real test and update the compatibility table."""
    f = mnt / "plain"
    f.write_bytes(b"x")

    with pytest.raises(OSError):  # ENOSYS: mknod not implemented in fuse.py
        os.mkfifo(mnt / "fifo")
    with pytest.raises(OSError):  # EOPNOTSUPP: no xattr storage yet
        os.setxattr(f, "user.test", b"v")
    with pytest.raises(OSError):  # EPERM: PI-1 forbids a second placement
        os.link(f, mnt / "hardlink")


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
