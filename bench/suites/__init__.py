# ./bench/suites/__init__.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
The registry. Every benchmark reachable from the CLI is one entry here, and
adding a benchmark means adding a line here and nothing else.

Surface
-------
Entry points
  SUITES         name -> loader; the whole fan-out of `python -m bench`
  GROUPS         the named subsets CI runs
  load(name)     resolve one suite to its callable

Configurable values
  GROUPS         which suites belong to `core`, `deep`, `external`

Fan-out points
  SUITES is the dispatch table. Suites are imported lazily so that one that
  needs an absent tool (external) cannot stop the rest from running.
"""

from __future__ import annotations

from typing import Callable

# name -> "module:function", resolved on demand.
SUITES: dict[str, str] = {
    "throughput": "bench.suites.fsio:throughput",
    "smallfile": "bench.suites.fsio:smallfile",
    "random": "bench.suites.fsio:random_read",
    "append": "bench.suites.fsio:append_latency",
    "space": "bench.suites.space:space",
    "dedup": "bench.suites.space:dedup",
    "versions": "bench.suites.space:versions",
    "prune": "bench.suites.space:prune_reclaim",
    "ingest_scale": "bench.suites.scale:ingest_scale",
    "dir_scale": "bench.suites.scale:dir_scale",
    "memory": "bench.suites.scale:memory",
    "unlock": "bench.suites.maintenance:unlock",
    "verify": "bench.suites.maintenance:verify",
    "transfer": "bench.suites.maintenance:transfer",
    "concurrency": "bench.suites.robustness:concurrency",
    "durability": "bench.suites.robustness:durability",
    "external": "bench.suites.external:comparators",
    "resolve": "bench.suites.resolve:resolve",
    "cli": "bench.suites.rust:cli",
    "interop": "bench.suites.rust:interop",
}

# What CI runs, and in what order. `core` is the set that fits a runner
# comfortably; `deep` adds the ones that want minutes and disk; `external`
# needs gocryptfs/restic installed and is opt-in everywhere.
GROUPS: dict[str, tuple[str, ...]] = {
    "core": ("throughput", "smallfile", "random", "append", "space", "dedup"),
    "deep": (
        "versions",
        "prune",
        "ingest_scale",
        "dir_scale",
        "resolve",
        "memory",
        "unlock",
        "verify",
        "transfer",
        "concurrency",
        "durability",
        "cli",
        "interop",
    ),
    "external": ("external",),
}
GROUPS["all"] = GROUPS["core"] + GROUPS["deep"] + GROUPS["external"]


def load(name: str) -> Callable:
    import importlib

    module_name, _, attr = SUITES[name].partition(":")
    return getattr(importlib.import_module(module_name), attr)


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
