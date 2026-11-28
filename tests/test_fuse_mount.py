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
    for mp, _daemon in _mounted(tmp_path):
        yield mp


def _mounted(tmp_path: Path, *extra: str):
    """Yields (mountpoint, daemon process) once, then unmounts."""
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
            *extra,
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
        yield mp, proc
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


def test_lookup_after_entry_timeout_keeps_unflushed_size(mnt: Path):
    """Extend a file through an open rw fd, wait out the 1s entry timeout,
    then open it again BY PATH. The fresh LOOKUP must report the pending
    size, not the committed one: the kernel adopts the size in a lookup
    reply, and a committed-only answer shrank i_size under the writer so the
    new reader was cut short. PostgreSQL 18's initdb copies template files
    exactly this way, and lost every block after the first."""
    p = mnt / "grow.bin"
    with open(p, "wb") as f:
        f.write(b"a" * 8192)
    w = os.open(p, os.O_RDWR)
    try:
        os.pwrite(w, b"b" * 24576, 8192)
        time.sleep(1.5)
        r = os.open(p, os.O_RDONLY)
        try:
            assert os.fstat(r).st_size == 32768
            assert _pread_uncached(r, 65536, 0) == b"a" * 8192 + b"b" * 24576
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
# Ownership on create (issue #15)
# --------------------------------------------------------------------------
def test_new_nodes_belong_to_the_caller(mnt: Path):
    """A node is owned by the caller that made it and carries exactly the
    mode it asked for, less the umask; under a setgid directory it takes the
    directory's group and a new subdirectory inherits the setgid bit."""
    old = os.umask(0o022)
    try:
        os.mkdir(mnt / "d", 0o750)
        os.close(os.open(mnt / "d" / "f", os.O_WRONLY | os.O_CREAT, 0o666))
        d, f = os.stat(mnt / "d"), os.stat(mnt / "d" / "f")
        me = (os.geteuid(), os.getegid())
        assert (d.st_uid, d.st_gid, d.st_mode & 0o7777) == (*me, 0o750)
        assert (f.st_uid, f.st_gid, f.st_mode & 0o7777) == (*me, 0o644)
        if os.geteuid() == 0:  # only root can give a directory another group
            os.chown(mnt / "d", -1, 65534)
            os.chmod(mnt / "d", 0o2775)
            os.mkdir(mnt / "d" / "sub", 0o755)
            sub = os.stat(mnt / "d" / "sub")
            assert (sub.st_gid, sub.st_mode & 0o7777) == (65534, 0o2755)
    finally:
        os.umask(old)


@pytest.mark.skipif(os.geteuid() != 0, reason="needs root to serve another uid")
def test_root_daemon_serves_another_uid():
    """The initdb failure: a root daemon with --allow-other, a directory
    handed to another uid, and that uid building a 0700 tree inside it. With
    the daemon's own uid recorded as owner, the kernel's permission check
    (default_permissions) refused the uid entry to what it had just made."""
    # the other uid has to reach the mountpoint, so not under pytest's 0700
    # basetemp
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="aloelite-uid-"))
    root.chmod(0o755)
    try:
        _serve_another_uid(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _serve_another_uid(root: Path) -> None:
    for mp, _daemon in _mounted(root, "--allow-other"):
        os.mkdir(mp / "home")
        os.chown(mp / "home", 65534, 65534)
        made = subprocess.run(
            ["sh", "-c", "umask 077; mkdir -p a/b/c; echo ok > a/b/c/f; cat a/b/c/f"],
            cwd=mp / "home",
            user=65534,
            group=65534,
            extra_groups=[],
            capture_output=True,
            text=True,
        )
        assert made.returncode == 0, made.stderr
        assert made.stdout == "ok\n"
        st = os.stat(mp / "home" / "a" / "b" / "c")
        assert (st.st_uid, st.st_gid, st.st_mode & 0o7777) == (65534, 65534, 0o700)


def test_a_killed_daemons_mount_does_not_block_a_remount(tmp_path: Path):
    """SIGKILL the daemon, clear the dead kernel mount, mount again at once.
    The killed daemon's rw row used to refuse the remount (mount_conflict)
    until its five-minute lease ran out; its recorded holder is now
    provably gone, so the next daemon retires the row (#15)."""
    first = _mounted(tmp_path)
    mp, daemon = next(first)
    (mp / "f").write_bytes(b"kept")
    os.sync()
    daemon.kill()
    first.close()  # fusermount3 -u the dead mount, and reap the daemon
    mp.rmdir()
    for mp, _daemon in _mounted(tmp_path):
        assert (mp / "f").read_bytes() == b"kept"


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
