# ./manager/web.py
# License: Apache-2.0
"""
manager.web — the `aloelite-web` console entrypoint.

`aloelite-web` with no arguments is the lowest-commitment path: direct
(browser) mode, loopback bind, ~/.aloelite root, no sudo, no FUSE
preflight. FUSE provisioning is opt-in with ALOELITE_DIRECT_ONLY=0
(the container's `python3 -m manager` is unaffected — it defaults to
the full FUSE mode as before).

The env default must land before manager.api is imported, because
ALOELITE_ROOT is resolved at import time — hence this wrapper module
with its deferred import.
"""

import argparse
import os


def _version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("aloelite")
    except PackageNotFoundError:
        return "unknown (not installed)"


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="aloelite-web",
        description="Aloelite web manager — browse, upload, and download "
        "files in aloelite filesystems from a browser.",
        epilog="Environment variables ALOELITE_API_PORT, ALOELITE_API_HOST, "
        "ALOELITE_ROOT, ALOELITE_DIRECT_ONLY, and ALOELITE_WEBDAV are honored "
        "when the corresponding flag is absent.",
    )
    ap.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"%(prog)s {_version()}",
    )
    ap.add_argument("-p", "--port", type=int, help="listen port (default 8080)")
    ap.add_argument(
        "--host",
        help="bind address (default 127.0.0.1; the API has no "
        "authentication, so binding wider exposes every volume)",
    )
    ap.add_argument("--root", help="data directory (default ~/.aloelite)")
    ap.add_argument(
        "--auth",
        choices=["cookie", "off"],
        help="client auth for encrypted volume content (default cookie: "
        "each browser/device proves the PIN once and holds its own "
        "session; without one, encrypted content answers 401). 'off': any "
        "client may use an unlocked volume (trusted networks).",
    )
    ap.add_argument(
        "--fuse",
        action="store_true",
        help="enable FUSE provisioning mode (container-grade preflight)",
    )
    ap.add_argument(
        "--webdav",
        action="store_true",
        help="serve every volume over WebDAV at /dav/<volume-id> (RFC 4918 "
        "class 1: no locking, so macOS Finder mounts read-only). Off by "
        "default; encrypted volumes authenticate with HTTP Basic where the "
        "password is the PIN, so bind loopback or put TLS in front.",
    )
    args = ap.parse_args()

    if args.port is not None:
        os.environ["ALOELITE_API_PORT"] = str(args.port)
    if args.host:
        os.environ["ALOELITE_API_HOST"] = args.host
    if args.root:
        os.environ["ALOELITE_ROOT"] = args.root
    if args.auth:
        os.environ["ALOELITE_AUTH"] = args.auth
    if args.fuse:
        os.environ["ALOELITE_DIRECT_ONLY"] = "0"
    if args.webdav:
        os.environ["ALOELITE_WEBDAV"] = "1"
    os.environ.setdefault("ALOELITE_DIRECT_ONLY", "1")

    if os.geteuid() == 0 and not args.root and not os.environ.get("ALOELITE_ROOT"):
        print(
            "note: running as root — data will live in /root/.aloelite. "
            "sudo is not required for direct mode."
        )

    from .__main__ import main as _main

    return _main()
