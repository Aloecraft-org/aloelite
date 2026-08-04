# ./tests/test_fuse_mount.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
Mounted-filesystem tests: the FUSE layer through the kernel, for the
semantics no ops-layer test can reach (kernel page cache, cross-fd
visibility, the daemon actually serving bytes).

These exercise the handle-coherence contract: data written through one file
handle is visible to reads through any other handle on the same file, sized
consistently with fstat, before the writer closes. The regression they pin
is the short-read-despite-correct-st_size failure that broke `git push`
onto a mount (git index-pack reads the pack back through a second fd while
still writing it; receive.unpackLimit pushes >= 100 objects through that
path).

Every read that must prove DAEMON behavior first drops the kernel page
cache for the fd (posix_fadvise DONTNEED). Without that, the reader is
served the writer's still-resident pages and the test passes even when the
daemon would have returned nothing -- which is exactly how the original bug
stayed invisible to casual testing.

Self-skipping: needs /dev/fuse, fusermount3, and pyfuse3 -- absent any of
them (e.g. CI without the [fuse] extra) the module skips collection.
"""

from __future__ import annotations

import os
import shutil
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


def _pread_uncached(fd: int, n: int, off: int) -> bytes:
    """Read via the daemon, not the page cache: drop cached pages for the fd
    first so the returned bytes prove what the FUSE layer served."""
    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    return os.pread(fd, n, off)


# --------------------------------------------------------------------------
# Cross-handle coherence (the incident regression)
# --------------------------------------------------------------------------
def test_second_fd_sees_unflushed_writes(mnt: Path):
    """The reported repro: write 60000 bytes, no close/fsync; a second
    read-only fd must see all of them, cached or not."""
    p = mnt / "t2.bin"
    w = os.open(p, os.O_RDWR | os.O_CREAT | os.O_TRUNC)
    try:
        os.write(w, b"A" * 60000)
        r = os.open(p, os.O_RDONLY)
        try:
            assert os.fstat(w).st_size == 60000
            assert os.fstat(r).st_size == 60000
            assert len(os.pread(r, 70000, 0)) == 60000
            assert _pread_uncached(r, 70000, 0) == b"A" * 60000
        finally:
            os.close(r)
    finally:
        os.close(w)


def test_concurrent_rw_handles_do_not_lose_updates(mnt: Path):
    """Two rw fds writing disjoint extents; both survive, gap zero-fills."""
    p = mnt / "two.bin"
    w1 = os.open(p, os.O_RDWR | os.O_CREAT | os.O_TRUNC)
    w2 = os.open(p, os.O_RDWR)
    os.pwrite(w1, b"X" * 100, 0)
    os.pwrite(w2, b"Y" * 100, 200)
    os.close(w1)
    os.close(w2)
    r = os.open(p, os.O_RDONLY)
    try:
        got = _pread_uncached(r, 400, 0)
    finally:
        os.close(r)
    assert got == b"X" * 100 + b"\0" * 100 + b"Y" * 100


def test_rw_sibling_reads_through_overlay(mnt: Path):
    """A second rw fd on the same file sees the first fd's unflushed bytes."""
    p = mnt / "ov.bin"
    w1 = os.open(p, os.O_RDWR | os.O_CREAT | os.O_TRUNC)
    try:
        os.write(w1, b"Z" * 5000)
        w2 = os.open(p, os.O_RDWR)
        try:
            assert _pread_uncached(w2, 6000, 0) == b"Z" * 5000
        finally:
            os.close(w2)
    finally:
        os.close(w1)


def test_append_batch_visible_before_close(mnt: Path):
    """Bytes buffered by an O_APPEND handle are served to a reader that
    arrives before the append batch commits."""
    p = mnt / "log.txt"
    a = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    try:
        os.write(a, b"line1\n")
        r = os.open(p, os.O_RDONLY)
        try:
            assert _pread_uncached(r, 100, 0) == b"line1\n"
        finally:
            os.close(r)
    finally:
        os.close(a)


def test_plain_roundtrip_unaffected(mnt: Path):
    """The safe pattern stays safe: write -> close -> reopen -> read."""
    p = mnt / "clean.bin"
    w = os.open(p, os.O_RDWR | os.O_CREAT | os.O_TRUNC)
    os.write(w, b"Q" * 60000)
    os.close(w)
    r = os.open(p, os.O_RDONLY)
    try:
        assert _pread_uncached(r, 70000, 0) == b"Q" * 60000
    finally:
        os.close(r)


# --------------------------------------------------------------------------
# End to end: the actual customer failure
# --------------------------------------------------------------------------
@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_push_pack_over_unpack_limit(mnt: Path, tmp_path: Path):
    """Push >= 100 objects so receive.unpackLimit routes through index-pack,
    which reads the pack back through a second fd while still writing it.
    This failed with 'premature end of pack file' before handle coherence."""

    def git(*args: str, cwd: Path) -> None:
        subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, timeout=120
        )

    bare = mnt / "repo.git"
    git("init", "-q", "--bare", str(bare), cwd=mnt)
    work = tmp_path / "work"
    work.mkdir()
    git("init", "-q", ".", cwd=work)
    git("config", "user.email", "t@t", cwd=work)
    git("config", "user.name", "t", cwd=work)
    for i in range(120):
        (work / f"f{i}.txt").write_bytes(os.urandom(64) + f"file {i}\n".encode())
    git("add", "-A", cwd=work)
    git("commit", "-qm", "120 objects", cwd=work)
    git(
        "-c",
        "receive.unpackLimit=100",
        "push",
        "-q",
        str(bare),
        "HEAD:refs/heads/master",
        cwd=work,
    )
    fsck = subprocess.run(
        ["git", "fsck", "--strict"],
        cwd=bare,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert fsck.returncode == 0, fsck.stderr


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
