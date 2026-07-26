# ./aloelite/admin.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
aloelite-admin — maintenance and rescue utilities.

Operates on volumes, keys, and the file itself (vs the daily-driver CLI,
which operates on files inside volumes). Also reachable as `aloelite admin`.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from . import errors
from .aloelite import Aloelite
from .cli import (
    _enc_label,
    _fail,
    _mount,
    _select_volume,
    _ts,
    _version,
    _volume_line,
)
from .pin import PinError, add_pin_arguments, read_pin


def _prompt_new_pin(args) -> bytes:
    """New PIN: --new-pin-env/--new-pin-file for scripts, else getpass with
    confirmation (setting a PIN blind is unrecoverable if mistyped)."""
    try:
        p = read_pin(None, args.new_pin_file, args.new_pin_env)
    except PinError as e:
        raise SystemExit(_fail(str(e)))
    if p is not None:
        return p
    try:
        open("/dev/tty", "r").close()
    except OSError:
        raise SystemExit(
            _fail("no terminal for the new PIN; use --new-pin-env or --new-pin-file")
        )
    first = getpass.getpass("New PIN: ")
    if getpass.getpass("Confirm new PIN: ") != first:
        raise SystemExit(_fail("PINs did not match"))
    return first.encode()


def _cmd_pin_change(fs: Aloelite, args) -> int:
    vol = _select_volume(fs, args.volume)
    try:
        old = read_pin(args.pin, args.pin_file, args.pin_env)
    except PinError as e:
        raise SystemExit(_fail(str(e)))
    if old is None:
        try:
            open("/dev/tty", "r").close()
        except OSError:
            return _fail("no old PIN given and no terminal to prompt")
        old = getpass.getpass("Old PIN: ").encode()
    new = _prompt_new_pin(args)
    fs.change_pin(vol, old, new)
    print("PIN changed")
    return 0


def _dest_pin(args) -> bytes | None:
    try:
        p = read_pin(None, args.dest_pin_file, args.dest_pin_env)
    except PinError as e:
        raise SystemExit(_fail(str(e)))
    if p is not None:
        return p
    if not args.encrypt:
        return None
    try:
        open("/dev/tty", "r").close()
    except OSError:
        raise SystemExit(
            _fail("--encrypt needs a terminal; use --dest-pin-env/--dest-pin-file")
        )
    first = getpass.getpass("Destination PIN: ")
    if getpass.getpass("Confirm destination PIN: ") != first:
        raise SystemExit(_fail("PINs did not match"))
    return first.encode()


def _src_pin(args) -> bytes | None:
    try:
        return read_pin(args.pin, args.pin_file, args.pin_env)
    except PinError as e:
        raise SystemExit(_fail(str(e)))


def _cmd_snapshot(fs: Aloelite, args) -> int:
    from .transfer import snapshot_volume

    vol = _select_volume(fs, args.volume)
    vid, c, e = snapshot_volume(args.file, vol, args.name, pin=_src_pin(args))
    print(f"snapshot '{args.name}' ({vid}): {c} containers, {e} entries")
    return 0


def _cmd_export(fs: Aloelite, args) -> int:
    from .transfer import export_volume

    vol = _select_volume(fs, args.volume)
    vid, c, e = export_volume(
        args.file,
        vol,
        args.dest,
        src_pin=_src_pin(args),
        dest_pin=_dest_pin(args),
        dest_name=args.rename,
    )
    print(f"exported {c} containers, {e} entries -> {args.dest} volume {vid}")
    return 0


def _cmd_import(fs: Aloelite, args) -> int:
    from .transfer import export_volume

    with Aloelite(args.src) as src_fs:
        from .cli import _select_volume as sel

        vol = sel(src_fs, args.volume)
    vid, c, e = export_volume(
        args.src,
        vol,
        args.file,
        src_pin=_src_pin(args),
        dest_pin=_dest_pin(args),
        dest_name=args.rename,
    )
    print(f"imported {c} containers, {e} entries <- {args.src} volume {vid}")
    return 0


def _cmd_verify(fs: Aloelite, args) -> int:
    with _mount(fs, args) as m:
        rep = m.verify(deep=args.deep)
    label = "deep" if args.deep else "shallow"
    if rep.ok:
        print(f"ok ({label}): {rep.entries_checked} entries"
              + (f", {rep.chunks_checked} chunks" if args.deep else ""))
        return 0
    for p in rep.problems:
        print(p)
    return 1


def _cmd_health(fs: Aloelite, args) -> int:
    anomalies = fs.health_check()
    if not anomalies:
        print("ok: no anomalies")
        return 0
    for a in anomalies:
        print(f"{a.kind}  {a.id}")
    return 1


def _cmd_info(fs: Aloelite, args) -> int:
    size = os.path.getsize(args.file)
    wal = args.file + "-wal"
    wal_sz = os.path.getsize(wal) if os.path.exists(wal) else 0
    vols = fs.list_volumes()
    mounts = fs.list_mounts(include_unmounted=True)
    active = [m for m in mounts if getattr(m.state, "value", m.state) == "active"]
    print(f"{args.file}: {size} bytes" + (f" (+{wal_sz} WAL)" if wal_sz else ""))
    print(f"volumes: {len(vols)}")
    for v in vols:
        print("  " + _volume_line(fs, v))
    print(f"mounts: {len(active)} active, {len(mounts)} total rows")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="aloelite-admin",
        description="Aloelite maintenance utilities (volumes, keys, file health).",
    )
    ap.add_argument(
        "-v", "--version", action="version", version=f"%(prog)s {_version()}"
    )
    ap.add_argument(
        "-f",
        "--file",
        default=os.environ.get("ALOELITE_FILE"),
        help="path to the .sqlite/.fs file (default: $ALOELITE_FILE)",
    )
    ap.add_argument(
        "--volume",
        metavar="NAME_OR_ID",
        help="volume name or id (optional if the file has exactly one)",
    )
    add_pin_arguments(ap)  # the OLD pin, where one is needed
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("pin", help="PIN management (change)")
    psub = p.add_subparsers(dest="pin_cmd", required=True)
    pc = psub.add_parser("change", help="rotate a volume's PIN (K_v unchanged)")
    pc.add_argument("--new-pin-file", metavar="PATH", help="file holding the new PIN")
    pc.add_argument("--new-pin-env", metavar="VAR", help="env var holding the new PIN")

    p = sub.add_parser(
        "snapshot",
        help="fork a volume in-place as a new volume (near-free for plain; "
        "full copy for encrypted)",
    )
    p.add_argument("name", help="name for the snapshot volume")

    for verb, direction in (("export", "into DEST"), ("import", "from SRC")):
        p = sub.add_parser(
            verb,
            help=f"copy a volume {direction} (fresh key ladder; "
            "encrypted iff a dest pin is given)",
        )
        p.add_argument(
            "dest" if verb == "export" else "src",
            help="the other .fs file (created if missing on export/import)",
        )
        p.add_argument("--rename", metavar="NAME",
                       help="volume name on the destination (default: keep)")
        p.add_argument("--encrypt", action="store_true",
                       help="prompt (with confirm) for a destination PIN")
        p.add_argument("--dest-pin-file", metavar="PATH")
        p.add_argument("--dest-pin-env", metavar="VAR")

    p = sub.add_parser("verify", help="integrity-check committed content (exit 1 on problems)")
    p.add_argument("--deep", action="store_true",
                   help="also fetch, address-check, and decrypt every chunk")

    sub.add_parser("health", help="run the consistency check (exit 1 on anomalies)")
    sub.add_parser("info", help="one-screen file dossier")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not args.file:
        return _fail("no file given: pass -f or set ALOELITE_FILE")
    if not os.path.exists(args.file):
        return _fail(f"{args.file}: no such file")
    try:
        with Aloelite(args.file) as fs:
            if args.cmd == "pin" and args.pin_cmd == "change":
                return _cmd_pin_change(fs, args)
            if args.cmd == "snapshot":
                return _cmd_snapshot(fs, args)
            if args.cmd == "export":
                return _cmd_export(fs, args)
            if args.cmd == "import":
                return _cmd_import(fs, args)
            if args.cmd == "verify":
                return _cmd_verify(fs, args)
            if args.cmd == "health":
                return _cmd_health(fs, args)
            if args.cmd == "info":
                return _cmd_info(fs, args)
            return _fail("unknown command")
    except SystemExit as e:
        return int(e.code or 0)
    except errors.BadKey:
        return _fail("wrong PIN")
    except errors.EncryptionRequired as e:
        return _fail(str(e))
    except errors.FsError as e:
        return _fail(f"{e.code}: {e}")
    except OSError as e:
        return _fail(f"{type(e).__name__}: {e}")


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
