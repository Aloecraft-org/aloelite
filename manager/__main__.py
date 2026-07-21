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


def main() -> int:
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
    print(f"aloelite manager: http://{'localhost' if host in ('127.0.0.1', '::1') else host}:{port}/admin")
    print(f"  data root: {ALOELITE_ROOT}")
    if direct_only:
        print("  mode: direct only (browser access; set ALOELITE_DIRECT_ONLY=0 for FUSE)")
    # threaded=True: mount/export endpoints block; serve them concurrently.
    try:
        app.run(host=host, port=port, threaded=True)
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
