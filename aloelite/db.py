# ./aloelite/db.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
Connection + template scaffolding, and the transaction boundary.

This is the substrate the entire flat function layer is written in terms of.
Two responsibilities:

  1. Own ONE sqlite3 connection per Fs handle (the connection-owning model;
     ACC-1 "access is never ambient"). No pool — a reference oracle wants the
     single-writer reality to be simply true, not worked around.

  2. Execute the named SQL templates from sql-templates.yaml with named binds,
     and provide the two primitives the templates can't express alone:
       * create_monotonic     — host-mints a monotonic id (D-1/D-2) and passes
         it into the create template as :id; tracks the volume high-water
         mark for flush at commit.
       * txn                  — the transaction context manager that makes the
         interface's `atomic` annotations real (autocommit off; commit on
         success, rollback on any exception).

Templates are loaded once and addressed as "group.name" (e.g.
"resolution.resolve_segment").
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from pathlib import Path as _FsPath
from typing import Any, Iterator, Mapping

import yaml

from ._sqlite import sqlite3
from .crypto import Cipher, IdentityCipher
from .errors import Unsupported
from .ids import MonotonicMint, stateless_uuid7

# Template groups that contain executable `sql` entries (host_only / meta are not).
_SQL_GROUPS = ("resolution", "mutation", "validation", "recursive", "maintenance")

# The newest sqlite feature the schema/templates rely on is jsonb() (3.45);
# unixepoch('subsec') (3.42) has the nastier failure mode -- an unknown
# modifier returns NULL instead of raising, which flows through printf as
# zeros and through coalesce into NOT NULL violations. Probe BOTH capabilities
# at open, not the version string: version parsing lies (vendor backports),
# capabilities don't.
MIN_SQLITE = (3, 45)

# Schema era stamped into PRAGMA user_version. Files stamped with an OLDER era
# (or 0, the pre-era default) get their derived objects -- views and triggers,
# which hold no data -- dropped and re-created from the current schema on open,
# so their definitions belong to the installed aloelite, not to whatever
# version happened to create the file (the `IF NOT EXISTS` fossilization that
# shipped 'subsec' triggers to hosts that couldn't run them). Files stamped
# with a NEWER era are refused with a clear error instead of being half-read.
# Bump this whenever schema.sql changes any view, trigger, or table.
#
# Era 2 (the 0.4 break-once migration): host-minted ids (D-1/D-2), ownership/
# time columns and per-placement names, PI-1 narrowed to containers, NODE-2
# widened, mount policy columns, xattrs, and TIMESTAMPS IN NANOSECONDS.
# Table-shape changes beyond the derived-object refresh live in _MIGRATIONS:
# each entry upgrades a file FROM era-1 (one below its key) and runs before
# the executescript that rebuilds the derived objects. Steps must be
# crash-idempotent -- a failure between migration and stamp reruns them.
SCHEMA_ERA = 2

# ms epochs stay below 1e15 until the year 33658; ns epochs passed 1e18 in
# 2001. Any stored value under this bound is an unmigrated millisecond value,
# which is what makes the x1e6 rewrite safe to rerun after a crash.
_NS_BOUND = 10**15


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM pragma_table_info(?) WHERE name = ?", (table, column)
        ).fetchone()
        is not None
    )


def _migrate_to_era2(conn: sqlite3.Connection) -> None:
    """Era 1 -> 2. Additive columns (guarded per-column so a crashed run can
    rerun), the ms->ns value rewrite (guarded by _NS_BOUND), and the drop of
    the era-1 PI-1 partial unique index (its narrowed replacement is the
    edge_guard_single_parent trigger pair, rebuilt with the derived objects)."""
    for table, column, decl in [
        ("node", "uid", "INTEGER"),
        ("node", "gid", "INTEGER"),
        ("node", "mode", "INTEGER"),
        ("node", "atime", "INTEGER"),
        ("node", "ctime", "INTEGER"),
        ("edge", "name", "TEXT"),
        ("mount", "access", "TEXT NOT NULL DEFAULT 'rw'"),
        ("mount", "principal", "TEXT"),
    ]:
        if not _column_exists(conn, table, column):
            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {decl}')
    for table, column in [
        ("node", "created_at"),
        ("node", "modified_at"),
        ("volume", "created_at"),
        ("mount", "created_at"),
        ("mount", "expires_at"),
        ("lock", "created_at"),
        ("lock", "expires_at"),
    ]:
        conn.execute(
            f'UPDATE "{table}" SET "{column}" = "{column}" * 1000000 '
            f'WHERE "{column}" IS NOT NULL AND "{column}" < {_NS_BOUND} '
            f'AND "{column}" > 0'
        )
    conn.execute("DROP INDEX IF EXISTS edge_active_placement")


_MIGRATIONS = {2: _migrate_to_era2}


def _check_sqlite_capabilities(conn: sqlite3.Connection) -> None:
    """Refuse a too-old sqlite AT OPEN with an actionable message, instead of
    letting it surface later as a NOT NULL IntegrityError, a wrong-answer NULL
    timestamp, or -- through FUSE -- a bare EIO with the traceback invisible."""
    try:
        conn.execute("SELECT jsonb(1)")
        subsec_ok = conn.execute("SELECT unixepoch('subsec') IS NOT NULL").fetchone()[0]
    except sqlite3.OperationalError:
        subsec_ok = False
    if not subsec_ok:
        raise Unsupported(
            f"host sqlite {sqlite3.sqlite_version} is too old for aloelite "
            f"(needs jsonb + unixepoch subsec, sqlite >= "
            f"{'.'.join(map(str, MIN_SQLITE))}). Fixes: pip install "
            f"'aloelite[bundled-sqlite]', or provide libsqlite3 >= "
            f"{'.'.join(map(str, MIN_SQLITE))}."
        )


class Templates:
    """Parsed sql-templates.yaml: name -> SQL string, addressed as 'group.name'."""

    def __init__(self, by_name: dict[str, str], version: int) -> None:
        self._by_name = by_name
        self.version = version

    @classmethod
    def load(cls, path: str | _FsPath) -> "Templates":
        spec = yaml.safe_load(_FsPath(path).read_text())
        by_name: dict[str, str] = {}
        for group in _SQL_GROUPS:
            for name, entry in (spec.get(group) or {}).items():
                by_name[f"{group}.{name}"] = entry["sql"]
        return cls(by_name, version=spec["meta"]["version"])

    def sql(self, name: str) -> str:
        try:
            return self._by_name[name]
        except KeyError:
            raise KeyError(f"no SQL template named {name!r}") from None

    def __contains__(self, name: str) -> bool:
        return name in self._by_name


class Db:
    """Owns one connection and runs templates against it."""

    def __init__(self, conn: sqlite3.Connection, templates: Templates) -> None:
        self._conn = conn
        self._t = templates
        # The active at-rest cipher for the mounted session. Identity (no-op) by
        # default, so an unencrypted volume runs the same path and the whole
        # conformance suite is unaffected. mount() installs a ChunkCipher when a
        # PIN unlocks an encrypted volume; unmount() restores the identity.
        self.cipher: Cipher = IdentityCipher()
        # Per-mount session material (runtime-only): the token handed to the
        # user, the mount nonce, and the memory-only sealed mount secret. Never
        # persisted beyond N_m (which is on the mount row). None when no
        # encrypted session is active.
        self.active_session: dict[str, Any] | None = None
        # Per-volume monotonic id mints (D-2): in-memory only, fenced from the
        # volume's high-water mark on first use, flushed back per write txn.
        self._mints: dict[str, MonotonicMint] = {}
        self._pending_wm: dict[str, tuple[int, int]] = {}
        # row access by column name everywhere
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        # Multi-connection model (mount is a row, not a connection): WAL lets
        # readers and the single writer coexist; busy_timeout makes a second
        # writer wait briefly rather than fail instantly. (No-op on :memory:.)
        try:
            self._conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError:
            # WAL needs a shared-memory file (mmap); filesystems without it
            # (notably an aloelite FUSE mount) can still run in a rollback
            # journal mode. PERSIST avoids journal unlink churn.
            self._conn.execute("PRAGMA journal_mode = PERSIST")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        # We manage transactions explicitly via txn(); disable the driver's
        # implicit BEGIN-before-DML so autocommit is the default outside txn().
        self._conn.isolation_level = None

    # -- connection lifecycle ------------------------------------------------
    @classmethod
    def open(
        cls,
        db_ref: str | _FsPath,
        templates_path: str | _FsPath,
        *,
        schema_path: str | _FsPath | None = None,
        check_same_thread: bool = True,
    ) -> "Db":
        # check_same_thread=False is for holders that serialize every call
        # through their own lock (manager direct sessions); the engine itself
        # adds no thread safety.
        conn = sqlite3.connect(str(db_ref), check_same_thread=check_same_thread)
        try:
            _check_sqlite_capabilities(conn)
        except Unsupported:
            conn.close()
            raise
        db = cls(conn, Templates.load(templates_path))
        if schema_path is not None:
            era = conn.execute("PRAGMA user_version").fetchone()[0]
            if era > SCHEMA_ERA:
                conn.close()
                raise Unsupported(
                    f"file was written by a newer aloelite (schema era {era}; "
                    f"this build understands {SCHEMA_ERA}). Upgrade aloelite "
                    f"to open it."
                )
            fresh = (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' LIMIT 1"
                ).fetchone()
                is None
            )
            if era < SCHEMA_ERA:
                # Table-shape migrations first (new columns, value rewrites),
                # oldest era to newest, so the derived objects rebuilt below
                # can reference the new columns. Each step is idempotent; the
                # stamp at the end is what marks the whole upgrade done. A
                # fresh file has no tables to migrate -- executescript below
                # creates them era-current.
                if not fresh:
                    for target in range(max(era + 1, 2), SCHEMA_ERA + 1):
                        step = _MIGRATIONS.get(target)
                        if step is not None:
                            step(conn)
                # Derived objects belong to the installed version, not to the
                # file's creation era: drop every view and trigger so the
                # executescript below re-creates them from the CURRENT schema.
                # They hold no data, so this is always safe; tables keep
                # IF NOT EXISTS and are never dropped here.
                derived = conn.execute(
                    "SELECT type, name FROM sqlite_master "
                    "WHERE type IN ('trigger', 'view')"
                ).fetchall()
                for typ, name in derived:
                    if typ == "trigger":
                        conn.execute(f'DROP TRIGGER IF EXISTS "{name}"')
                for typ, name in derived:
                    if typ == "view":
                        conn.execute(f'DROP VIEW IF EXISTS "{name}"')
            conn.executescript(_FsPath(schema_path).read_text())
            conn.execute(f"PRAGMA user_version = {SCHEMA_ERA}")
            conn.commit()
        return db

    def close(self) -> None:
        if self._pending_wm and not self._conn.in_transaction:
            try:
                self._flush_watermarks()
            except sqlite3.Error:
                pass  # detach fence still bounds the next session (D-2)
        self._conn.close()

    # -- raw template execution ---------------------------------------------
    def run(
        self, template: str, params: Mapping[str, Any] | None = None
    ) -> sqlite3.Cursor:
        """Execute a named template, returning the cursor (for SELECTs)."""
        return self._conn.execute(self._t.sql(template), dict(params or {}))

    def one(
        self, template: str, params: Mapping[str, Any] | None = None
    ) -> sqlite3.Row | None:
        return self.run(template, params).fetchone()

    def all(
        self, template: str, params: Mapping[str, Any] | None = None
    ) -> list[sqlite3.Row]:
        return self.run(template, params).fetchall()

    def scalar(self, template: str, params: Mapping[str, Any] | None = None) -> Any:
        row = self.one(template, params)
        return None if row is None else row[0]

    def rowcount(self, template: str, params: Mapping[str, Any] | None = None) -> int:
        return self.run(template, params).rowcount

    # -- id generation -------------------------------------------------------
    #
    # Host-minted (doc/DECISIONS.md D-1/D-2). The caller always holds the id
    # before the INSERT, so there is no read-back and no single-owning-
    # connection requirement.
    #
    #   * node / edge ids come from a per-volume MonotonicMint, fenced at
    #     first use by the volume's stored (wm_ts, wm_seq) high-water mark so
    #     a new session can never mint at or below anything the volume has
    #     recorded — clock regression and failover skew included.
    #   * volume / mount / lock ids are stateless uuid7s (no ordering promise).
    #
    # The high-water mark is written back inside the same transaction as the
    # creates it covers (one monotonic UPDATE per write txn, not per id), so
    # a rollback loses the advance together with the rows and the stored mark
    # exactly covers committed ids. A shared-backend port may batch the
    # advance more loosely; D-2's contract only requires the attach fence.

    def gen_id(self) -> str:
        """A fresh stateless uuid7 (for volume/mount/lock ids)."""
        return stateless_uuid7()

    def _mint_for(self, volume: str) -> MonotonicMint:
        mint = self._mints.get(volume)
        if mint is None:
            mint = self._mints[volume] = MonotonicMint()
            row = self._conn.execute(
                "SELECT wm_ts, wm_seq FROM volume WHERE volume_id = ?", (volume,)
            ).fetchone()
            if row is not None:
                mint.fence(row["wm_ts"], row["wm_seq"])
        return mint

    def create_monotonic(self, insert_template: str, params: Mapping[str, Any]) -> str:
        """Create a node/edge with a host-minted monotonic id, passed to the
        template as :id. `params` must include 'volume' (which may be None on
        the import/recovery path — stateless mint, no watermark)."""
        volume = params.get("volume")
        if volume is None:
            new_id = stateless_uuid7()
        else:
            mint = self._mint_for(volume)
            new_id = mint.mint()
            self._pending_wm[volume] = (mint.ts, mint.seq)
        self.run(insert_template, {**params, "id": new_id})
        if not self._conn.in_transaction:
            self._flush_watermarks()
        return new_id

    def _flush_watermarks(self) -> None:
        """Advance each touched volume's high-water mark to the mint's state.
        Monotonic guard in the WHERE: concurrent sessions can interleave
        flushes in any order without ever moving a mark backwards."""
        for volume, (ts, seq) in self._pending_wm.items():
            self._conn.execute(
                "UPDATE volume SET wm_ts = ?, wm_seq = ? WHERE volume_id = ? "
                "AND (wm_ts < ? OR (wm_ts = ? AND wm_seq < ?))",
                (ts, seq, volume, ts, ts, seq),
            )
        self._pending_wm.clear()

    # -- transaction boundary -----------------------------------------------
    @contextmanager
    def txn(self) -> Iterator["Db"]:
        """Atomic boundary for an operation. Commit on success, rollback on any
        exception. Nesting is not supported here (operations are flat); a single
        with-block wraps one whole Mount API operation.
        """
        self._conn.execute("BEGIN")
        try:
            yield self
        except BaseException:
            # discard the advance with the rows it covered
            self._pending_wm.clear()
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._flush_watermarks()
            self._conn.execute("COMMIT")

    # -- escape hatch for the few host-only walks that need direct SQL -------
    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    # -- content chunking primitives ----------------------------------------
    #
    # Shared by the function layer (operations.py) and the streaming descriptor
    # so both chunk/reassemble identically. These run within whatever txn the
    # caller has open: the atomic whole-file ops call them inside the op's single
    # transaction; the streaming descriptor calls stage_chunks inside its own
    # independent staging commit and then swaps the pointer in a separate txn
    # (CV-5 — no long-lived write txn for the stream).

    def chunk_size_of(self, volume: str) -> int:
        """The volume's fixed chunk size (CV-1), read from the volume row."""
        return self.scalar("resolution.read_chunk_size", {"volume": volume})

    def alloc_version(self, node: str) -> int:
        """The next per-content version to write (CV-3), allocated under the
        entry's write lock. Held by the caller; this is just the read."""
        return self.scalar("mutation.next_version", {"node": node})

    def stage_chunks(self, node: str, version: int, volume: str, data: bytes) -> int:
        """Split `data`, upsert each chunk into the immutable pool (dedup), and
        record the ordered manifest rows for (node, version). Returns the total
        byte size. Does NOT advance the committed pointer — that is the separate
        swap (update_content). Uniform chunking: even a tiny file is one short
        chunk; an empty payload stages zero chunks.
        """
        size = len(data)
        for index, chunk in enumerate(split_chunks(data, self.chunk_size_of(volume))):
            ct, n_c, tag = self.cipher.encrypt_chunk(chunk)
            # Address over the CIPHERTEXT actually stored, so "same address <=>
            # same stored bytes" holds even across volumes keyed differently.
            # Convergent ct is deterministic within a volume (dedup preserved);
            # random mode and foreign keys produce distinct ct, hence distinct
            # addresses, so no cross-volume aliasing.
            ch = chunk_hash(ct)
            self.run(
                "mutation.upsert_chunk",
                {"hash": ch, "data": ct, "length": len(chunk), "n_c": n_c, "tag": tag},
            )
            self.run(
                "mutation.insert_chunk_ref",
                {"node": node, "version": version, "index": index, "hash": ch},
            )
        return size

    def stage_chunk(self, node: str, version: int, index: int, data: bytes) -> None:
        """Stage ONE chunk + its single ordered manifest ref, in the caller's
        txn. The streaming writer commits each chunk in its own short
        transaction (upsert + ref together, so a committed chunk always has a
        committed reference — no window where a pool row exists unreferenced),
        keeping resident memory and the WAL bounded to ~one chunk regardless of
        file size.
        """
        ct, n_c, tag = self.cipher.encrypt_chunk(data)
        # Address over the ciphertext actually stored (see stage_chunks).
        ch = chunk_hash(ct)
        self.run(
            "mutation.upsert_chunk",
            {"hash": ch, "data": ct, "length": len(data), "n_c": n_c, "tag": tag},
        )
        self.run(
            "mutation.insert_chunk_ref",
            {"node": node, "version": version, "index": index, "hash": ch},
        )

    def read_content_meta(self, node: str) -> tuple[int, int] | None:
        """(committed version, materialized size) for an entry, or None if it
        has no content row. The streaming reader needs the size up front to do
        END-relative seeks and to bound ranged reads."""
        row = self.one("resolution.get_content_meta", {"node": node})
        if row is None:
            return None
        return row["version"], row["size"]

    def read_chunk_range(
        self, node: str, version: int, lo: int, hi: int
    ) -> list[tuple[int, bytes]]:
        """The chunks of `version` whose chunk_index is in [lo, hi], in order.
        The streaming reader fetches only the chunks covering a requested byte
        range instead of reassembling the whole file."""
        rows = self.all(
            "resolution.read_chunks_range",
            {"node": node, "version": version, "lo": lo, "hi": hi},
        )
        return [
            (
                r["chunk_index"],
                self.cipher.decrypt_chunk(r["data"], r["N_c"], r["enc_tag"]),
            )
            for r in rows
        ]

    def read_content_bytes(self, node: str) -> bytes:
        """Reassemble an entry's current bytes from its committed version's
        ordered chunk manifest. Empty (no content row or zero chunks) => b''."""
        meta = self.one("resolution.get_content_meta", {"node": node})
        if meta is None:
            return b""
        rows = self.all(
            "resolution.read_chunks", {"node": node, "version": meta["version"]}
        )
        return b"".join(
            self.cipher.decrypt_chunk(r["data"], r["N_c"], r["enc_tag"]) for r in rows
        )


# ---------------------------------------------------------------------------
# Pure chunking helpers (no connection): content addressing folds the byte
# length into the hash (CV-2) so a short final/small chunk can never collide
# with a full chunk sharing leading bytes. chunk_size is therefore effectively a
# MAX size; uniform chunking stores a small file as one short chunk.
# ---------------------------------------------------------------------------
def chunk_hash(data: bytes) -> str:
    h = hashlib.sha256()
    h.update(len(data).to_bytes(8, "big"))
    h.update(data)
    return h.hexdigest()


def split_chunks(data: bytes, chunk_size: int) -> list[bytes]:
    if not data:
        return []
    return [data[i : i + chunk_size] for i in range(0, len(data), chunk_size)]


__all__ = ["Templates", "Db", "chunk_hash", "split_chunks"]
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
