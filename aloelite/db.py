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
       * create_returning_id  — the inseparable create_* + get_generated_*_id
         pair, run on the same connection so last_insert_rowid() is valid.
       * txn                  — the transaction context manager that makes the
         interface's `atomic` annotations real (autocommit off; commit on
         success, rollback on any exception).

Templates are loaded once and addressed as "group.name" (e.g.
"resolution.resolve_segment").
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from pathlib import Path as _FsPath
from typing import Any, Iterator, Mapping, Sequence

import yaml

from .crypto import Cipher, IdentityCipher

# Template groups that contain executable `sql` entries (host_only / meta are not).
_SQL_GROUPS = ("resolution", "mutation", "validation", "recursive", "maintenance")


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
        # row access by column name everywhere
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        # Multi-connection model (mount is a row, not a connection): WAL lets
        # readers and the single writer coexist; busy_timeout makes a second
        # writer wait briefly rather than fail instantly. (No-op on :memory:.)
        self._conn.execute("PRAGMA journal_mode = WAL")
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
        db = cls(conn, Templates.load(templates_path))
        if schema_path is not None:
            conn.executescript(_FsPath(schema_path).read_text())
        return db

    def close(self) -> None:
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
    # IMPORTANT: last_insert_rowid() is NOT usable to read back a generated id.
    # An INSERT performed inside an INSTEAD OF trigger (our insert-views) does
    # not update the connection's last_insert_rowid() once the view-insert
    # returns. So we use two correct paths:
    #
    #   * node / edge ids are minted monotonically per volume in SQL, so the
    #     row just inserted is the MAX uuid7 in that volume (single-owning-
    #     connection model => unambiguous). create_monotonic() reads it back.
    #   * volume / mount / lock ids are stateless: gen_id() SELECTs a fresh
    #     uuid7 from SQL, the caller passes it INTO the insert explicitly, so
    #     the caller already holds the id — no read-back needed.

    def gen_id(self) -> str:
        """A fresh stateless uuid7 (for volume/mount/lock), minted in SQL."""
        return self.scalar("mutation.new_uuid7")

    def create_monotonic(
        self,
        insert_template: str,
        fetch_template: str,
        params: Mapping[str, Any],
    ) -> str:
        """Create a node/edge (monotonic SQL id) and read the minted id back as
        the max uuid7 in its volume. `params` must include 'volume'.
        """
        self.run(insert_template, params)
        new_id = self.scalar(fetch_template, {"volume": params["volume"]})
        if new_id is None:
            raise RuntimeError(f"{insert_template} produced no row to read back")
        return new_id

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
            self._conn.execute("ROLLBACK")
            raise
        else:
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
