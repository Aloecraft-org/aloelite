# ./tests/test_schema_era.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
Schema-era stamping and derived-object refresh (the anti-fossilization
machinery), plus the host-supplied-created_at template contract.

Context: files used to keep whatever view/trigger text they were created
with, forever (`CREATE ... IF NOT EXISTS` on every derived object), which
shipped `unixepoch('subsec')` trigger bodies to hosts whose sqlite returned
NULL for it. Now PRAGMA user_version carries a schema era: an older-era (or
unstamped) file gets every view and trigger dropped and re-created from the
CURRENT schema.sql on open -- always safe, they hold no data -- while a
newer-era file is refused with a clear error instead of being half-read.
Tables are never touched by the refresh; their vocabulary constraints live
in guard triggers (node_guard_type) precisely so the refresh can evolve
them.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from test_operations import SCHEMA, TEMPLATES

from aloelite import Db, errors
from aloelite import operations as ops
from aloelite.db import SCHEMA_ERA


def _open(path) -> Db:
    return Db.open(path, TEMPLATES, schema_path=SCHEMA)


def _user_version(db: Db) -> int:
    return db.connection.execute("PRAGMA user_version").fetchone()[0]


def _trigger_sql(db: Db, name: str) -> str | None:
    row = db.connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)
    ).fetchone()
    return row[0] if row else None


# --------------------------------------------------------------------------
# Era stamping + refresh
# --------------------------------------------------------------------------
def test_new_file_is_stamped_with_current_era(tmp_path: Path):
    db = _open(tmp_path / "a.fs")
    assert _user_version(db) == SCHEMA_ERA
    db.close()


def test_stale_derived_objects_are_rewritten_on_open(tmp_path: Path):
    """The fossilization regression: a file carrying an older era and an
    outdated trigger body must come back from open() with the CURRENT
    definition and stamp. This is what rescues fielded files whose triggers
    still contain version-dependent SQL."""
    p = tmp_path / "old.fs"
    db = _open(p)
    vol = ops.create_volume(db, "v")
    # Forge an "old release" file: replace a trigger with divergent text and
    # wind the era back, exactly what a pre-era file looks like on disk.
    db.connection.executescript(
        "DROP TRIGGER mount_new_ins;"
        "CREATE TRIGGER mount_new_ins INSTEAD OF INSERT ON mount_new "
        "BEGIN SELECT RAISE(ABORT, 'fossilized trigger body'); END;"
        "PRAGMA user_version = 0;"
    )
    db.close()

    db = _open(p)
    assert _user_version(db) == SCHEMA_ERA
    sql = _trigger_sql(db, "mount_new_ins")
    assert sql is not None and "fossilized" not in sql
    # and the file WORKS: the forged trigger would have aborted this mount
    mid = ops.mount(db, vol.id, "/", ttl_ms=60_000)
    assert ops.stat(db, mid, "/").id == vol.root
    db.close()


def test_same_era_open_leaves_file_alone_and_works(tmp_path: Path):
    p = tmp_path / "cur.fs"
    db = _open(p)
    vol = ops.create_volume(db, "v")
    db.close()
    db = _open(p)  # second open at the same era: plain idempotent apply
    assert _user_version(db) == SCHEMA_ERA
    mid = ops.mount(db, vol.id, "/", ttl_ms=60_000)
    ops.create_entry(db, mid, "/f", b"data")
    assert ops.read_all(db, mid, "/f") == b"data"
    db.close()


def test_newer_era_file_is_refused_clearly(tmp_path: Path):
    p = tmp_path / "future.fs"
    db = _open(p)
    db.connection.execute(f"PRAGMA user_version = {SCHEMA_ERA + 1}")
    db.close()
    with pytest.raises(errors.Unsupported) as ei:
        _open(p)
    assert "newer aloelite" in str(ei.value)


# --------------------------------------------------------------------------
# Node-type vocabulary: guard trigger, not table CHECK
# --------------------------------------------------------------------------
def test_unknown_node_type_rejected_by_guard(tmp_path: Path):
    from aloelite._sqlite import sqlite3

    db = _open(tmp_path / "t.fs")
    with pytest.raises(sqlite3.IntegrityError, match="NODE-2"):
        db.connection.execute(
            "INSERT INTO node (node_id, type, name, created_at) "
            "VALUES ('x', 'gremlin', 'g', 1)"
        )
    db.close()


# --------------------------------------------------------------------------
# created_at is host-supplied through the templates (conformance surface)
# --------------------------------------------------------------------------
def test_create_templates_thread_host_created_at(tmp_path: Path):
    """The template must carry the host's value into the row verbatim -- the
    trigger's coalesce fallback exists but nothing may rely on it. A fixed
    sentinel proves the value came from the bind, not from SQL-side now()."""
    db = _open(tmp_path / "c.fs")
    vol = ops.create_volume(db, "v")
    sentinel = 1_234_567_890_123

    mid = db.gen_id()
    db.run(
        "mutation.create_mount",
        {
            "id": mid,
            "volume": vol.id,
            "mount_point": vol.root,
            "expires_at": None,
            "created_at": sentinel,
            "n_m": b"0123456789abcdef",
        },
    )
    row = db.connection.execute(
        "SELECT created_at FROM mount WHERE mount_id=?", (mid,)
    ).fetchone()
    assert row[0] == sentinel

    lid = db.gen_id()
    db.run(
        "mutation.create_lock",
        {
            "id": lid,
            "mount": mid,
            "node": vol.root,
            "expires_at": None,
            "created_at": sentinel,
        },
    )
    row = db.connection.execute(
        "SELECT created_at FROM lock WHERE lock_id=?", (lid,)
    ).fetchone()
    assert row[0] == sentinel
    db.close()


def test_operations_supply_created_at_now(tmp_path: Path):
    """The ops layer passes real wall-clock ms for volume/mount/lock/node --
    NOT NULL holds even if every SQL-side fallback were deleted."""
    db = _open(tmp_path / "n.fs")
    before = int(time.time() * 1000) - 5
    vol = ops.create_volume(db, "v")
    mid = ops.mount(db, vol.id, "/", ttl_ms=60_000)
    ops.create_entry(db, mid, "/f", b"x")
    after = int(time.time() * 1000) + 5
    for table, col, ident in (
        ("volume", "volume_id", vol.id),
        ("mount", "mount_id", mid),
    ):
        got = db.connection.execute(
            f"SELECT created_at FROM {table} WHERE {col}=?", (ident,)
        ).fetchone()[0]
        assert before <= got <= after, (table, got)
    got = db.connection.execute(
        "SELECT created_at FROM node WHERE name='f'"
    ).fetchone()[0]
    assert before <= got <= after
    db.close()


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
