# ./tests/test_conformance_suite.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
Python runner for the shared conformance scenarios in conformance/scenarios/.

The scenarios are data — no Python in them — so the Rust, JS/WASM and Kotlin
implementations run the same oracle from their own runners rather than
hand-translating this file's assertions and hoping the two stay in agreement.
See conformance/README.md for the format.

This runner is deliberately thin. It resolves the scenario vocabulary onto the
flat function layer, and its only cleverness is the two mappings below:

  _ARG_ALIASES  scenario args use mount-api.yaml's parameter names, which are
                not always the Python signature's names (the spec says `from`
                and `to`; operations.py says `src` and `dst`).
  _decode       the tagged byte forms, so a fixture never has to guess at an
                encoding.

It does not replace tests/test_operations.py: that suite reaches into schema
state for things the API does not surface, which is exactly what a portable
scenario must not do.
"""

from __future__ import annotations

import base64
import inspect
from pathlib import Path
from typing import Any

import pytest
import yaml

from aloelite import errors
from aloelite import operations as ops
from aloelite.db import Db

_ROOT = Path(__file__).resolve().parent.parent
_SCENARIOS = _ROOT / "conformance" / "scenarios"
_SPEC = yaml.safe_load((_ROOT / "aloelite" / "config" / "mount-api.yaml").read_text())

# The spec's parameter names, mapped onto this binding's signatures.
_ARG_ALIASES = {"from": "src", "to": "dst"}

_BYTE_TAGS = {"utf8", "base64", "hex", "repeat"}


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def _load_all() -> list[tuple[str, dict[str, Any]]]:
    out = []
    for path in sorted(_SCENARIOS.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text())
        for scenario in doc["scenarios"]:
            out.append((scenario["scenario"], scenario))
    return out


_ALL = _load_all()


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------
def _decode(value: Any) -> bytes:
    """A tagged byte literal -> bytes. The tags are the format's whole
    encoding story; anything else in a bytes position is a fixture bug."""
    if not isinstance(value, dict):
        raise AssertionError(f"bytes must be tagged, got {value!r}")
    if "utf8" in value:
        return str(value["utf8"]).encode()
    if "base64" in value:
        return base64.b64decode(value["base64"])
    if "hex" in value:
        # whitespace is allowed so long fixtures can group bytes readably
        return bytes.fromhex("".join(str(value["hex"]).split()))
    if "repeat" in value:
        return _decode(value["repeat"]) * int(value["count"])
    raise AssertionError(f"unknown byte tag in {value!r}")


def _is_bytes_literal(value: Any) -> bool:
    return isinstance(value, dict) and bool(_BYTE_TAGS & set(value))


def _deref(value: Any, binds: dict[str, Any]) -> Any:
    """Resolve {ref: name} / {ref: name.field} against earlier bindings."""
    name, _, field = str(value["ref"]).partition(".")
    assert name in binds, f"unbound ref {name!r}"
    target = binds[name]
    if not field:
        return target
    assert hasattr(target, field), f"{name} has no field {field!r}"
    return getattr(target, field)


def _resolve_args(args: dict[str, Any], binds: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for key, value in args.items():
        if _is_bytes_literal(value):
            value = _decode(value)
        elif isinstance(value, dict) and "ref" in value:
            value = _deref(value, binds)
        out[_ARG_ALIASES.get(key, key)] = value
    return out


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
def _sort_key(entry: dict[str, Any]) -> tuple:
    # (name, hidden-last) — never the engine's natural row order
    return (str(entry.get("name", "")), not entry.get("visible", True))


def _as_dict(value: Any) -> Any:
    return value.model_dump() if hasattr(value, "model_dump") else value


def _match(expected: Any, actual: Any, binds: dict[str, Any], where: str) -> None:
    if _is_bytes_literal(expected):
        assert actual == _decode(expected), f"{where}: bytes differ"
        return
    if isinstance(expected, dict) and "ref" in expected:
        assert actual == _deref(expected, binds), f"{where}: not the expected value"
        return
    if isinstance(expected, list):
        got = [_as_dict(item) for item in actual]
        if expected and all(isinstance(item, dict) for item in expected):
            expected = sorted(expected, key=_sort_key)
            got = sorted(got, key=_sort_key)
        assert len(expected) == len(got), (
            f"{where}: expected {len(expected)}, got {len(got)}"
        )
        for i, (e, a) in enumerate(zip(expected, got)):
            _match(e, a, binds, f"{where}[{i}]")
        return
    if isinstance(expected, dict):
        got = _as_dict(actual)
        assert isinstance(got, dict), f"{where}: expected a record, got {actual!r}"
        for key, sub in expected.items():
            assert key in got, f"{where}: no field {key!r} in {sorted(got)}"
            _match(sub, got[key], binds, f"{where}.{key}")
        return
    # scalars: enums compare by value, everything else by equality
    actual = getattr(actual, "value", actual)
    assert expected == actual, f"{where}: expected {expected!r}, got {actual!r}"


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
def _call(op: str, args: dict[str, Any], db: Db, mount: str) -> Any:
    fn = getattr(ops, op, None)
    assert fn is not None, f"no operations.{op}"
    accepted = set(inspect.signature(fn).parameters)
    unknown = set(args) - accepted
    assert not unknown, f"{op} got unknown args {sorted(unknown)}"
    return fn(db, mount, **args)


def _run(scenario: dict[str, Any], tmp_path: Path) -> None:
    setup = scenario.get("setup") or {}
    db = Db.open(
        str(tmp_path / "conformance.fs"),
        _ROOT / "aloelite/config/sql-templates.yaml",
        schema_path=_ROOT / "aloelite/sql/schema.sql",
    )
    try:
        volume = ops.create_volume(db, "conformance", setup.get("chunk_size", 1048576))
        mount = ops.mount(db, volume.id)
        binds: dict[str, Any] = {}
        for i, step in enumerate(scenario["steps"]):
            where = f"step {i} ({step['op']})"
            args = _resolve_args(step.get("args") or {}, binds)
            if "raises" in step:
                with pytest.raises(errors.FsError) as caught:
                    _call(step["op"], args, db, mount)
                assert caught.value.code == step["raises"], (
                    f"{where}: expected {step['raises']}, raised {caught.value.code}"
                )
                continue
            result = _call(step["op"], args, db, mount)
            if "bind" in step:
                binds[step["bind"]] = result
            if "expect" in step:
                _match(step["expect"], result, binds, where)
    finally:
        db.close()


@pytest.mark.parametrize("name,scenario", _ALL, ids=[n for n, _ in _ALL])
def test_scenario(name: str, scenario: dict[str, Any], tmp_path: Path) -> None:
    _run(scenario, tmp_path)


# ---------------------------------------------------------------------------
# The fixtures themselves are checked against the spec, so a scenario cannot
# quietly invent an operation or an error code that no implementation owes.
# ---------------------------------------------------------------------------
def _declared_ops() -> set[str]:
    names = set()
    for entries in _SPEC["operations"].values():
        if isinstance(entries, dict):
            names |= {op for op in entries if op != "note"}
    names |= {f"{op}_by_id" for op in _SPEC["id_variants"]["applies_to"]}
    return names


def test_scenarios_only_use_declared_operations():
    declared = _declared_ops()
    for name, scenario in _ALL:
        for step in scenario["steps"]:
            assert step["op"] in declared, (
                f"{name}: {step['op']} is not in mount-api.yaml"
            )


def test_scenarios_only_expect_declared_errors():
    closed = set(_SPEC["errors"])
    for name, scenario in _ALL:
        for step in scenario["steps"]:
            code = step.get("raises")
            assert code is None or code in closed, (
                f"{name}: {code} is not a declared error"
            )


def test_scenario_names_are_unique():
    names = [name for name, _ in _ALL]
    assert len(names) == len(set(names)), (
        "scenario names are test ids; they must be unique"
    )


def test_every_scenario_cites_requirements():
    for name, scenario in _ALL:
        assert scenario.get("requirements"), f"{name}: no requirement ids cited"


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
