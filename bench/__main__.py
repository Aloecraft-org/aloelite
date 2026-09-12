# ./bench/__main__.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
Aloelite benchmarks: `python -m bench`.

    python -m bench                          # core suites, ci scale
    python -m bench --scale full             # the numbers for the doc
    python -m bench --suite throughput       # one suite
    python -m bench --group all --scale ci   # everything a runner can do
    python -m bench --json out/results.json --markdown out/BENCHMARKS.md

Nothing here asserts and nothing returns a failing status for a slow number:
throughput is a property of the host, so a benchmark that failed a build
would only ever be reporting on the runner. A suite that CRASHES is a real
failure and does exit non-zero, because that is a bug in the benchmark or in
the engine, not a measurement.

Surface
-------
Entry points
  main()        the CLI
  run_suites()  the loop, also importable

Configurable values
  DEFAULT_SCALE / DEFAULT_GROUP / DEFAULT_JSON

Fan-out points
  bench.suites.SUITES is the dispatch table; bench.harness.SCALES is the
  size axis. Neither is duplicated here.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

from .harness import SCALES, Run, describe_host, section
from .suites import GROUPS, SUITES, load

DEFAULT_SCALE = "ci"
DEFAULT_GROUP = "core"
DEFAULT_JSON = "bench-results/results.json"


def run_suites(names, scratch: Path, scale: str, keep_going: bool = True) -> Run:
    host = describe_host(scratch, scale)
    run = Run.begin(scale, host)
    print(f"aloelite benchmarks — scale={scale}")
    for k, v in host.items():
        print(f"  {k:<20} {v}")
    cfg = SCALES[scale]
    failures = []
    for name in names:
        section(name)
        workdir = scratch / name
        workdir.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        try:
            load(name)(run, workdir, cfg)
        except Exception as exc:  # a crashing suite is a bug, not a datapoint
            traceback.print_exc()
            run.skip(name, f"crashed: {exc.__class__.__name__}: {exc}")
            failures.append(name)
            if not keep_going:
                raise
        finally:
            # Printed for every suite: a run that takes twenty minutes needs
            # to say which suite took them.
            print(f"  [{name} took {time.monotonic() - started:.1f}s]", flush=True)
            shutil.rmtree(workdir, ignore_errors=True)
    if failures:
        print(f"\nsuites that crashed: {', '.join(failures)}", file=sys.stderr)
    run.host["failed_suites"] = failures
    return run


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bench", description=__doc__)
    ap.add_argument("--scale", choices=sorted(SCALES), default=DEFAULT_SCALE)
    ap.add_argument(
        "--group", choices=sorted(GROUPS), default=DEFAULT_GROUP, help="a named set"
    )
    ap.add_argument(
        "--suite",
        action="append",
        choices=sorted(SUITES),
        help="run just this suite (repeatable); overrides --group",
    )
    ap.add_argument("--json", default=DEFAULT_JSON, help="where results.json goes")
    ap.add_argument("--markdown", help="also render a markdown report here")
    ap.add_argument(
        "--scratch",
        help="working directory (default: a temp dir). Must be on the disk "
        "whose numbers you want — a tmpfs scratch measures memory.",
    )
    ap.add_argument(
        "--strict",
        action="store_true",
        help="stop at the first suite that crashes instead of carrying on",
    )
    args = ap.parse_args(argv)

    names = args.suite or list(GROUPS[args.group])
    owned = args.scratch is None
    scratch = Path(args.scratch or tempfile.mkdtemp(prefix="aloebench-"))
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        run = run_suites(names, scratch, args.scale, keep_going=not args.strict)
    finally:
        if owned:
            shutil.rmtree(scratch, ignore_errors=True)

    out = Path(args.json)
    run.write(out)
    print(f"\nwrote {out} ({len(run.rows)} rows)")
    if args.markdown:
        from .report import render

        md = Path(args.markdown)
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text(render(run))
        print(f"wrote {md}")
    return 1 if run.host.get("failed_suites") else 0


if __name__ == "__main__":
    raise SystemExit(main())
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
