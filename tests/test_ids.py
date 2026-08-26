# ./tests/test_ids.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
Host-minted id tests (doc/DECISIONS.md D-1/D-2).

Three layers:
  * format parity — the Python mint produces byte-identical layout to the
    SQL expression the retired triggers used (checked against sqlite's own
    printf, so drift in either direction fails);
  * MonotonicMint semantics — strict ordering, the 1 ms borrow on sequence
    overflow, clock-regression absorption, and the attach fence;
  * watermark round-trip — the volume high-water mark exactly covers
    committed ids, survives rollback, and fences a fresh session even when
    its clock reads earlier than the stored mark.
"""

from __future__ import annotations

import re
import sqlite3

import pytest

from aloelite.ids import MonotonicMint, format_uuid7, stateless_uuid7

_UUID7 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

# The exact expression the era-1 SQL triggers minted with, parameterized on
# (ts-hex, seq). Running it through sqlite makes sqlite the referee for parity.
_SQL_MINT = """
SELECT lower(printf('%s-%s-7%s-%s-%s',
  substr(:t, 1, 8), substr(:t, 9, 4), printf('%03x', :s),
  substr('89ab', abs(random()) % 4 + 1, 1) || substr(lower(hex(randomblob(2))), 2, 3),
  lower(hex(randomblob(6)))))
"""


def test_format_matches_the_sql_triggers_layout():
    conn = sqlite3.connect(":memory:")
    for ts, seq in [(0, 0), (1723542000123, 7), (2**48 - 1, 4095)]:
        sql_id = conn.execute(
            _SQL_MINT, {"t": format(ts, "012x"), "s": seq}
        ).fetchone()[0]
        py_id = format_uuid7(ts, seq)
        # random tails differ; the deterministic prefix (ts + seq + version
        # nibble) must be identical, as must total shape
        assert py_id[:19] == sql_id[:19]
        assert _UUID7.match(py_id), py_id
        assert _UUID7.match(sql_id), sql_id


def test_stateless_shape_and_uniqueness():
    ids = {stateless_uuid7() for _ in range(1000)}
    assert len(ids) == 1000
    for i in ids:
        assert _UUID7.match(i), i


def test_seq_outside_12_bits_refused():
    with pytest.raises(ValueError):
        format_uuid7(0, 4096)


def test_mint_is_strictly_increasing_bytewise():
    m = MonotonicMint()
    ids = [m.mint() for _ in range(5000)]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


def test_mint_absorbs_clock_regression():
    m = MonotonicMint()
    first = m.mint(now_ms=1_000_000)
    stepped_back = m.mint(now_ms=999_000)  # clock stepped back a second
    assert stepped_back > first


def test_sequence_overflow_borrows_one_ms():
    m = MonotonicMint()
    m.mint(now_ms=5_000)
    for _ in range(4095):  # exhaust the 12-bit space at ts=5000
        m.mint(now_ms=5_000)
    assert (m.ts, m.seq) == (5_000, 4095)
    m.mint(now_ms=5_000)
    assert (m.ts, m.seq) == (5_001, 0)


def test_fence_is_monotonic_and_binding():
    m = MonotonicMint()
    m.fence(10_000, 42)
    assert m.mint(now_ms=3_000) > format_uuid7(10_000, 42)  # slow clock loses
    m.fence(1_000, 0)  # re-fencing below current state is a no-op
    assert (m.ts, m.seq) >= (10_000, 43)


def test_watermark_covers_committed_ids_and_fences_next_session(tmp_path):
    from aloelite.aloelite import Aloelite

    ref = tmp_path / "wm.fs"
    fs = Aloelite(ref)
    vol = fs.create_volume("data", enc_mode="none").id
    m = fs.mount(vol)
    for i in range(10):
        m.create_entry(f"/f{i}", data=b"x")
    db = fs.db
    top_node = db.connection.execute("SELECT max(node_id) FROM node").fetchone()[0]
    top_edge = db.connection.execute("SELECT max(edge_id) FROM edge").fetchone()[0]
    wm = db.connection.execute("SELECT wm_ts, wm_seq FROM volume").fetchone()
    stored_top = format_uuid7(wm["wm_ts"], wm["wm_seq"])
    # the stored mark's (ts, seq) prefix is >= every committed id's prefix
    assert stored_top[:19] >= max(top_node, top_edge)[:19]
    fs.close()

    # a fresh session fences above the mark even with a slow clock: force the
    # fence path by minting with a regressed clock immediately after attach
    fs2 = Aloelite(ref)
    fs2.mount(vol)
    mint = fs2.db._mint_for(vol)
    regressed = mint.mint(now_ms=0)
    assert regressed[:19] > stored_top[:19] or regressed[:19] == format_uuid7(
        wm["wm_ts"], wm["wm_seq"] + 1
    )[:19]
    assert regressed > stored_top
    fs2.close()


def test_rollback_discards_watermark_advance(tmp_path):
    from aloelite.aloelite import Aloelite

    fs = Aloelite(tmp_path / "rb.fs")
    vol = fs.create_volume("data", enc_mode="none").id
    m = fs.mount(vol)
    m.create_entry("/keep", data=b"x")
    db = fs.db
    before = tuple(db.connection.execute("SELECT wm_ts, wm_seq FROM volume").fetchone())
    with pytest.raises(RuntimeError):
        with db.txn():
            db.create_monotonic(
                "mutation.create_node",
                {
                    "type": "entry",
                    "name": "orphan",
                    "created_at": 1,
                    "modified_at": None,
                    "volume": vol,
                    "metadata": None,
                },
            )
            raise RuntimeError("abort")
    after = tuple(db.connection.execute("SELECT wm_ts, wm_seq FROM volume").fetchone())
    assert after == before  # the advance died with the rows it covered
    fs.close()


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
