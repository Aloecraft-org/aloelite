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
    # Forge an "old release" file: replace a guard trigger with divergent
    # text, leave behind a stray era-1 derived object, and wind the era back
    # -- exactly what a fielded pre-era file looks like on disk.
    db.connection.executescript(
        "DROP TRIGGER node_guard_type;"
        "CREATE TRIGGER node_guard_type BEFORE INSERT ON node "
        "BEGIN SELECT RAISE(ABORT, 'fossilized trigger body'); END;"
        "CREATE VIEW node_new AS SELECT * FROM node WHERE 0;"
        "PRAGMA user_version = 1;"
    )
    db.close()

    db = _open(p)
    assert _user_version(db) == SCHEMA_ERA
    sql = _trigger_sql(db, "node_guard_type")
    assert sql is not None and "fossilized" not in sql
    # retired era-1 derived objects are swept, not fossilized
    assert (
        db.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'node_new'"
        ).fetchone()
        is None
    )
    # and the file WORKS: the forged trigger would have aborted node creation
    mid = ops.mount(db, vol.id, "/", ttl_ms=60_000)
    ops.create_entry(db, mid, "/works", b"x")
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


# A faithful era-1 miniature: the era-1 table shapes (no ownership columns,
# no edge.name, no mount policy columns), the era-1 PI-1 partial unique
# index, and MILLISECOND timestamps. What the era-2 migration must eat.
_ERA1_DDL = """
CREATE TABLE node (
  node_id TEXT PRIMARY KEY, type TEXT NOT NULL, name TEXT NOT NULL,
  created_at INTEGER NOT NULL, modified_at INTEGER,
  volume_id TEXT REFERENCES volume (volume_id), metadata BLOB) STRICT;
CREATE TABLE content (
  node_id TEXT PRIMARY KEY REFERENCES node (node_id) ON DELETE CASCADE,
  version INTEGER NOT NULL DEFAULT 0, size INTEGER NOT NULL DEFAULT 0,
  content_hash BLOB, retention_keep INTEGER) STRICT;
CREATE TABLE content_chunk (
  chunk_hash TEXT PRIMARY KEY, data BLOB NOT NULL, length INTEGER NOT NULL,
  N_c BLOB NOT NULL, enc_tag BLOB NOT NULL) STRICT;
CREATE TABLE content_version (
  content_id TEXT NOT NULL REFERENCES node (node_id) ON DELETE CASCADE,
  version INTEGER NOT NULL, chunk_index INTEGER NOT NULL,
  chunk_hash TEXT NOT NULL REFERENCES content_chunk (chunk_hash), proof BLOB,
  PRIMARY KEY (content_id, version, chunk_index)) STRICT;
CREATE TABLE volume (
  volume_id TEXT PRIMARY KEY, root_node_id TEXT UNIQUE REFERENCES node (node_id),
  name TEXT, created_at INTEGER NOT NULL,
  api_version INTEGER NOT NULL DEFAULT 1,
  chunk_size INTEGER NOT NULL DEFAULT 1048576,
  wm_ts INTEGER NOT NULL DEFAULT 0, wm_seq INTEGER NOT NULL DEFAULT 0,
  enc_mode TEXT NOT NULL DEFAULT 'none'
    CHECK (enc_mode IN ('none', 'convergent', 'random')),
  wrapped_key BLOB, wrap_nonce BLOB) STRICT;
CREATE TABLE edge (
  edge_id TEXT PRIMARY KEY,
  from_id TEXT NOT NULL REFERENCES node (node_id),
  to_id TEXT NOT NULL REFERENCES node (node_id),
  volume_id TEXT NOT NULL REFERENCES volume (volume_id),
  archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1))) STRICT;
CREATE TABLE mount (
  mount_id TEXT PRIMARY KEY,
  volume_id TEXT NOT NULL REFERENCES volume (volume_id),
  mount_point TEXT NOT NULL REFERENCES node (node_id),
  state TEXT NOT NULL DEFAULT 'new'
    CHECK (state IN ('new', 'active', 'unmounted')),
  expires_at INTEGER, created_at INTEGER NOT NULL, N_m BLOB NOT NULL) STRICT;
CREATE TABLE lock (
  lock_id TEXT PRIMARY KEY,
  mount_id TEXT NOT NULL REFERENCES mount (mount_id) ON DELETE CASCADE,
  node_id TEXT NOT NULL REFERENCES node (node_id),
  read_count INTEGER NOT NULL DEFAULT 0, write_count INTEGER NOT NULL DEFAULT 0,
  expires_at INTEGER, created_at INTEGER NOT NULL) STRICT;
CREATE UNIQUE INDEX edge_active_placement
  ON edge (volume_id, to_id) WHERE archived = 0;
INSERT INTO volume (volume_id, name, created_at)
  VALUES ('0198a000-0000-7000-8000-000000000001', 'v', 1723500000000);
INSERT INTO node (node_id, type, name, created_at, modified_at, volume_id)
  VALUES ('0198a000-0000-7000-8000-000000000002', 'container', '/',
          1723500000000, NULL, '0198a000-0000-7000-8000-000000000001');
INSERT INTO node (node_id, type, name, created_at, modified_at, volume_id)
  VALUES ('0198a000-0000-7001-8000-000000000003', 'entry', 'f',
          1723500000123, 1723500000456, '0198a000-0000-7000-8000-000000000001');
UPDATE volume SET root_node_id = '0198a000-0000-7000-8000-000000000002';
INSERT INTO edge (edge_id, from_id, to_id, volume_id)
  VALUES ('0198a000-0000-7002-8000-000000000004',
          '0198a000-0000-7000-8000-000000000002',
          '0198a000-0000-7001-8000-000000000003',
          '0198a000-0000-7000-8000-000000000001');
INSERT INTO content (node_id, version, size)
  VALUES ('0198a000-0000-7001-8000-000000000003', 0, 0);
UPDATE volume SET wm_ts = 1723500000123, wm_seq = 1;
PRAGMA user_version = 1;
"""


def test_era1_file_migrates_to_era2_on_open(tmp_path: Path):
    """The break-once migration, end to end on a genuine era-1 file: ownership
    and placement columns appear, ms timestamps become ns (x1e6, exactly
    once even after a crash-rerun), the era-1 PI-1 unique index is gone, and
    the file then just works — resolve, create, list, remove."""
    from aloelite._sqlite import sqlite3 as sq

    p = tmp_path / "era1.fs"
    conn = sq.connect(str(p))
    conn.executescript(_ERA1_DDL)
    conn.commit()
    conn.close()

    db = _open(p)
    assert _user_version(db) == SCHEMA_ERA
    c = db.connection
    # columns arrived
    node_cols = {r[1] for r in c.execute("PRAGMA table_info(node)")}
    assert {"uid", "gid", "mode", "atime", "ctime"} <= node_cols
    assert "name" in {r[1] for r in c.execute("PRAGMA table_info(edge)")}
    mount_cols = {r[1] for r in c.execute("PRAGMA table_info(mount)")}
    assert {"access", "principal"} <= mount_cols
    # ms -> ns exactly once
    created, modified = c.execute(
        "SELECT created_at, modified_at FROM node WHERE name='f'"
    ).fetchone()
    assert created == 1723500000123 * 1_000_000
    assert modified == 1723500000456 * 1_000_000
    assert c.execute("SELECT created_at FROM volume").fetchone()[0] == (
        1723500000000 * 1_000_000
    )
    # the high-water mark is NOT rescaled: wm_ts stays uuid7-milliseconds
    assert tuple(c.execute("SELECT wm_ts, wm_seq FROM volume").fetchone()) == (
        1723500000123,
        1,
    )
    # the era-1 unique index is gone (its replacement is the guard triggers)
    assert (
        c.execute(
            "SELECT 1 FROM sqlite_master WHERE name='edge_active_placement'"
        ).fetchone()
        is None
    )
    # and the migrated file WORKS, with ids fenced above the era-1 watermark
    vol = "0198a000-0000-7000-8000-000000000001"
    mid = ops.mount(db, vol, "/", ttl_ms=60_000)
    assert ops.stat(db, mid, "/f").name == "f"
    nid = ops.create_entry(db, mid, "/g", b"data")
    assert str(nid) > "0198a000-0000-7001-8000-000000000003"
    assert ops.read_all(db, mid, "/g") == b"data"
    db.close()

    # idempotence: a crash between migration and stamp reruns every step --
    # simulate by winding the stamp back on the already-migrated file
    db = _open(p)
    db.connection.execute("PRAGMA user_version = 1")
    db.close()
    db = _open(p)
    created2 = db.connection.execute(
        "SELECT created_at FROM node WHERE name='f'"
    ).fetchone()[0]
    assert created2 == created  # x1e6 did not apply twice
    db.close()


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
            "access": "rw",
            "principal": None,
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
    """The ops layer passes real wall-clock ns for volume/mount/lock/node --
    NOT NULL holds even if every SQL-side fallback were deleted."""
    db = _open(tmp_path / "n.fs")
    before = time.time_ns() - 5_000_000
    vol = ops.create_volume(db, "v")
    mid = ops.mount(db, vol.id, "/", ttl_ms=60_000)
    ops.create_entry(db, mid, "/f", b"x")
    after = time.time_ns() + 5_000_000
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
