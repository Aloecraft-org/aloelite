# ./bench/suites/rust.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
Implementation against implementation: the same Mount API, the same on-disk
format, the same CLI contract, written twice.

The FUSE comparison needs nothing here -- `rust-fuse` is an ordinary entry in
corpus.FUSE_DAEMONS, so every suite already reports it beside `fuse`. What
this module adds is the two comparisons that do NOT fit that matrix:

  cli        both `aloelite` binaries driven through the shared verb
             contract (aloelite/config/cli.yaml), one process per operation.
             Startup is measured separately because at this granularity it is
             most of the cost, and it is the half that differs by two orders
             of magnitude.
  interop    a volume written by one implementation and read by the other,
             both ways. Timed, but the point is that it is checked: a
             benchmark that quietly read back the wrong bytes would report a
             throughput for nothing.

Surface
-------
Entry points
  cli          per-verb latency, both implementations
  interop      cross-implementation round trip, both directions

Configurable values
  VERBS            the contract verbs exercised, and how each is built
  STARTUP_ROUNDS   samples for the process-startup floor
  VERB_ROUNDS      samples per verb

Fan-out points
  VERBS is the dispatch table: one entry per measured command. IMPLS is the
  implementation axis and has exactly two members, which is the subject.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from ..corpus import BENCH_PIN, block_source, payload, rust_bin
from ..harness import MiB, Row, Run, each, latency_detail, rate, timed

STARTUP_ROUNDS = 20
VERB_ROUNDS = 20
# Entries the `ls` verb lists. Small on purpose: readdir is superlinear in
# directory size (doc/BENCHMARKS.md), and this row is about CLI overhead,
# not about that.
LIST_ENTRIES = 20

# name -> (argv tail builder, needs a mounted volume). Every entry is a verb
# from aloelite/config/cli.yaml, which both implementations parse from the
# same table -- that is what makes these commands identical rather than
# merely similar.
VERBS: dict[str, str] = {
    "put_large": "one `put` of a multi-MiB file: startup amortized, engine dominant",
    "get_large": "one `get` of the same file back out",
    "ls": "one `ls` of a populated directory",
    "stat": "one `stat` of a single entry",
    "mkdir": "one `mkdir -p`",
}


def _impls() -> dict[str, list[str]]:
    """Implementation -> how to invoke its `aloelite`.

    The Python side is invoked as the console script a user would run, not as
    `python -m`, so the interpreter startup being measured is the one they
    actually pay.
    """
    out: dict[str, list[str]] = {}
    py = Path(sys.executable).parent / "aloelite"
    out["py-cli"] = (
        [str(py)] if py.is_file() else [sys.executable, "-m", "aloelite.cli"]
    )
    rust = rust_bin("aloelite")
    if rust is not None:
        out["rust-cli"] = [str(rust)]
    return out


def _run(argv: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, env=env, timeout=600)


def cli(run: Run, scratch: Path, cfg: dict) -> None:
    """Per-verb wall time for both binaries, plus the startup floor.

    One process per operation, which is what a shell script pays. The floor
    row is not a footnote: for `ls`, `stat` and `mkdir` it IS the measurement,
    and subtracting it is the only way to see the engine underneath.
    """
    impls = _impls()
    if "rust-cli" not in impls:
        run.skip(
            "rust/cli", "aloelite (rust) not built: `cargo build --release` in rust/"
        )
    scratch.mkdir(parents=True, exist_ok=True)

    size = min(cfg["seq_mb"], 64) * MiB
    src = scratch / "source.bin"
    with open(src, "wb") as fh:
        for block in block_source(size, "unique", seed=31):
            fh.write(block)

    for impl, launch in impls.items():
        fs_file = scratch / f"cli-{impl}.fs"
        base = [*launch, "-f", str(fs_file)]

        # Startup floor: --version parses nothing and opens no file.
        floor = [
            timed(lambda: _run([*launch, "--version"])) for _ in range(STARTUP_ROUNDS)
        ]
        run.add(
            Row(
                suite="cli",
                metric="startup",
                unit="ms",
                value=latency_detail(floor)["p50_ms"],
                frontend=impl,
                volume="-",
                n=len(floor),
                detail=latency_detail(floor),
                note="`--version`: process spawn plus runtime init, no I/O",
            )
        )

        _run([*base])  # create the file with its default volume
        _run([*base, "volume", "create", "bench"])
        vol = [*base, "-v", "bench"]

        # A directory with something in it, so `ls` has work to do and `stat`
        # has a target. Built through the CLI under test, which is also a
        # first check that this binary can drive its own contract.
        small = scratch / "small.bin"
        small.write_bytes(payload(4096, seed=5))
        _run([*vol, "mkdir", "-p", "/d"])
        for i in range(LIST_ENTRIES):
            _run([*vol, "put", str(small), f"/d/f{i}.bin"])

        commands = {
            "put_large": lambda: _run([*vol, "put", str(src), "/big.bin"]),
            "get_large": lambda: _run(
                [*vol, "get", "/big.bin", str(scratch / "out.bin")]
            ),
            "ls": lambda: _run([*vol, "ls", "/d"]),
            "stat": lambda: _run([*vol, "stat", "/d/f1.bin"]),
            "mkdir": lambda: _run(
                [*vol, "mkdir", "-p", f"/made/{os.urandom(4).hex()}"]
            ),
        }
        for verb in each(f"{impl} verb", list(commands)):
            fn = commands[verb]
            rounds = 3 if verb.endswith("_large") else VERB_ROUNDS
            samples = [timed(fn) for _ in range(rounds)]
            detail = latency_detail(samples)
            detail["startup_floor_ms"] = latency_detail(floor)["p50_ms"]
            detail["above_floor_ms"] = detail["p50_ms"] - detail["startup_floor_ms"]
            if verb.endswith("_large"):
                detail["MiB_s"] = rate(size, min(samples))
            run.add(
                Row(
                    suite="cli",
                    metric=verb,
                    unit="ms",
                    value=detail["p50_ms"],
                    frontend=impl,
                    volume="plain",
                    n=len(samples),
                    detail=detail,
                    note=VERBS[verb],
                )
            )


def interop(run: Run, scratch: Path, cfg: dict) -> None:
    """One implementation writes, the other reads -- both directions, and the
    bytes are compared.

    This is the row that only exists because there are two implementations of
    one format. It is timed like the others, but a mismatch is reported as a
    failure count rather than a slow number: two implementations that disagree
    about the bytes have no performance worth discussing.
    """
    impls = _impls()
    if "rust-cli" not in impls:
        run.skip("rust/interop", "aloelite (rust) not built")
        return
    scratch.mkdir(parents=True, exist_ok=True)
    size = min(cfg["space_corpus_mb"], 32) * MiB

    for writer, reader in (("py-cli", "rust-cli"), ("rust-cli", "py-cli")):
        for mode, pin in (("plain", None), ("convergent", BENCH_PIN)):
            fs_file = scratch / f"interop-{writer}-{mode}.fs"
            src = scratch / "payload.bin"
            with open(src, "wb") as fh:
                for block in block_source(size, "unique", seed=77):
                    fh.write(block)

            wbase = [*impls[writer], "-f", str(fs_file)]
            rbase = [*impls[reader], "-f", str(fs_file)]
            pin_args = ["--pin", pin.decode()] if pin else []

            _run([*wbase, *pin_args])
            _run([*wbase, *pin_args, "volume", "create", "shared"])
            wrote = timed(
                lambda: _run(
                    [*wbase, *pin_args, "-v", "shared", "put", str(src), "/x.bin"]
                )
            )
            out = scratch / f"back-{writer}-{mode}.bin"
            read = timed(
                lambda: _run(
                    [*rbase, *pin_args, "-v", "shared", "get", "/x.bin", str(out)]
                )
            )
            ok = out.is_file() and out.read_bytes() == src.read_bytes()

            run.add(
                Row(
                    suite="interop",
                    metric="cross_impl_roundtrip",
                    unit="count",
                    value=0.0 if ok else 1.0,
                    frontend=f"{writer}->{reader}",
                    volume=mode,
                    n=1,
                    detail={
                        "bytes_match": float(ok),
                        "write_ms": wrote * 1e3,
                        "read_ms": read * 1e3,
                        "write_MiB_s": rate(size, wrote),
                        "read_MiB_s": rate(size, read),
                    },
                    note=(
                        f"{writer} wrote a {mode} volume, {reader} read it back; "
                        "value is the mismatch count, target 0"
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
