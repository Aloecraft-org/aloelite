# ./aloelite/ids.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
Host-side id minting (doc/DECISIONS.md D-1/D-2).

Ids are uuid7 strings in the exact layout the retired SQL triggers produced:

    TTTTTTTT-TTTT-7SSS-Vrrr-rrrrrrrrrrrr

where T = 48-bit unix epoch milliseconds as 12 lowercase hex digits,
S = 12-bit sequence in rand_a, V = variant nibble drawn from '89ab', and
r = 15 random hex digits. Every implementation (Python, Rust, Kotlin) mints
this same layout; conformance/vectors pins it.

Two mints:

  * `stateless_uuid7()` — volume / mount / lock ids (no ordering promise);
    the sequence nibbles are random, exactly like the old SQL stateless path.
  * `MonotonicMint` — node / edge ids. Strictly increasing (timestamp, seq)
    per mint instance; on 12-bit sequence overflow within one millisecond the
    timestamp borrows 1 ms forward (matching the trigger's behavior — spin is
    pointless when the borrow is invisible sub-ms skew).

Ordering contract (D-2): strict within a mint; strict across mints separated
by more than the coordination window; arbitrary within the window. The fence
against clock regression is the volume high-water mark: `MonotonicMint.fence`
is called at attach with the volume's stored (wm_ts, wm_seq) so a mint can
never produce an id at or below anything the volume has already recorded.
"""

from __future__ import annotations

import secrets
import time

# 12-bit sequence space in rand_a (4096 ids per millisecond per mint).
_SEQ_LIMIT = 4096
_VARIANT = "89ab"


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def format_uuid7(ts_ms: int, seq: int) -> str:
    """The shared layout. Random tail minted here; callers own (ts, seq)."""
    if not 0 <= seq < _SEQ_LIMIT:
        raise ValueError(f"seq {seq} outside 12-bit space")
    t = format(ts_ms & 0xFFFFFFFFFFFF, "012x")
    variant = _VARIANT[secrets.randbelow(4)]
    return (
        f"{t[0:8]}-{t[8:12]}-7{seq:03x}-"
        f"{variant}{secrets.token_hex(2)[1:4]}-{secrets.token_hex(6)}"
    )


def stateless_uuid7(now_ms: int | None = None) -> str:
    """A fresh non-monotonic uuid7 (volume/mount/lock ids)."""
    return format_uuid7(
        _now_ms() if now_ms is None else now_ms, secrets.randbelow(_SEQ_LIMIT)
    )


class MonotonicMint:
    """Strictly-increasing (ts, seq) state for one volume within one mount
    session. In-memory only — a restarted session re-fences from the volume
    row (D-2); nothing here is ever persisted per mount."""

    __slots__ = ("ts", "seq")

    def __init__(self) -> None:
        self.ts = 0
        self.seq = -1  # first mint at a fenced ts uses seq 0

    def fence(self, wm_ts: int, wm_seq: int) -> None:
        """Raise the floor to the volume's high-water mark. Idempotent and
        monotonic: fencing below the current state is a no-op, so re-fencing
        (e.g. after a reconnect) can never move the mint backwards."""
        if (wm_ts, wm_seq) > (self.ts, self.seq):
            self.ts, self.seq = wm_ts, wm_seq

    def mint(self, now_ms: int | None = None) -> str:
        """The next id, strictly above every id this mint has produced and
        above the fence. Clock regression is absorbed: a `now` at or below
        the current state advances the sequence (borrowing 1 ms forward on
        overflow) instead of ever reusing or reversing (ts, seq)."""
        now = _now_ms() if now_ms is None else now_ms
        if now > self.ts:
            self.ts, self.seq = now, 0
        elif self.seq + 1 < _SEQ_LIMIT:
            self.seq += 1
        else:
            self.ts, self.seq = self.ts + 1, 0
        return format_uuid7(self.ts, self.seq)


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
