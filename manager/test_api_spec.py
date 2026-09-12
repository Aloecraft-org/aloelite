# ./manager/test_api_spec.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
manager/api-spec.yaml is projected onto the running Flask app, the way
tests/test_spec_projection.py projects mount-api.yaml onto the engine.

The point is drift: a route spec that is only prose describes the manager as
it was on the day someone wrote it down. These tests fail when api.py and the
spec disagree in EITHER direction — a route added without a spec entry, or a
spec entry for a route that no longer exists — which is what makes the file
usable as the contract a second-language manager implements.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from manager.api import create_app
from manager.engine.store import JsonVolumeStore

_SPEC_PATH = Path(__file__).resolve().parent / "api-spec.yaml"
_SPEC = yaml.safe_load(_SPEC_PATH.read_text())

# Framework-provided endpoints that are not part of the manager's contract.
_NOT_CONTRACT = {"static"}

_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    root = tmp_path_factory.mktemp("apispec")
    store = JsonVolumeStore(str(root / "store.json"))
    try:
        yield create_app(store, supervisor=None, aloelite_root=str(root))
    finally:
        store.close()


def _live_routes(app) -> set[tuple[str, str]]:
    out = set()
    for rule in app.url_map.iter_rules():
        if rule.endpoint in _NOT_CONTRACT:
            continue
        for method in rule.methods & _METHODS:
            out.add((method, str(rule.rule)))
    return out


def _spec_routes() -> set[tuple[str, str]]:
    return {(r["method"], r["path"]) for r in _SPEC["routes"]}


def test_every_live_route_is_specified(app):
    missing = sorted(_live_routes(app) - _spec_routes())
    assert not missing, (
        f"routes exist in manager/api.py but not in manager/api-spec.yaml: {missing}"
    )


def test_every_specified_route_exists(app):
    stale = sorted(_spec_routes() - _live_routes(app))
    assert not stale, (
        f"manager/api-spec.yaml describes routes manager/api.py does not serve: {stale}"
    )


def test_route_entries_are_well_formed():
    seen = set()
    for route in _SPEC["routes"]:
        key = (route["method"], route["path"])
        assert key not in seen, f"duplicate spec entry for {key}"
        seen.add(key)
        assert route["method"] in _METHODS, f"{key}: unknown method"
        assert route.get("summary"), f"{key}: no summary"
        assert route.get("responses"), f"{key}: no responses"
        for code in route["responses"]:
            assert isinstance(code, int) and 100 <= code <= 599, (
                f"{key}: {code!r} is not an HTTP status code "
                "(YAML keys like 200 must stay unquoted integers)"
            )
        auth = route.get("auth", "open")
        assert auth in ("open", "gated"), f"{key}: unknown auth mode {auth!r}"


# The BEHAVIOR behind `auth: gated` (401 without a session, per-client
# sessions, CSRF) is exercised end to end in manager/test_web_files.py, which
# owns the session fixtures, and in the real browser by
# script/browser_check.py. This module deliberately checks only that the spec
# and the route table agree — a second assertion of the same behavior here
# would be a weaker duplicate of a stronger test.


def test_auth_section_documents_the_real_credential_names():
    """The header/cookie names are what a port must reproduce byte-for-byte,
    so pin them against api.py rather than trusting the prose."""
    src = (Path(__file__).resolve().parent / "api.py").read_text()
    assert _SPEC["auth"]["credential"]["header"] in src
    assert _SPEC["auth"]["csrf"]["header"].split(":")[0] in src
    cookie_template = _SPEC["auth"]["credential"]["cookie"]
    assert cookie_template.split("<")[0] in src  # the 'aloe_m_' prefix


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
