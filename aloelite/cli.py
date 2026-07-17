# ./aloelite/cli.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
aloelite — command-line front-end over the Aloelite wrapper.

    aloelite -f notebook.fs ls /
    aloelite -f notebook.fs -v vault put local.txt /docs/remote.txt
    cat data | aloelite -f notebook.fs put - /stdin.bin
    aloelite -f notebook.fs get /docs/remote.txt -            # to stdout
    aloelite -f notebook.fs mkdir -p /a/b/c
    aloelite -f notebook.fs volumes
    aloelite -f notebook.fs mounts

Session-per-invocation: each command opens the file, mounts, operates,
unmounts, closes. Volume selection (-v) accepts a name or a volume id
(canonical uuid7 with dashes, or bare hex — dashes stripped, lowercase).
With no -v: a file containing exactly one volume uses it; several volumes
is a refusal-to-guess, listing the candidates. PIN comes from the standard
--pin/--pin-file/--pin-env flags; interactively, an encrypted volume with
no flag prompts via getpass.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import sys

from . import errors
from .aloelite import Aloelite, Mount
from .pin import PinError, add_pin_arguments, read_pin
from .types import VolumeId

_CHUNK = 1 << 20
_HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")


def _fail(msg: str, code: int = 1) -> "int":
    print(f"aloelite: {msg}", file=sys.stderr)
    return code


def _normalize_volume_ref(ref: str) -> str:
    """Accept a volume id as canonical uuid7 or bare hex: strip dashes,
    and if 32 hex chars remain, re-dash into canonical lowercase form.
    Anything else is returned untouched (it may be a name)."""
    bare = ref.replace("-", "")
    if _HEX32.match(bare):
        b = bare.lower()
        return f"{b[0:8]}-{b[8:12]}-{b[12:16]}-{b[16:20]}-{b[20:32]}"
    return ref


def _select_volume(fs: Aloelite, ref: str | None) -> VolumeId:
    vols = fs.list_volumes()
    if ref is None:
        if len(vols) == 1:
            return vols[0].id
        if not vols:
            raise SystemExit(_fail("file contains no volumes"))
        names = ", ".join(f"{v.name or '(unnamed)'} ({v.id[:8]}…)" for v in vols)
        raise SystemExit(_fail(f"multiple volumes; pick one with -v: {names}"))
    ref = _normalize_volume_ref(ref)
    # name-first, id-fallback — same contract as Aloelite.mount
    resolved = fs.resolve_volume_name(ref)
    if resolved is not None:
        return resolved
    for v in vols:
        if v.id == ref:
            return v.id
    raise SystemExit(_fail(f"no volume named or identified by {ref!r}"))


def _mount(fs: Aloelite, args) -> Mount:
    vol = _select_volume(fs, args.volume)
    try:
        pin = read_pin(args.pin, args.pin_file, args.pin_env)
    except PinError as e:
        raise SystemExit(_fail(str(e)))
    try:
        return fs.mount(vol, pin=pin)
    except errors.EncryptionRequired:
        if pin is None and sys.stdin.isatty():
            pin = getpass.getpass("PIN: ").encode()
            return fs.mount(vol, pin=pin)
        raise


# -- verbs -------------------------------------------------------------------
def _cmd_ls(m: Mount, args) -> int:
    for e in m.list(args.path):
        if not e.visible:
            continue
        if args.long:
            size = m.stat_by_id(e.node).size if e.type.value == "entry" else "-"
            print(f"{e.type.value[0]}  {size!s:>10}  {e.path}")
        else:
            print(e.path + ("/" if e.type.value == "container" else ""))
    return 0


def _cmd_put(m: Mount, args) -> int:
    if args.src == "-":
        m.put(args.dst, sys.stdin.buffer.read(), append=args.append)
        return 0
    # file source: stream through a descriptor (bounded memory)
    if args.append:
        with open(args.src, "rb") as f:
            m.put(args.dst, f.read(), append=True)  # atomic append per call
        return 0
    with open(args.src, "rb") as f, m.open_write(args.dst) as w:
        while chunk := f.read(_CHUNK):
            w.write(chunk)
    return 0


def _cmd_get(m: Mount, args) -> int:
    out = sys.stdout.buffer if args.dst in (None, "-") else open(args.dst, "wb")
    try:
        with m.open_read(args.src) as r:
            while chunk := r.read(_CHUNK):
                out.write(chunk)
    finally:
        if out is not sys.stdout.buffer:
            out.close()
    return 0


def _cmd_mkdir(m: Mount, args) -> int:
    m.mkdir(args.path, parents=args.parents, exist_ok=args.parents)
    return 0


def _cmd_rm(m: Mount, args) -> int:
    if args.recursive:
        m.remove_recursive(args.path)
    else:
        m.remove(args.path)
    return 0


def _cmd_cat(m: Mount, args) -> int:
    with m.open_read(args.path) as r:
        while chunk := r.read(_CHUNK):
            sys.stdout.buffer.write(chunk)
    return 0


def _cmd_cp(m: Mount, args) -> int:
    # engine copy: dedup-preserving, near-free (chunks re-referenced)
    m.copy(args.src, args.dst)
    return 0


def _cmd_stat(m: Mount, args) -> int:
    st = m.stat(args.path)
    print(f"path:     {args.path}")
    print(f"id:       {st.id}")
    print(f"type:     {st.type.value}")
    print(f"size:     {st.size if st.size is not None else '-'}")
    print(f"created:  {st.created_at}")
    print(f"modified: {st.modified_at}")
    if st.metadata:
        print(f"metadata: {st.metadata}")
    return 0


def _cmd_tree(m: Mount, args) -> int:
    def walk(path: str, prefix: str) -> None:
        entries = [e for e in m.list(path) if e.visible]
        for i, e in enumerate(entries):
            last = i == len(entries) - 1
            branch = "└── " if last else "├── "
            is_dir = e.type.value == "container"
            print(prefix + branch + e.name + ("/" if is_dir else ""))
            if is_dir:
                walk(e.path, prefix + ("    " if last else "│   "))

    root = args.path or "/"
    print(root)
    walk(root, "")
    return 0


def _cmd_mv(m: Mount, args) -> int:
    m.move(args.src, args.dst)
    return 0


def _cmd_prune(fs: Aloelite, args) -> int:
    """Reclaim unreferenced state: retired locks and volatile nodes, then
    superseded/aborted content versions and unreferenced pool chunks.
    Scoped to -v when given, whole file otherwise. --vacuum compacts the
    file afterward (returns freed pages to the OS)."""
    vol = _select_volume(fs, args.volume) if args.volume else None
    r1 = fs.prune(vol)
    r2 = fs.prune_content(vol)
    print(
        f"pruned: {r1.nodes_pruned} nodes, {r1.locks_pruned} locks, "
        f"{r2.versions_pruned} versions, {r2.chunks_pruned} chunks"
    )
    if args.vacuum:
        fs.db.connection.execute("VACUUM")
        print("vacuumed")
    return 0


def _cmd_volumes(fs: Aloelite, args) -> int:
    for v in fs.list_volumes():
        print(f"{v.id}  {v.name or '(unnamed)'}")
    return 0


def _cmd_mounts(fs: Aloelite, args) -> int:
    names = {v.id: v.name for v in fs.list_volumes()}
    for i in fs.list_mounts(include_unmounted=args.all):
        label = f"{names.get(i.volume) or i.volume[:8]}:{i.mount_path or '?'}"
        print(f"{i.id}  {i.state.value:<9}  {label}")
    return 0


# -- wiring ------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="aloelite", description="Operate on an Aloelite filesystem file."
    )
    ap.add_argument(
        "-f",
        "--file",
        default=os.environ.get("ALOELITE_FILE"),
        help="path to the .sqlite/.fs file (default: $ALOELITE_FILE)",
    )
    ap.add_argument(
        "-v",
        "--volume",
        metavar="NAME_OR_ID",
        help="volume name or id (optional if the file has exactly one)",
    )
    add_pin_arguments(ap)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ls", help="list a directory")
    p.add_argument("path", nargs="?", default="/")
    p.add_argument("-l", "--long", action="store_true", help="show type and size")

    p = sub.add_parser("put", help="write a local file (or '-' = stdin) to a path")
    p.add_argument("src")
    p.add_argument("dst")
    p.add_argument("--append", action="store_true")

    p = sub.add_parser("get", help="read a path to a local file (or '-' = stdout)")
    p.add_argument("src")
    p.add_argument("dst", nargs="?")

    p = sub.add_parser("mkdir", help="create a container")
    p.add_argument("path")
    p.add_argument(
        "-p",
        "--parents",
        action="store_true",
        help="create parents; no error if it exists (mkdir -p)",
    )

    p = sub.add_parser("rm", help="remove an entry or empty container")
    p.add_argument("path")
    p.add_argument("-r", "--recursive", action="store_true")

    p = sub.add_parser("mv", help="move/rename")
    p.add_argument("src")
    p.add_argument("dst")

    p = sub.add_parser("cat", help="print a file to stdout")
    p.add_argument("path")

    p = sub.add_parser("cp", help="copy (dedup-preserving, near-free)")
    p.add_argument("src")
    p.add_argument("dst")

    p = sub.add_parser("stat", help="show a node's details")
    p.add_argument("path")

    p = sub.add_parser("tree", help="print a directory tree")
    p.add_argument("path", nargs="?", default="/")

    p = sub.add_parser("prune", help="reclaim unreferenced nodes, locks, and content")
    p.add_argument(
        "--vacuum", action="store_true", help="compact the file afterward (VACUUM)"
    )

    p = sub.add_parser("volumes", help="list volumes in the file")

    p = sub.add_parser("mounts", help="list durable mounts in the file")
    p.add_argument("--all", action="store_true", help="include retired mounts")

    return ap


_MOUNT_VERBS = {
    "ls": _cmd_ls,
    "put": _cmd_put,
    "get": _cmd_get,
    "cat": _cmd_cat,
    "cp": _cmd_cp,
    "stat": _cmd_stat,
    "tree": _cmd_tree,
    "mkdir": _cmd_mkdir,
    "rm": _cmd_rm,
    "mv": _cmd_mv,
}
_FS_VERBS = {"volumes": _cmd_volumes, "mounts": _cmd_mounts, "prune": _cmd_prune}


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not args.file:
        return _fail("no file given: pass -f or set ALOELITE_FILE")
    try:
        with Aloelite(args.file) as fs:
            if args.cmd in _FS_VERBS:
                return _FS_VERBS[args.cmd](fs, args)
            with _mount(fs, args) as m:
                return _MOUNT_VERBS[args.cmd](m, args)
    except SystemExit as e:
        return int(e.code or 0)
    except errors.BadKey:
        return _fail("wrong PIN")
    except errors.EncryptionRequired as e:
        return _fail(str(e))
    except errors.FsError as e:
        return _fail(f"{e.code}: {e}")
    except OSError as e:
        return _fail(str(e))


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
