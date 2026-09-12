# ./tests/test_cli_contract.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
Binds the Python CLI to aloelite/config/cli.yaml.

The CLI had no spec: 754 lines of argparse, documented by example in the
README, tested by a lean end-to-end file. Porting it made the missing
contract concrete, so cli.yaml now names every verb, positional, flag and
global, and this module projects the argparse tree onto it in both
directions — a verb or a flag cannot be added to one without the other. The
Rust twin is rust/aloelite-cli/tests/contract.rs, against the same file.

Structural only: option strings, positional names and optionality, scopes'
membership. Help text is each implementation's own.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest
import yaml

from aloelite.cli import _FS_VERBS, _MOUNT_VERBS, _build_parser

_CONTRACT = Path(__file__).resolve().parent.parent / "aloelite" / "config" / "cli.yaml"


@pytest.fixture(scope="module")
def contract() -> dict[str, Any]:
    return yaml.safe_load(_CONTRACT.read_text())


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _shape(
    parser: argparse.ArgumentParser,
) -> tuple[list[tuple[str, bool]], dict[str, list[str]]]:
    """(positionals as (name, optional), flags as dest -> option strings),
    ignoring help and the subparsers action."""
    positionals: list[tuple[str, bool]] = []
    flags: dict[str, list[str]] = {}
    for action in parser._actions:
        if isinstance(action, (argparse._HelpAction, argparse._SubParsersAction)):
            continue
        if action.option_strings:
            flags[action.dest] = list(action.option_strings)
        else:
            positionals.append((action.dest, action.nargs == "?"))
    return positionals, flags


def _declared_args(spec: dict[str, Any]) -> list[tuple[str, bool]]:
    return [(a["name"], bool(a.get("optional"))) for a in spec.get("args", [])]


def test_verbs_match_in_both_directions(contract):
    declared = set(contract["verbs"])
    parsed = set(_subparsers(_build_parser()))
    assert parsed == declared, f"argparse {parsed ^ declared} differ from cli.yaml"
    # and the dispatch tables cover exactly the declared verbs
    assert set(_MOUNT_VERBS) | set(_FS_VERBS) == declared


def test_each_verb_has_the_declared_shape(contract):
    subs = _subparsers(_build_parser())
    for name, spec in contract["verbs"].items():
        parser = subs[name]
        positionals, flags = _shape(parser)
        if "sub" in spec:
            # a verb with sub-verbs: its own positionals are the sub-verb name
            nested = _subparsers(parser)
            assert set(nested) == set(spec["sub"]), f"{name}: sub-verbs differ"
            for sub_name, sub_spec in spec["sub"].items():
                sub_pos, sub_flags = _shape(nested[sub_name])
                assert sub_pos == _declared_args(sub_spec), (
                    f"{name} {sub_name}: positionals"
                )
                assert sub_flags == {}, (
                    f"{name} {sub_name}: unexpected flags {sub_flags}"
                )
            assert flags == {}, f"{name}: unexpected flags {flags}"
            continue
        assert positionals == _declared_args(spec), f"{name}: positionals differ"
        declared_flags = {k: list(v) for k, v in spec.get("flags", {}).items()}
        assert flags == declared_flags, f"{name}: flags differ"


def test_scopes_match_the_dispatch_tables(contract):
    for name, spec in contract["verbs"].items():
        table = _MOUNT_VERBS if spec["scope"] == "mount" else _FS_VERBS
        assert name in table, f"{name} is declared {spec['scope']}-scoped"


def test_globals_match(contract):
    _, flags = _shape(_build_parser())
    # argparse's version action carries its own dest; compare by option strings
    parsed = {tuple(v) for v in flags.values()}
    declared = {tuple(g["flags"]) for g in contract["globals"].values()}
    assert parsed == declared
    # the one optional-value flag is --pin and only --pin
    pin_actions = [a for a in _build_parser()._actions if a.option_strings == ["--pin"]]
    assert pin_actions and pin_actions[0].nargs == "?"
    optional_valued = {
        k for k, g in contract["globals"].items() if g.get("optional_value")
    }
    assert optional_valued == {"pin"}


def test_delegations_are_the_documented_three(contract):
    assert contract["delegations"]["names"] == ["fuse", "web", "admin"]


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
