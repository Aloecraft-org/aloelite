# ./aloelite/fuse.py
# License: Apache-2.0 (disclaimer at bottom of file)
#!/usr/bin/env python3
"""
aloefuse — mount an AloeLite volume as a FUSE filesystem (Linux/pyfuse3).

    sudo apt install fuse3 libfuse3-dev
    pip install pyfuse3

    # plain volume
    python3 aloefuse.py photos.sqlite photos /mnt/photos

    # encrypted volume (new or existing); three ways to supply the PIN:
    python3 aloefuse.py vault.sqlite vault /mnt/vault --pin-env VAULT_PIN
    python3 aloefuse.py vault.sqlite vault /mnt/vault --pin-file ~/.vaultpin
    python3 aloefuse.py vault.sqlite vault /mnt/vault --pin "my secret"

    # unmount
    fusermount3 -u /mnt/photos

Streaming model (minimal): a sequentially-written file is held open as a
streaming Descriptor and bytes pass straight through to the engine, which
flushes one chunk at a time — memory stays bounded regardless of file size, so
a 15 GB copy no longer buffers the whole file. A read-only file is served by a
ranged-read Descriptor (only the chunks covering each read are fetched).

Anything that doesn't fit the sequential-stream shape falls back to a buffered
read_all/write_all path *for that one file*: O_RDWR, a plain O_WRONLY without
O_TRUNC (partial overwrite), or a size change with no open handle. A non-
sequential write on a streaming handle (a seek backwards into already-flushed,
immutable bytes) returns ENOTSUP rather than corrupting — file-manager and `cp`
copies are sequential, so the fast path covers them. The buffered fallback still
holds its file in RAM (fine for small files; large random-access rewrites are
out of scope for this oracle).
"""

from __future__ import annotations

import argparse
import errno
import os
import stat as st_mod
import sys

import pyfuse3
import trio

from aloelite.aloelite import AloeLite
import aloelite.errors as aloe_errors
from aloelite.types import WriteMode, Whence

ROOT = pyfuse3.ROOT_INODE  # == 1


# --- uuid7 -> 64-bit inode (FNV-1a), root pinned to ROOT -------------------
def _ino(node_id: str) -> int:
    h = 14695981039346656037
    for b in node_id.encode():
        h = ((h ^ b) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return h if h > 1 else h + 2  # avoid 0 (invalid) / 1 (reserved)


# --- map FsError -> errno --------------------------------------------------
_ERRNO = {
    "NotFound": errno.ENOENT,
    "NotAContainer": errno.ENOTDIR,
    "NotAnEntry": errno.EISDIR,
    "NotEmpty": errno.ENOTEMPTY,
    "Nameless": errno.EINVAL,
    "LockHeld": errno.EAGAIN,
    "WouldCycle": errno.EINVAL,
    "VolumeMismatch": errno.EXDEV,
    "MountInvalid": errno.EIO,
    "Unsupported": errno.ENOTSUP,
    # Encryption errors should not appear at FUSE op time (the volume is
    # already mounted), but map them defensively so _wrap never swallows them.
    "BadKey": errno.EACCES,
    "EncryptionRequired": errno.EACCES,
}


def _wrap(e: Exception) -> pyfuse3.FUSEError:
    return pyfuse3.FUSEError(_ERRNO.get(type(e).__name__, errno.EIO))


class AloeFuse(pyfuse3.Operations):
    def __init__(self, mount):
        super().__init__()
        self.m = mount
        root = self.m.stat("/")
        self._n = {ROOT: root.id}  # inode -> NodeId
        # fh -> handle state, one of:
        #   {"mode":"w",   "path", "w": Descriptor, "pos": int}   sequential stream write
        #   {"mode":"r",   "path", "r": Descriptor}               ranged stream read
        #   {"mode":"buf", "path", "buf": bytearray, "dirty": bool}  buffered fallback
        self._open = {}
        self._fh = 0

    # -- inode bookkeeping --------------------------------------------------
    def _register(self, node_id) -> int:
        ino = _ino(node_id.value if hasattr(node_id, "value") else str(node_id))
        self._n[ino] = node_id
        return ino

    def _path(self, inode: int) -> str:
        if inode == ROOT:
            return "/"
        return self.m.path_of(self._n[inode])

    def _next_fh(self) -> int:
        self._fh += 1
        return self._fh

    def _attr(self, inode: int, info) -> pyfuse3.EntryAttributes:
        a = pyfuse3.EntryAttributes()
        a.st_ino = inode
        is_dir = info.type.value == "container"
        a.st_mode = (st_mod.S_IFDIR | 0o755) if is_dir else (st_mod.S_IFREG | 0o644)
        a.st_nlink = 2 if is_dir else 1
        a.st_size = 0 if is_dir else info.size
        a.st_uid = os.getuid()
        a.st_gid = os.getgid()
        a.st_mtime_ns = info.modified_at * 1_000_000
        a.st_ctime_ns = info.created_at * 1_000_000
        a.st_atime_ns = a.st_mtime_ns
        a.st_blksize = 512
        a.st_blocks = (a.st_size + 511) // 512
        a.entry_timeout = 0  # no kernel attr/entry caching (writes mutate)
        a.attr_timeout = 0
        return a

    # -- lookups ------------------------------------------------------------
    async def getattr(self, inode, ctx=None):
        try:
            return self._attr(inode, self.m.stat_by_id(self._n[inode]))
        except KeyError:
            raise pyfuse3.FUSEError(errno.ENOENT)
        except Exception as e:
            raise _wrap(e)

    async def lookup(self, parent_inode, name, ctx=None):
        nm = os.fsdecode(name)
        if nm in (".", ".."):  # let the kernel/root handle these
            return await self.getattr(parent_inode if nm == "." else ROOT)
        try:
            base = self._path(parent_inode).rstrip("/")
            info = self.m.stat(f"{base}/{nm}")
        except Exception as e:
            raise _wrap(e)
        return self._attr(self._register(info.id), info)

    # -- directories --------------------------------------------------------
    async def opendir(self, inode, ctx):
        return inode

    async def readdir(self, inode, start, token):
        try:
            entries = [e for e in self.m.list(self._path(inode)) if e.visible]
        except Exception as e:
            raise _wrap(e)
        for i, e in enumerate(entries):
            if i < start:
                continue
            info = self.m.stat_by_id(e.node)
            ino = self._register(e.node)
            if not pyfuse3.readdir_reply(
                token, os.fsencode(e.name), self._attr(ino, info), i + 1
            ):
                break

    # -- create / delete ----------------------------------------------------
    async def mkdir(self, parent_inode, name, mode, ctx):
        base = self._path(parent_inode).rstrip("/")
        try:
            node = self.m.create_container(f"{base}/{os.fsdecode(name)}")
            return self._attr(self._register(node), self.m.stat_by_id(node))
        except Exception as e:
            raise _wrap(e)

    async def create(self, parent_inode, name, mode, flags, ctx):
        base = self._path(parent_inode).rstrip("/")
        path = f"{base}/{os.fsdecode(name)}"
        try:
            node = self.m.create_entry(path, b"")
            writer = self.m.open_write(path, WriteMode.TRUNCATE)  # stream from byte 0
        except Exception as e:
            raise _wrap(e)
        ino = self._register(node)
        fh = self._next_fh()
        self._open[fh] = {"mode": "w", "path": path, "w": writer, "pos": 0}
        fi = pyfuse3.FileInfo(fh=fh, direct_io=True)
        return (fi, self._attr(ino, self.m.stat_by_id(node)))

    async def unlink(self, parent_inode, name, ctx):
        base = self._path(parent_inode).rstrip("/")
        try:
            self.m.remove(f"{base}/{os.fsdecode(name)}")
        except Exception as e:
            raise _wrap(e)

    async def rmdir(self, parent_inode, name, ctx):
        base = self._path(parent_inode).rstrip("/")
        try:
            self.m.remove(f"{base}/{os.fsdecode(name)}")
        except Exception as e:
            raise _wrap(e)

    async def rename(self, p_old, name_old, p_new, name_new, flags, ctx):
        ob = self._path(p_old).rstrip("/")
        nb = self._path(p_new).rstrip("/")
        src = f"{ob}/{os.fsdecode(name_old)}"
        dst = f"{nb}/{os.fsdecode(name_new)}"
        try:
            if p_old == p_new:
                self.m.rename(src, os.fsdecode(name_new))
            else:
                self.m.move(src, dst)
        except Exception as e:
            raise _wrap(e)

    # -- file io ------------------------------------------------------------
    def _open_buffered(self, path: str, fh: int, *, truncate: bool) -> None:
        data = b"" if truncate else self.m.read_all(path)
        self._open[fh] = {
            "mode": "buf",
            "path": path,
            "buf": bytearray(data),
            "dirty": truncate,
        }

    async def open(self, inode, flags, ctx):
        path = self._path(inode)
        acc = flags & os.O_ACCMODE
        try:
            if acc == os.O_RDONLY:
                # ranged streaming reader (only fetches the chunks each read needs)
                reader = self.m.open_read(path)
                fh = self._next_fh()
                self._open[fh] = {"mode": "r", "path": path, "r": reader}
            elif acc == os.O_WRONLY and (flags & os.O_TRUNC):
                writer = self.m.open_write(path, WriteMode.TRUNCATE)
                fh = self._next_fh()
                self._open[fh] = {"mode": "w", "path": path, "w": writer, "pos": 0}
            elif acc == os.O_WRONLY and (flags & os.O_APPEND):
                size = self.m.stat(path).size
                writer = self.m.open_write(path, WriteMode.APPEND)
                fh = self._next_fh()
                self._open[fh] = {"mode": "w", "path": path, "w": writer, "pos": size}
            else:
                # O_RDWR, or plain O_WRONLY (partial overwrite): buffered fallback
                fh = self._next_fh()
                self._open_buffered(path, fh, truncate=bool(flags & os.O_TRUNC))
        except Exception as e:
            raise _wrap(e)
        return pyfuse3.FileInfo(fh=fh, direct_io=True)

    async def read(self, fh, off, size):
        h = self._open[fh]
        try:
            if h["mode"] == "r":
                h["r"].seek(off, Whence.SET)
                return h["r"].read(size)
            if h["mode"] == "buf":
                return bytes(h["buf"][off : off + size])
            # write-only streaming handle
            raise pyfuse3.FUSEError(errno.ENOTSUP)
        except pyfuse3.FUSEError:
            raise
        except Exception as e:
            raise _wrap(e)

    async def write(self, fh, off, data):
        h = self._open[fh]
        try:
            if h["mode"] == "w":
                if off == h["pos"]:
                    n = h["w"].write(data)  # straight through -> engine flushes chunks
                    h["pos"] += n
                    return n
                # non-sequential write into a streaming handle: a seek back into
                # already-flushed, immutable bytes can't be rewritten cheaply.
                raise pyfuse3.FUSEError(errno.ENOTSUP)
            if h["mode"] == "buf":
                buf = h["buf"]
                if off > len(buf):
                    buf.extend(b"\x00" * (off - len(buf)))
                buf[off : off + len(data)] = data
                h["dirty"] = True
                return len(data)
            raise pyfuse3.FUSEError(errno.ENOTSUP)  # read handle
        except pyfuse3.FUSEError:
            raise
        except Exception as e:
            raise _wrap(e)

    async def setattr(self, inode, attr, fields, fh, ctx):
        # only size changes need action; accept mode/uid/time updates silently
        if fields.update_size:
            new = attr.st_size
            h = self._open.get(fh) if fh is not None else None
            if h is not None and h["mode"] == "w":
                # tolerate preallocation (ftruncate to >= current position before
                # writing); reject a real mid-stream shrink into flushed bytes.
                if new < h["pos"]:
                    raise pyfuse3.FUSEError(errno.ENOTSUP)
                # new >= pos: no-op hint; the sequential writes define real size
            elif h is not None and h["mode"] == "buf":
                buf = h["buf"]
                if new < len(buf):
                    del buf[new:]
                else:
                    buf.extend(b"\x00" * (new - len(buf)))
                h["dirty"] = True
            elif h is not None and h["mode"] == "r":
                raise pyfuse3.FUSEError(errno.ENOTSUP)
            else:
                # no open handle: read-modify-write via the atomic whole-file path
                path = self._path(inode)
                try:
                    data = bytearray(self.m.read_all(path))
                    if new < len(data):
                        del data[new:]
                    else:
                        data.extend(b"\x00" * (new - len(data)))
                    self.m.write_all(path, bytes(data))
                except Exception as e:
                    raise _wrap(e)
        return await self.getattr(inode)

    # -- commit / close -----------------------------------------------------
    # For streaming handles, full chunks are already committed as they stream;
    # the final short chunk + the committed-version pointer swap happen on
    # close() at release. flush/fsync are no-ops for streaming handles (the
    # descriptor is closed exactly once, at release). The buffered fallback
    # commits its whole buffer with the atomic write_all.
    async def flush(self, fh):
        h = self._open.get(fh)
        if h and h["mode"] == "buf" and h["dirty"]:
            try:
                self.m.write_all(h["path"], bytes(h["buf"]))
                h["dirty"] = False
            except Exception as e:
                raise _wrap(e)

    async def fsync(self, fh, datasync):
        await self.flush(fh)

    async def release(self, fh):
        h = self._open.pop(fh, None)
        if not h:
            return
        try:
            if h["mode"] == "w":
                h["w"].close()  # final chunk + pointer swap + unlock
            elif h["mode"] == "r":
                h["r"].close()
            elif h["mode"] == "buf" and h["dirty"]:
                self.m.write_all(h["path"], bytes(h["buf"]))
        except Exception as e:
            raise _wrap(e)

    async def statfs(self, ctx):
        s = pyfuse3.StatvfsData()
        s.f_bsize = s.f_frsize = 512
        s.f_blocks = s.f_bfree = s.f_bavail = 0
        s.f_files = s.f_ffree = s.f_favail = 0
        s.f_namemax = 255
        return s


def _find_or_create_volume(fs, name, pin=None):
    for v in fs.list_volumes():
        if v.name == name:
            return v.id
    return fs.create_volume(name, pin=pin).id


def _read_pin(args) -> bytes | None:
    """Resolve PIN from --pin / --pin-file / --pin-env (in that precedence order).
    Returns None if none of the three flags were given (unencrypted mount)."""
    if args.pin is not None:
        return args.pin.encode()
    if args.pin_file is not None:
        p = os.path.expanduser(args.pin_file)
        try:
            return open(p, "rb").read().rstrip(b"\n")
        except OSError as e:
            print(f"aloefuse: cannot read --pin-file {p!r}: {e}", file=sys.stderr)
            sys.exit(1)
    if args.pin_env is not None:
        val = os.environ.get(args.pin_env)
        if val is None:
            print(
                f"aloefuse: environment variable {args.pin_env!r} is not set",
                file=sys.stderr,
            )
            sys.exit(1)
        return val.encode()
    return None


async def _watch_stop(stop_event, interval: float = 0.2) -> None:
    """Poll a threading.Event; when set, ask pyfuse3.main to return. Used by the
    manager's supervisor to stop a mount thread cleanly (alongside the external
    `fusermount3 -uz`, which on its own also causes pyfuse3.main to return)."""
    while not stop_event.is_set():
        await trio.sleep(interval)
    pyfuse3.terminate()


async def fuse_main(
    sqlite_path: str,
    volume_name: str,
    mountpoint: str,
    pin: bytes | None = None,
    *,
    stop_event=None,
    allow_other: bool = True,
    debug: bool = False,
) -> None:
    """Mount one AloeLite volume at `mountpoint` and serve FUSE until the mount
    is torn down (external `fusermount3 -uz`, or `stop_event` being set).

    Owns its own AloeLite connection, mount session, and pyfuse3 session, so
    many of these run concurrently — one per thread, each in its own trio.run().
    Mount/PIN errors (aloe_errors.BadKey / EncryptionRequired) propagate to the
    caller instead of exiting, so the supervisor can translate and report them.

    The spec's `trio.run(fuse_main, sqlite_path, volume_name, mountpoint, pin)`
    works directly; pass stop_event/allow_other/debug via functools.partial.
    """
    fs = AloeLite(sqlite_path)
    try:
        vol_id = _find_or_create_volume(fs, volume_name, pin=pin)
        mount = fs.mount(vol_id, pin=pin).__enter__()
        try:
            ops = AloeFuse(mount)
            opts = set(pyfuse3.default_options)
            opts.add("fsname=aloefuse")
            if allow_other:
                # consumer containers run as other UIDs; without this they can't
                # read the mount (the single most common silent failure here).
                opts.add("allow_other")
            if debug:
                opts.add("debug")
            pyfuse3.init(ops, mountpoint, opts)
            try:
                if stop_event is not None:
                    async with trio.open_nursery() as nursery:
                        nursery.start_soon(_watch_stop, stop_event)
                        await pyfuse3.main()
                        nursery.cancel_scope.cancel()  # main returned: stop watcher
                else:
                    await pyfuse3.main()
            finally:
                pyfuse3.close(unmount=True)
        finally:
            mount.__exit__(None, None, None)
    finally:
        fs.close()


def main():
    ap = argparse.ArgumentParser(
        description="Mount an AloeLite volume via FUSE.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Encryption
----------
Supply a PIN to mount an encrypted volume (or to create a new encrypted one):

  --pin SECRET            PIN on the command line (avoid on shared hosts)
  --pin-file ~/.aloepin   read PIN from a file (newline stripped)
  --pin-env ALOE_PIN      read PIN from an environment variable

If the volume already exists and is encrypted, a PIN is required.
If it already exists and is plain, a PIN must NOT be given.

Examples
--------
  # plain
  python3 aloefuse.py photos.sqlite photos /mnt/photos

  # encrypted (new or existing)
  python3 aloefuse.py vault.sqlite vault /mnt/vault --pin-env VAULT_PIN
""",
    )
    ap.add_argument("db", help="path to the AloeLite sqlite file")
    ap.add_argument("volume", help="volume name (created if absent)")
    ap.add_argument("mountpoint", help="empty directory to mount at")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument(
        "--allow-other",
        action="store_true",
        help="allow other UIDs to access the mount "
        "(requires user_allow_other in /etc/fuse.conf)",
    )

    pin_grp = ap.add_argument_group("encryption")
    pin_grp.add_argument(
        "--pin",
        metavar="SECRET",
        help="PIN (plaintext, prefer --pin-file or --pin-env)",
    )
    pin_grp.add_argument(
        "--pin-file", metavar="PATH", help="file whose contents are the PIN"
    )
    pin_grp.add_argument(
        "--pin-env", metavar="VAR", help="environment variable holding the PIN"
    )

    args = ap.parse_args()
    pin = _read_pin(args)

    import functools

    runner = functools.partial(
        fuse_main,
        args.db,
        args.volume,
        args.mountpoint,
        pin,
        allow_other=args.allow_other,
        debug=args.debug,
    )
    try:
        trio.run(runner)
    except aloe_errors.BadKey:
        print(
            f"aloefuse: wrong PIN for volume '{args.volume}' in {args.db!r}.\n"
            "  Check your --pin / --pin-file / --pin-env value.",
            file=sys.stderr,
        )
        sys.exit(1)
    except aloe_errors.EncryptionRequired:
        if pin is None:
            print(
                f"aloefuse: volume '{args.volume}' is encrypted but no PIN was given.\n"
                "  Use --pin, --pin-file, or --pin-env.",
                file=sys.stderr,
            )
        else:
            print(
                f"aloefuse: volume '{args.volume}' is not encrypted but a PIN was given.\n"
                "  Drop --pin / --pin-file / --pin-env to mount a plain volume.",
                file=sys.stderr,
            )
        sys.exit(1)


if __name__ == "__main__":
    sys.exit(main())
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
