# ./bench/suites/maintenance.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
The operations an administrator waits on, and the one question that decides
whether they are usable: does the cost depend on how much is in the volume?

  unlock     Argon2id at the shipped parameters, and change_pin — both of
             which SHOULD be flat in volume size, because neither touches
             content. A row that grows here is a bug, not a benchmark.
  verify     seconds per GB, shallow and deep. Deep re-addresses and decrypts
             every chunk, so it is the one that scales with content.
  transfer   export and snapshot, plus what they do to a foreground workload
             running at the same time — the number that decides whether you
             can snapshot during the day.

Surface
-------
Entry points
  unlock / verify / transfer

Configurable values
  UNLOCK_SIZES_MB    volume sizes the flatness claim is checked at
  UNLOCK_ROUNDS      samples per unlock measurement
  FOREGROUND_OP_BYTES / FOREGROUND_GAP_S    the concurrent workload's shape

Fan-out points
  None: each entry point is a straight loop over corpus.VOLUME_MODES or a
  fixed size list above.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from aloelite.aloelite import Aloelite

from ..corpus import BENCH_PIN, VOLUME_MODES, block_source, engine_session
from ..harness import MiB, Row, Run, each, latency_detail, timed

# Two sizes an order of magnitude apart is enough to catch a cost that scales;
# three would only cost time to say the same thing.
UNLOCK_SIZES_MB = (1, 64)
UNLOCK_ROUNDS = 5
FOREGROUND_OP_BYTES = 64 * 1024
FOREGROUND_GAP_S = 0.002


def unlock(run: Run, scratch: Path, cfg: dict) -> None:
    """Mount latency, and change_pin latency, against volume size.

    Unlock derives K_u from the PIN with Argon2id and unwraps K_v: two
    constant-size operations. change_pin re-derives and re-wraps the same
    32-byte key. Neither reads content, so both should be flat — and the
    Argon2id parameters are the entire cost, which is why they are printed
    alongside.
    """
    from aloelite import crypto

    params = {
        "argon2_time_cost": float(crypto.ARGON2_TIME_COST),
        "argon2_memory_kib": float(crypto.ARGON2_MEMORY_COST),
        "argon2_parallelism": float(crypto.ARGON2_PARALLELISM),
    }
    for size_mb in each("volume MiB ->", UNLOCK_SIZES_MB):
        for mode in ("plain", "convergent"):
            spec = VOLUME_MODES[mode]
            fs_file = scratch / f"unlock-{mode}-{size_mb}.fs"
            with engine_session(fs_file, mode, name="vault") as (_fs, m):
                with m.open_write("/fill.bin") as fh:
                    for block in block_source(size_mb * MiB, "unique", seed=size_mb):
                        fh.write(block)

            fs = Aloelite(fs_file)
            try:
                vol = fs.resolve_volume_name("vault")
                mounts = []

                def _mount():
                    mounts.append(fs.mount(vol, pin=spec["pin"]))

                samples = []
                for _ in range(UNLOCK_ROUNDS):
                    samples.append(timed(_mount))
                    mounts.pop().unmount()
                run.add(
                    Row(
                        suite="unlock",
                        metric="mount_latency",
                        unit="ms",
                        value=latency_detail(samples)["p50_ms"],
                        frontend="direct",
                        volume=mode,
                        n=len(samples),
                        detail={
                            "volume_MiB": float(size_mb),
                            **latency_detail(samples),
                            **(params if mode != "plain" else {}),
                        },
                        note=(
                            "Argon2id + unwrap"
                            if mode != "plain"
                            else "no key ladder — the contrast row"
                        ),
                    )
                )

                if spec["pin"] is None:
                    continue
                new_pin = b"second-benchmark-pin"
                pins = [BENCH_PIN, new_pin]
                samples = []
                for i in range(UNLOCK_ROUNDS):
                    old, new = pins[i % 2], pins[(i + 1) % 2]
                    samples.append(timed(lambda: fs.change_pin(vol, old, new)))
                run.add(
                    Row(
                        suite="unlock",
                        metric="change_pin_latency",
                        unit="ms",
                        value=latency_detail(samples)["p50_ms"],
                        frontend="direct",
                        volume=mode,
                        n=len(samples),
                        detail={
                            "volume_MiB": float(size_mb),
                            **latency_detail(samples),
                        },
                        note="re-derive K_u and re-wrap K_v; content untouched",
                    )
                )
            finally:
                fs.close()


def verify(run: Run, scratch: Path, cfg: dict) -> None:
    """Integrity sweep, per GB.

    Shallow walks the manifests: refs resolve, indexes are contiguous, sizes
    add up. Deep additionally fetches every chunk, recomputes its address
    (bitrot) and decrypts it under the mount cipher (authenticity). The gap
    between the two rows is the price of actually proving the bytes.
    """
    size = cfg["verify_mb"] * MiB
    for mode in ("plain", "convergent"):
        fs_file = scratch / f"verify-{mode}.fs"
        with engine_session(fs_file, mode) as (_fs, m):
            with m.open_write("/payload.bin") as fh:
                for block in block_source(size, "unique", seed=5):
                    fh.write(block)
            for deep in (False, True):
                report = {}

                def _verify():
                    rep = m.verify(deep=deep)
                    report["entries"] = rep.entries_checked
                    report["chunks"] = rep.chunks_checked
                    report["ok"] = rep.ok

                elapsed = timed(_verify)
                gib = size / (1 << 30)
                run.add(
                    Row(
                        suite="verify",
                        metric="verify_deep" if deep else "verify_shallow",
                        unit="s",
                        value=elapsed / gib if gib else 0.0,
                        frontend="direct",
                        volume=mode,
                        n=1,
                        detail={
                            "elapsed_s": elapsed,
                            "content_MiB": float(cfg["verify_mb"]),
                            "entries_checked": float(report["entries"]),
                            "chunks_checked": float(report["chunks"]),
                            "ok": float(bool(report["ok"])),
                        },
                        note="seconds per GiB of content",
                    )
                )


def transfer(run: Run, scratch: Path, cfg: dict) -> None:
    """Export and snapshot, measured twice: alone, and with a foreground
    reader running against the same file.

    The second measurement is the one that matters. An export that takes
    four minutes is fine if the volume stays usable; the same export is
    unusable if it stalls every read behind it. The foreground p99 with and
    without the export running is that answer, and it is why this suite
    reports a latency ratio rather than just a duration.
    """
    from aloelite.transfer import export_volume, snapshot_volume

    size = cfg["verify_mb"] * MiB
    for mode in ("plain", "convergent"):
        spec = VOLUME_MODES[mode]
        fs_file = scratch / f"transfer-{mode}.fs"
        with engine_session(fs_file, mode, name="src") as (_fs, m):
            with m.open_write("/payload.bin") as fh:
                for block in block_source(size, "unique", seed=6):
                    fh.write(block)
            volume = m.info().volume

        # Control: the foreground workload with nothing else running.
        control = _foreground(fs_file, mode, seconds=3.0)
        run.add(
            Row(
                suite="transfer",
                metric="foreground_pread",
                unit="ms",
                value=control["p50_ms"],
                frontend="direct",
                volume=mode,
                cache="warm",
                n=int(control["ops"]),
                detail=control,
                note="control: no maintenance running",
            )
        )

        for op, fn in (
            (
                "export",
                lambda: export_volume(
                    str(fs_file),
                    volume,
                    str(scratch / f"exported-{mode}.fs"),
                    src_pin=spec["pin"],
                    dest_pin=spec["pin"],
                ),
            ),
            (
                "snapshot",
                lambda: snapshot_volume(
                    str(fs_file), volume, f"snap-{mode}", pin=spec["pin"]
                ),
            ),
        ):
            # The worker mounts BEFORE the maintenance op starts and says so.
            # Mounting is itself a write (it inserts a mount row), so a worker
            # that mounted while an export held the write lock would just die
            # of SQLITE_BUSY and measure nothing. That failure is worth
            # reporting, but it belongs to the concurrency suite, not here.
            worker = _Foreground(fs_file, mode)
            worker.start()
            elapsed = timed(fn)
            observed = worker.finish()

            run.add(
                Row(
                    suite="transfer",
                    metric=f"{op}_duration",
                    unit="s",
                    value=elapsed,
                    frontend="direct",
                    volume=mode,
                    n=1,
                    detail={
                        "content_MiB": float(cfg["verify_mb"]),
                        "MiB_s": (size / MiB) / elapsed if elapsed else 0.0,
                    },
                    note=f"{op} of a {cfg['verify_mb']} MiB volume",
                )
            )
            if observed.get("ops"):
                run.add(
                    Row(
                        suite="transfer",
                        metric="foreground_pread",
                        unit="ms",
                        value=observed["p50_ms"],
                        frontend="direct",
                        volume=mode,
                        cache="warm",
                        n=int(observed["ops"]),
                        detail={
                            **observed,
                            "p99_vs_control": (
                                observed["p99_ms"] / control["p99_ms"]
                                if control["p99_ms"]
                                else 0.0
                            ),
                        },
                        note=f"while a {op} runs on the same file",
                    )
                )


class _Foreground:
    """The concurrent reader, run as a thread that owns its own connection.

    Mount happens in start() and start() does not return until it has, so the
    maintenance op it is meant to run alongside cannot be blamed for the
    mount's own contention. Busy errors inside the loop are COUNTED rather
    than raised: an operation refused under contention is a data point, and a
    dead worker thread is not.
    """

    def __init__(self, fs_file: Path, mode: str) -> None:
        self.fs_file = fs_file
        self.mode = mode
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._result: dict = {}
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self, timeout: float = 60.0) -> None:
        self._thread.start()
        self._ready.wait(timeout)

    def finish(self, timeout: float = 60.0) -> dict:
        self._stop.set()
        self._thread.join(timeout)
        return self._result

    def _run(self) -> None:
        try:
            self._result = _foreground(
                self.fs_file, self.mode, None, self._stop, self._ready
            )
        finally:
            self._ready.set()


def _foreground(
    fs_file: Path, mode: str, seconds: float | None, stop=None, ready=None
) -> dict:
    """A small, steady reader on its OWN connection.

    Its own connection because the engine adds no thread safety: one sqlite
    connection serves one caller. Two handles on one file is the supported
    concurrent shape (WAL), and it is also the shape a manager process has.
    """
    import sqlite3

    spec = VOLUME_MODES[mode]
    fs = Aloelite(fs_file)
    samples: list[float] = []
    busy = 0
    try:
        vol = fs.resolve_volume_name("src")
        # ro: export and snapshot hold rw mounts of their own while they run,
        # and era 2 refuses an overlapping rw. A reader declaring `ro` is both
        # accurate and the only way this measures anything.
        with fs.mount(vol, pin=spec["pin"], access="ro") as m:
            size = m.stat("/payload.bin").size or 0
            if ready is not None:
                ready.set()
            import random

            rng = random.Random(4242)
            deadline = time.monotonic() + seconds if seconds is not None else None
            while True:
                if stop is not None and stop.is_set():
                    break
                if deadline is not None and time.monotonic() > deadline:
                    break
                off = rng.randrange(0, max(1, size - FOREGROUND_OP_BYTES))

                def _read():
                    with m.open_read("/payload.bin") as fh:
                        fh.seek(off)
                        fh.read(FOREGROUND_OP_BYTES)

                try:
                    samples.append(timed(_read))
                except sqlite3.OperationalError:
                    busy += 1
                time.sleep(FOREGROUND_GAP_S)
    finally:
        fs.close()
    out = latency_detail(samples)
    out["ops"] = float(len(samples))
    out["busy_errors"] = float(busy)
    return out


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
