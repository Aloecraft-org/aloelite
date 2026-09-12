# ./bench/suites/resolve.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
One query per path, or one per segment.

`resolution.resolve_path` walks a whole path in a single recursive CTE;
`resolution.resolve_segment` is the single-step primitive, and folding it
over a path is what a naive port writes first. Locally the difference is
small and the CTE can even lose at depth 1, which is why the template's own
note says not to fold it. The argument for the CTE is round trips: a fold
pays one per segment, the CTE pays one per path, and over a connection to a
remote backend that is the difference between usable and unusable.

The latency column is a model, not a measurement — a fixed per-round-trip
cost added to a locally measured query count. It is labelled as such on
every row, because a modelled number that reads like a measured one is
worse than no number.

Surface
-------
Entry points
  resolve        CTE against per-segment fold, at increasing depth

Configurable values
  DEPTHS         path depths swept
  LATENCY_MS     the per-round-trip cost the networked column models
  ROUNDS         samples per measurement (best-of, not median)

Fan-out points
  None: one loop over DEPTHS.
"""

from __future__ import annotations

from pathlib import Path

from aloelite.resolve import resolve as cte_resolve
from aloelite.resolve import split_path

from ..corpus import engine_session
from ..harness import Row, Run, each, timed

DEPTHS = (1, 4, 8, 16)
LATENCY_MS = 1.0
ROUNDS = 5


def _fold(db, root: str, path: str) -> str:
    """The per-segment fold, over the template that still backs single-step
    lookups. Resolver against resolver: timing `stat()` instead would put
    mount validation and a `get_node` on one side only."""
    node = root
    for seg in split_path(path):
        row = db.one("resolution.resolve_segment", {"container": node, "name": seg})
        if row is None:
            raise KeyError(seg)
        node = row["node_id"]
    return node


def resolve(run: Run, scratch: Path, cfg: dict) -> None:
    with engine_session(scratch / "resolve.fs", "plain") as (fs, m):
        db = fs.db
        root = m.info().mount_point
        built = ""
        for depth in each("path depth ->", DEPTHS):
            while len(split_path(built)) < depth:
                built += f"/d{len(split_path(built))}"
                m.create_container(built)
            path = built

            # best-of: the least noise-contaminated sample, which is what you
            # want when comparing two implementations of one thing.
            cte = min(
                timed(lambda p=path: cte_resolve(db, root, p)) for _ in range(ROUNDS)
            )
            fold = min(timed(lambda p=path: _fold(db, root, p)) for _ in range(ROUNDS))

            lat = LATENCY_MS / 1000.0
            for label, local, trips in (
                ("resolve_cte", cte, 1),
                ("resolve_fold", fold, depth),
            ):
                run.add(
                    Row(
                        suite="resolve",
                        metric=label,
                        unit="ms",
                        value=local * 1e3,
                        frontend="direct",
                        volume="plain",
                        cache="warm",
                        n=ROUNDS,
                        detail={
                            "depth": float(depth),
                            "round_trips": float(trips),
                            "modelled_at_1ms_rtt_ms": (local + trips * lat) * 1e3,
                        },
                        note=(
                            f"best of {ROUNDS}; the modelled column adds "
                            f"{LATENCY_MS:g} ms per round trip to the local "
                            "time and is NOT measured"
                        ),
                    )
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
