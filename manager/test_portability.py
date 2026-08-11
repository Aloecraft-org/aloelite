# ./manager/test_portability.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
Host-portability guards for the manager entrypoint.

WebDAV exists largely so Windows and macOS can reach a volume at all, since
FUSE was never an option there. That makes the manager's own ability to START
on those hosts part of the feature, not a separate concern -- a POSIX-only call
on the startup path takes the whole frontend down before it binds a socket, and
nothing in a Linux CI run notices.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture
def windows_like(monkeypatch):
    """A host without os.geteuid -- what Windows actually looks like to
    CPython.

    os.getuid is deliberately LEFT in place even though Windows lacks it too:
    pytest's own tmp_path goes through getpass.getuser(), which calls it, so
    removing it fails the fixture rather than the code under test. geteuid is
    the one that actually crashed the entrypoint.
    """
    monkeypatch.delattr(os, "geteuid", raising=False)
    return monkeypatch


def test_web_entrypoint_starts_on_a_host_without_geteuid(tmp_path, windows_like):
    """os.geteuid does not exist on Windows. It was called unconditionally to
    print a note about /root/.aloelite, so `aloelite-web --webdav` died with
    AttributeError before serving anything -- on the one platform where WebDAV
    is the only way in."""
    import manager.web as web

    reached: list[bool] = []
    windows_like.setattr(web, "_version", lambda: "test")
    windows_like.setattr(
        "sys.argv", ["aloelite-web", "--webdav", "--root", str(tmp_path)]
    )
    # Stop before the real server: this test is about reaching it at all.
    import manager.__main__ as entry

    windows_like.setattr(entry, "main", lambda: (reached.append(True), 0)[1])
    assert web.main() == 0
    assert reached, "main() returned before handing off to the server"


def test_webdav_and_tls_flags_survive_the_windows_path(tmp_path, windows_like):
    """The flags that matter on Windows specifically must still be wired
    through once the crash above is fixed."""
    import manager.__main__ as entry
    import manager.web as web

    windows_like.setattr(
        "sys.argv",
        ["aloelite-web", "--webdav", "--tls-self-signed", "--root", str(tmp_path)],
    )
    windows_like.setattr(entry, "main", lambda: 0)
    web.main()
    assert os.environ.get("ALOELITE_WEBDAV") == "1"
    assert os.environ.get("ALOELITE_TLS_SELF_SIGNED") == "1"


def test_no_posix_only_calls_on_the_startup_import_path():
    """A cheap guard against the same class of bug returning somewhere else on
    the path a plain `aloelite-web --webdav` actually executes."""
    import pathlib

    banned = ("os.geteuid", "os.getuid", "os.setsid", "os.fork", "os.killpg")
    root = pathlib.Path(__file__).resolve().parent
    offenders = []
    for name in ("web.py", "__main__.py", "api.py", "dav.py", "davlock.py", "tls.py"):
        text = (root / name).read_text()
        for call in banned:
            # A hasattr guard may wrap onto its own line, so the check is
            # per-FILE rather than per-line: using the call at all requires
            # the file to guard it somewhere.
            if f"{call}(" in text and f'hasattr(os, "{call[3:]}")' not in text:
                offenders.append(f"{name}: unguarded {call}")
    assert not offenders, "POSIX-only calls on the startup path: " + "; ".join(
        offenders
    )


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
