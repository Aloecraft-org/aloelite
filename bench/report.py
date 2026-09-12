# ./bench/report.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
results.json -> markdown. Rendering only: nothing here measures anything, and
nothing here decides what a number means.

The shape of every table is "one row per (metric, frontend, volume, cache)",
because that tuple is what makes a number attributable. Collapsing any of
those columns to shorten a table would produce the blurred average these
benchmarks exist to avoid.

Surface
-------
Entry points
  render(run)        the whole document
  main()             `python -m bench.report results.json > BENCHMARKS.md`

Configurable values
  SECTIONS       suite -> (heading, blurb), and the order they appear in
  CURVE_SUITES   the suites rendered as a curve pivot instead of a flat table
  DETAIL_COLUMNS which detail keys get their own column, per suite

Fan-out points
  SECTIONS is the order and the set. A suite missing from it still renders,
  under its own name, at the end — a new suite is never silently dropped.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

SECTIONS: dict[str, tuple[str, str]] = {
    "throughput": (
        "Sequential throughput",
        "Whole-file streaming, with ext4 on the same disk as the baseline. "
        "Every row includes the same durability barrier, so the engine's "
        "WAL commit is not being timed against ext4's buffered write.",
    ),
    "smallfile": (
        "Small-file operations",
        "A maildir-shaped tree. The cost here is per-operation, not "
        "per-byte, which is why it is measured apart from throughput.",
    ),
    "random": (
        "Random reads",
        "pread at arbitrary offsets in one large file — the "
        "qemu-image-on-a-mount case. Chunk size dominates: a 4 KiB read "
        "still fetches, and on an encrypted volume decrypts, a whole chunk.",
    ),
    "append": (
        "Append commit latency",
        "An rsyslog-shaped writer: small records, each durable before the "
        "next. The engine rewrites the tail chunk per append.",
    ),
    "space": (
        "Space: on disk over logical",
        "Below 1.0 the content pool is winning. Which corpus a user has "
        "decides the ratio entirely.",
    ),
    "dedup": (
        "Deduplication, within and across volumes",
        "`dedup_across_volumes` is the key-separation check: encrypted "
        "volumes address their chunks over ciphertext that is "
        "domain-separated by volume id, so identical plaintext in two "
        "encrypted volumes MUST NOT alias. 1.0 = stored nothing; "
        "0.0 = stored it all again.",
    ),
    "versions": (
        "Cost of a version",
        "A small edit to a large file carries untouched chunks by "
        "reference, so the floor is the chunk that was touched.",
    ),
    "prune": (
        "Reclaim: prune vs VACUUM",
        "Prune frees pages inside the file; only VACUUM returns bytes to "
        "the filesystem. Reported separately because `ls` only ever shows "
        "the second one.",
    ),
    "ingest_scale": (
        "Ingest rate against chunk-table depth",
        "Small chunks on purpose, so a million pool rows costs gigabytes "
        "rather than terabytes. Read the rows/s column, not the MiB/s.",
    ),
    "dir_scale": (
        "Lookup and readdir against directory size",
        "The curve to read carefully.",
    ),
    "memory": (
        "Peak RSS",
        "Measured in a child process, because ru_maxrss is a high-water "
        "mark that never comes back down. Subtract the baseline row.",
    ),
    "unlock": (
        "Unlock and change_pin",
        "Both should be flat in volume size: neither touches content.",
    ),
    "verify": ("Verify", "Seconds per GiB of content."),
    "transfer": (
        "Export, snapshot, and what they do to the foreground",
        "`foreground_pread` appears three times per volume: a control, then "
        "the same workload while each maintenance op runs.",
    ),
    "concurrency": (
        "Concurrency",
        "Separate processes, each with its own connection — WAL's supported "
        "shape. Read `scaling_efficiency`, not the aggregate.",
    ),
    "durability": (
        "Durability under kill -9",
        "`confirmed_then_lost` counts files whose write was confirmed and "
        "which are then missing or fail deep verify. The target is zero.",
    ),
    "resolve": (
        "One query per path, or one per segment",
        "The local columns are measured; `modelled_at_1ms_rtt_ms` adds a "
        "fixed per-round-trip cost to them and is a model, not a "
        "measurement.",
    ),
    "cli": (
        "The two `aloelite` binaries, verb for verb",
        "One process per operation, driven through the verb contract in "
        "`aloelite/config/cli.yaml` that both implementations parse from the "
        "same table. `startup` is the floor every other row in this table "
        "sits on; `above_floor_ms` is what the engine actually did.",
    ),
    "interop": (
        "Cross-implementation round trip",
        "One implementation writes a volume, the other reads it back, both "
        "directions and both volume modes. `value` is the mismatch count, so "
        "0 is the pass — a throughput figure would mean nothing if the bytes "
        "disagreed.",
    ),
    "external": (
        "Outside comparators",
        "gocryptfs for encrypted FUSE throughput, restic for ingest and "
        "dedup. Neither does aloelite's job; both do one part of it, which "
        "is what makes them useful yardsticks.",
    ),
}

CURVE_SUITES = {"ingest_scale": "pool_rows_after", "dir_scale": "dir_entries"}

DETAIL_COLUMNS: dict[str, tuple[str, ...]] = {
    "throughput": ("min", "max"),
    "smallfile": ("elapsed_s",),
    "random": ("p99_ms", "read_MiB_s"),
    "append": ("p99_ms", "records_per_s"),
    "space": ("logical_bytes", "on_disk_bytes", "pool_rows"),
    "dedup": ("bytes_added", "pool_rows"),
    "versions": ("chunks_touched", "elapsed_ms"),
    "prune": ("versions_pruned", "chunks_pruned", "after_vacuum_bytes"),
    "memory": ("rss_MiB", "stream_MiB"),
    "unlock": ("volume_MiB", "p99_ms"),
    "verify": ("elapsed_s", "chunks_checked"),
    "transfer": ("p99_ms", "p99_vs_control", "MiB_s", "busy_errors"),
    "concurrency": ("scaling_efficiency", "MiB_s_per_worker", "busy"),
    "durability": ("files_confirmed", "missing_after_crash", "p99_ms"),
    "external": ("elapsed_s", "min", "max"),
    "resolve": ("depth", "round_trips", "modelled_at_1ms_rtt_ms"),
    "cli": ("p99_ms", "startup_floor_ms", "above_floor_ms", "MiB_s"),
    "interop": ("bytes_match", "write_MiB_s", "read_MiB_s"),
}


def render(run: Any) -> str:
    data = run if isinstance(run, dict) else _as_dict(run)
    rows = data["rows"]
    out: list[str] = []
    out.append("# Benchmark results\n")
    out.append(_host_table(data))
    out.append(_caveat())

    seen = set()
    for suite in list(SECTIONS) + sorted({r["suite"] for r in rows}):
        if suite in seen:
            continue
        seen.add(suite)
        mine = [r for r in rows if r["suite"] == suite]
        if not mine:
            continue
        heading, blurb = SECTIONS.get(suite, (suite.replace("_", " ").title(), ""))
        out.append(f"\n## {heading}\n")
        if blurb:
            out.append(f"{blurb}\n")
        if suite in CURVE_SUITES:
            out.append(_curve_table(mine, CURVE_SUITES[suite]))
        else:
            out.append(_flat_table(suite, mine))

    if data.get("skipped"):
        out.append("\n## Not measured\n")
        out.append("| what | why |\n|---|---|")
        for what, why in sorted(data["skipped"].items()):
            out.append(f"| `{what}` | {why} |")
        out.append("")
    return "\n".join(out) + "\n"


def _as_dict(run) -> dict:
    from dataclasses import asdict

    return asdict(run)


def _host_table(data: dict) -> str:
    host = data["host"]
    keys = [
        "scale",
        "sqlite_version",
        "sqlite_source",
        "journal_mode",
        "synchronous",
        "chunk_size",
        "page_size",
        "aloelite_version",
        "python",
        "cpu_model",
        "cpu_count",
        "mem_total_gib",
        "scratch_fs",
        "platform",
        "git_commit",
    ]
    lines = ["| setting | value |", "|---|---|"]
    for k in keys:
        if k in host:
            lines.append(f"| {k} | `{host[k]}` |")
    lines.append(f"| started | `{data['started_at']}` |")
    return "\n".join(lines) + "\n"


def _caveat() -> str:
    return (
        "\nNothing here asserts. Throughput is a property of the host, so "
        "these numbers gate nothing; a run on a shared CI runner is a "
        "shape, not a specification. **What survives the noise is the ratio "
        "between rows measured in the same run**, because they share the "
        "machine and the moment.\n\n"
        "`cold` means the kernel page cache was dropped for the backing "
        "files first; `warm` means it was deliberately populated. Where the "
        "engine's own sqlite page cache could also be dropped it was, which "
        "is possible for `direct` and not for `fuse` — the daemon's "
        "connection is in another process. That asymmetry favours the FUSE "
        "rows.\n"
    )


def _flat_table(suite: str, rows: list[dict]) -> str:
    extra = [
        k for k in DETAIL_COLUMNS.get(suite, ()) if any(k in r["detail"] for r in rows)
    ]
    header = ["metric", "frontend", "volume", "cache", "value", "unit", "n", *extra]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for r in rows:
        cells = [
            f"`{r['metric']}`",
            r["frontend"],
            r["volume"],
            r["cache"],
            f"**{_num(r['value'])}**",
            r["unit"],
            str(r["n"]),
            *[_num(r["detail"].get(k)) for k in extra],
        ]
        lines.append("| " + " | ".join(cells) + " |")
    notes = sorted({r["note"] for r in rows if r["note"]})
    body = "\n".join(lines)
    if notes:
        body += "\n\n" + "\n".join(f"- {n}" for n in notes) + "\n"
    return body


def _curve_table(rows: list[dict], x_key: str) -> str:
    """One column per x value, one line per (metric, frontend, volume): a
    curve read across, which is the only way to see a sag."""
    xs = sorted({r["detail"][x_key] for r in rows if x_key in r["detail"]})
    series: dict[tuple, dict] = {}
    unit = {}
    for r in rows:
        if x_key not in r["detail"]:
            continue
        key = (r["metric"], r["frontend"], r["volume"])
        series.setdefault(key, {})[r["detail"][x_key]] = r["value"]
        unit[key] = r["unit"]
    header = ["metric", "frontend", "volume", "unit", *[f"{int(x):,}" for x in xs]]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for key, points in series.items():
        cells = [f"`{key[0]}`", key[1], key[2], unit[key]]
        cells += [_num(points.get(x)) for x in xs]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + f"\n\n_Columns are {x_key.replace('_', ' ')}._\n"


def _num(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, str):
        return v
    if v != v:  # NaN
        return "—"
    if v == 0:
        return "0"
    if abs(v) >= 1e6 or abs(v) < 1e-3:
        return f"{v:.3g}"
    if abs(v) >= 100:
        return f"{v:,.0f}"
    return f"{v:.3g}"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: python -m bench.report results.json [> out.md]", file=sys.stderr)
        return 2
    data = json.loads(Path(argv[0]).read_text())
    out = render(data)
    if len(argv) > 1:
        Path(argv[1]).write_text(out)
    else:
        sys.stdout.write(out)
    return 0


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
