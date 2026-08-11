# ./manager/__main__.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
manager.__main__ — process entrypoint.

  python3 -m manager

Wires the store, supervisor, and API together; runs preflight before serving;
installs SIGTERM/SIGINT handlers that shut the supervisor down cleanly.
"""

from __future__ import annotations

import os
import signal
import sys

from .api import ALOELITE_ROOT, HOST_MNT_PREFIX, create_app
from .preflight import MANAGER_MNT, run_preflight
from .store import JsonVolumeStore
from .supervisor import MountSupervisor

VOLUMES_JSON = os.path.join(ALOELITE_ROOT, "volumes.json")


def build(store=None, supervisor=None, registry=None):
    """Construct the (store, supervisor, app) triple. Exposed for tests."""
    from .direct import DirectSessionRegistry

    store = store or JsonVolumeStore(VOLUMES_JSON)
    supervisor = supervisor or MountSupervisor(
        store, aloelite_root=ALOELITE_ROOT, mnt_dir=MANAGER_MNT
    )
    registry = registry or DirectSessionRegistry()
    app = create_app(
        store,
        supervisor,
        registry=registry,
        aloelite_root=ALOELITE_ROOT,
        host_mnt_prefix=HOST_MNT_PREFIX,
    )
    return store, supervisor, app


_LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _tls_context(host: str):
    """Resolve TLS material, or refuse to serve credentials in the clear.

    Returns an ssl_context for app.run, or None for plain HTTP.

    The refusal is narrow and deliberate. WebDAV authenticates with HTTP Basic
    where the password IS the volume PIN, so serving it unencrypted on a
    non-loopback address puts that PIN on the wire in base64 on every request
    -- and a single directory listing is dozens of requests. That is not a
    hardening nicety to warn about; it is handing out the key to the volume.
    Loopback is unaffected, plain HTTP without WebDAV is unaffected (the JSON
    API's own auth is a separate question, already warned about above), and
    ALOELITE_INSECURE=1 exists for someone terminating TLS in a reverse proxy,
    which is a legitimate and common deployment.
    """
    cert = os.environ.get("ALOELITE_TLS_CERT", "")
    key = os.environ.get("ALOELITE_TLS_KEY", "")
    self_signed = os.environ.get("ALOELITE_TLS_SELF_SIGNED", "") not in ("", "0")
    webdav = os.environ.get("ALOELITE_WEBDAV", "") not in ("", "0")
    insecure = os.environ.get("ALOELITE_INSECURE", "") not in ("", "0")

    if cert or key:
        if not (cert and key):
            print(
                "error: --tls-cert and --tls-key must be given together",
                file=sys.stderr,
            )
            raise SystemExit(2)
        for label, path in (("certificate", cert), ("private key", key)):
            if not os.path.exists(path):
                print(f"error: TLS {label} not found: {path}", file=sys.stderr)
                raise SystemExit(2)
        print(f"  tls: {cert}")
        return (cert, key)

    if self_signed:
        from .tls import ensure_self_signed, fingerprint

        cert, key = ensure_self_signed(ALOELITE_ROOT, host)
        print(f"  tls: self-signed {cert}")
        print(f"  sha256: {fingerprint(cert)}")
        print(
            "  self-signed: browsers warn, and the Windows WebDAV redirector "
            "refuses outright until this certificate is trusted on the client "
            "(compare the fingerprint above). See doc/WEBDAV.md."
        )
        return (cert, key)

    if webdav and host not in _LOOPBACK and not insecure:
        print(
            f"error: refusing to serve WebDAV on {host} without TLS.\n"
            "  WebDAV authenticates with HTTP Basic and the password is the "
            "volume PIN, so this would put the PIN on the wire in cleartext "
            "on every request.\n"
            "  Fix by one of:\n"
            "    --tls-self-signed          (encrypts; must be trusted per client)\n"
            "    --tls-cert C --tls-key K   (a certificate clients already trust)\n"
            "    --host 127.0.0.1           (keep it on loopback)\n"
            "    --insecure                 (you terminate TLS in front of this)",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return None


def main() -> int:
    if any(a in ("-v", "--version") for a in sys.argv[1:]):
        from manager.web import _version

        print(f"aloelite {_version()}")
        return 0
    os.makedirs(ALOELITE_ROOT, exist_ok=True)
    store, supervisor, app = build()
    results = run_preflight(store, aloelite_root=ALOELITE_ROOT, mnt=MANAGER_MNT)
    app.config["PREFLIGHT_RESULTS"] = [
        {"name": r.name, "ok": r.ok, "fatal": r.fatal, "detail": r.detail}
        for r in results
    ]
    for mp in supervisor.auto_mount_all(log=app.logger.warning):
        app.logger.info("auto-mounted %s", mp)

    def _shutdown(signum, _frame):
        # Do NOT tear down here: blocking in a signal handler leaves the
        # process (and the port) alive. Just break out of app.run; the
        # finally below cleans up after the socket has closed.
        app.logger.info("signal %s received; shutting down", signum)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # The manager has no authentication. In direct-only mode (the local,
    # no-container on-ramp) bind loopback unless explicitly overridden; the
    # container/provisioner deployment keeps the 0.0.0.0 default.
    direct_only = os.environ.get("ALOELITE_DIRECT_ONLY", "") not in ("", "0")
    default_host = "127.0.0.1" if direct_only else "0.0.0.0"
    host = os.environ.get("ALOELITE_API_HOST", default_host)
    if direct_only and host not in ("127.0.0.1", "localhost", "::1"):
        app.logger.warning(
            "binding %s: the manager API has no authentication — anyone who "
            "can reach this address can read and write every volume",
            host,
        )
    port = int(os.environ.get("ALOELITE_API_PORT", "8080"))

    ssl_context = _tls_context(host)
    scheme = "https" if ssl_context else "http"

    display_host = "localhost" if host in ("127.0.0.1", "::1") else host
    print(f"aloelite manager: {scheme}://{display_host}:{port}/admin")
    print(f"  data root: {ALOELITE_ROOT}")
    if direct_only:
        print(
            "  mode: direct only (browser access; set ALOELITE_DIRECT_ONLY=0 for FUSE)"
        )
    # threaded=True: mount/export endpoints block; serve them concurrently.
    try:
        app.run(host=host, port=port, threaded=True, ssl_context=ssl_context)
    finally:
        # Socket is closed by the time we get here. A second Ctrl-C during a
        # hung teardown raises KeyboardInterrupt and kills the process.
        try:
            supervisor.shutdown()
            app.config["DIRECT_REGISTRY"].shutdown()
        finally:
            store.close()
    return 0


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
