# ./tests/test_spec_projection.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
Binds the Python projection to mount-api.yaml.

mount-api.yaml calls itself the "shared contract for all four implementations",
and errors.py / types.py / models.py each carry a docstring saying they must
stay in lockstep with it. Nothing enforced that: the YAML was documentation, and
the lockstep was human discipline. With one implementation that is survivable;
with two it is how they diverge, silently, in the error enum.

This module makes the drift mechanical. It is deliberately structural — names,
field sets, closed enums, reachability — and never asserts prose, because
descriptions are allowed to read differently in each language's idiom while the
contract stays the same.

Two allowlists below (_OPERATION_PROJECTION, _UNIMPLEMENTED_BY_ID) record where
Python legitimately differs from the flat spec, and where it simply has not
caught up. Both are ratchets: implementing something the spec promises fails
this test until the allowlist shrinks to match.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest
import yaml

from aloelite import errors, models, types
from aloelite import operations as ops
from aloelite.descriptor import Descriptor

_SPEC_PATH = (
    Path(__file__).resolve().parent.parent / "aloelite" / "config" / "mount-api.yaml"
)


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    return yaml.safe_load(_SPEC_PATH.read_text())


def _fields(record: dict[str, Any]) -> set[str]:
    """A record's field names — `note` is documentation, not a field."""
    return {k for k in record if k != "note"}


# ---------------------------------------------------------------------------
# Errors — the set is closed, so this is an equality check in both directions.
# ---------------------------------------------------------------------------
def test_error_codes_match_the_closed_set(spec):
    # FsError itself is the base, not a variant; its code is the sentinel.
    coded = {code for code in errors.BY_CODE if code != errors.FsError.code}
    assert coded == set(spec["errors"]), (
        "the error set is CLOSED: a variant must exist in errors.py and "
        "mount-api.yaml together"
    )


def test_every_error_class_is_exported(spec):
    for code in spec["errors"]:
        cls = errors.BY_CODE[code]
        assert cls.__name__ in errors.__all__, f"{cls.__name__} missing from __all__"


# ---------------------------------------------------------------------------
# Enums — closed, and ORDER matters: these project to positional variants in
# languages with C-style enums, so a reordering is a wire-format break.
# ---------------------------------------------------------------------------
def test_enums_match_in_name_and_order(spec):
    for name, declared in spec["enums"].items():
        values = declared if isinstance(declared, list) else declared["values"]
        cls = getattr(types, name, None)
        assert cls is not None, f"enum {name} has no projection in types.py"
        assert [member.value for member in cls] == values, f"{name} drifted"


# ---------------------------------------------------------------------------
# Scalars — only the primitive-based ones project to NewTypes. `opaque` and
# `binary` bases are platform handles (FsHandle, DbRef) or native byte strings
# (Bytes); they have no Python-side declaration to check.
# ---------------------------------------------------------------------------
def test_primitive_scalars_project_to_newtypes(spec):
    for name, declared in spec["scalars"].items():
        if declared.get("base") not in ("string", "int"):
            continue
        projected = getattr(types, name, None)
        assert projected is not None, f"scalar {name} missing from types.py"
        assert hasattr(projected, "__supertype__"), f"{name} should be a NewType"


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------
# Descriptor is the one record that is NOT a plain pydantic model in Python: the
# spec models the streaming handle as data crossing the boundary, while the
# binding hands back a live object owning the lock lifecycle (exactly what the
# `streaming` section says bindings should do). Its fields are constructor
# parameters instead of model fields.
_RECORDS_AS_OBJECTS = {"Descriptor": Descriptor}


def test_records_match_field_for_field(spec):
    for name, declared in spec["records"].items():
        expected = _fields(declared)
        if name in _RECORDS_AS_OBJECTS:
            got = set(inspect.signature(_RECORDS_AS_OBJECTS[name].__init__).parameters)
            assert expected <= got, f"{name} is missing {sorted(expected - got)}"
            continue
        model = getattr(models, name, None)
        assert model is not None, f"record {name} missing from models.py"
        assert set(model.model_fields) == expected, f"{name} drifted"


def test_no_undeclared_public_records():
    """A record that exists in Python but not in the spec is a contract another
    implementation cannot know to return."""
    declared = set(yaml.safe_load(_SPEC_PATH.read_text())["records"])
    public = {
        name
        for name in models.__all__
        if inspect.isclass(getattr(models, name, None))
        and hasattr(getattr(models, name), "model_fields")
    }
    assert public <= declared, (
        f"undeclared in mount-api.yaml: {sorted(public - declared)}"
    )


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------
# Most operations are flat functions in operations.py. These are the ones that
# are not, and why — each is an object method in Python because the spec's own
# `streaming` and `session` notes say bindings wrap them that way. Listing them
# here (rather than skipping the check) means deleting one still fails.
_OPERATION_PROJECTION = {
    "session.open": ("aloelite.db", "Db", "open"),
    "session.close": ("aloelite.db", "Db", "close"),
    "streaming.read": ("aloelite.descriptor", "Descriptor", "read"),
    "streaming.write": ("aloelite.descriptor", "Descriptor", "write"),
    "streaming.seek": ("aloelite.descriptor", "Descriptor", "seek"),
    "streaming.tell": ("aloelite.descriptor", "Descriptor", "tell"),
    "streaming.close": ("aloelite.descriptor", "Descriptor", "close"),
    "streaming.abort": ("aloelite.descriptor", "Descriptor", "abort"),
}


def _declared_operations(spec) -> list[tuple[str, str]]:
    out = []
    for group, entries in spec["operations"].items():
        if not isinstance(entries, dict):
            continue
        out += [(group, op) for op in entries if op != "note"]
    return out


def test_every_declared_operation_is_reachable(spec):
    import importlib

    for group, op in _declared_operations(spec):
        key = f"{group}.{op}"
        if key in _OPERATION_PROJECTION:
            module, cls, attr = _OPERATION_PROJECTION[key]
            owner = getattr(importlib.import_module(module), cls)
            assert callable(getattr(owner, attr, None)), (
                f"{key} -> {cls}.{attr} missing"
            )
        else:
            assert callable(getattr(ops, op, None)), f"{key} has no operations.{op}"


def test_no_undeclared_public_operations(spec):
    """The reverse direction, and the one that actually bit: a function shipped
    in operations.py but absent from the spec is a capability no other
    implementation knows it owes. Found append/truncate/write_range/
    set_metadata/set_mtime/set_retention/list_mounts/prune_content this way."""
    declared = {op for _, op in _declared_operations(spec)}
    declared |= {f"{op}_by_id" for op in spec["id_variants"]["applies_to"]}
    public = {
        name
        for name, obj in vars(ops).items()
        if inspect.isfunction(obj)
        and not name.startswith("_")
        and obj.__module__ == "aloelite.operations"
    }
    assert public <= declared, (
        f"undeclared in mount-api.yaml: {sorted(public - declared)}"
    )


# ---------------------------------------------------------------------------
# The `locks` flag — the annotation a port cannot afford to read wrong.
# ---------------------------------------------------------------------------
# Markers that mean "this operation touches the advisory lock table". Source
# inspection rather than behaviour: asserting the flag by *provoking* a
# conflict would need a live second mount per operation, and the point here is
# a cheap total check over every declared op, not a semantics test (those live
# in test_operations.py and conformance/scenarios/locking.yaml).
_LOCK_MARKERS = ("check_lock_held", "_require_own_lock", "mutation.create_lock")


def test_lock_flag_matches_the_implementation(spec):
    """`locks` was documentation, and it had already drifted.

    The ACC-11 work taught move/remove/remove_recursive/set_metadata to refuse
    a locked node but left all four declaring `locks: false`, and unlock /
    renew_lock never declared the flag at all. Nothing noticed, because nothing
    read it — which is exactly the failure mode this module exists to end: a
    second implementation reads the flag, not the Python.

    The reverse direction matters just as much. An operation added without
    considering locking silently declares `locks: false` by omission, and that
    is a decision, not a default. This test turns it into a visible one.
    """
    mismatched = []
    for _group, op in _declared_operations(spec):
        fn = getattr(ops, op, None)
        if fn is None or not inspect.isfunction(fn):
            continue  # reachability is test_every_declared_operation_is_reachable's job
        declared = bool((spec_flags(spec, op) or {}).get("locks", False))
        actual = any(m in inspect.getsource(fn) for m in _LOCK_MARKERS)
        if declared != actual:
            mismatched.append(f"{op}: spec says locks={declared}, code says {actual}")
    assert not mismatched, (
        "mount-api.yaml disagrees with operations.py:\n  " + "\n  ".join(mismatched)
    )


def spec_flags(spec, op: str) -> dict[str, Any] | None:
    for entries in spec["operations"].values():
        if isinstance(entries, dict) and isinstance(entries.get(op), dict):
            return entries[op].get("flags")
    return None


def test_declared_errors_are_the_only_ones_raised(spec):
    """Every `raises` entry must name a variant of the closed set."""
    closed = set(spec["errors"])
    for group, entries in spec["operations"].items():
        if not isinstance(entries, dict):
            continue
        for op, declared in entries.items():
            if op == "note" or not isinstance(declared, dict):
                continue
            unknown = set(declared.get("raises", [])) - closed
            assert not unknown, f"{group}.{op} raises undeclared {sorted(unknown)}"


# ---------------------------------------------------------------------------
# id-addressed variants
# ---------------------------------------------------------------------------
# The spec promises a *_by_id form for every path-addressed structural and read
# operation (NODE-5: hidden same-name siblings are reachable only by id). Python
# has one. This is a real gap, recorded rather than hidden — shrink the set as
# each lands, and the test tells you when you have forgotten to.
_UNIMPLEMENTED_BY_ID = {
    "list",
    "read_all",
    "move",
    "copy",
    "rename",
    "remove",
    "remove_recursive",
    "pack",
    "unpack",
    "open_read",
    "open_write",
}


def test_id_variant_gap_is_exactly_what_we_think_it_is(spec):
    promised = set(spec["id_variants"]["applies_to"])
    missing = {op for op in promised if not hasattr(ops, f"{op}_by_id")}
    assert missing == _UNIMPLEMENTED_BY_ID, (
        "the id-addressed surface moved: update _UNIMPLEMENTED_BY_ID "
        f"(now missing {sorted(missing)})"
    )


def test_implemented_id_variants_take_a_node_not_a_path(spec):
    for op in set(spec["id_variants"]["applies_to"]) - _UNIMPLEMENTED_BY_ID:
        params = list(inspect.signature(getattr(ops, f"{op}_by_id")).parameters)
        assert "node" in params, f"{op}_by_id should take a node id, got {params}"


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
