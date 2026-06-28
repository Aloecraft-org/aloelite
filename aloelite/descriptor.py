# ./aloelite/descriptor.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
Streaming descriptor.

A Descriptor is runtime state — a cursor plus (for writers) a small pending
buffer and the application-level lock — with no SQL of its own except at its
boundaries. It is a *contract* object, not Python ergonomics: open_read /
open_write return it and the other three implementations must produce an
equivalent.

Bounded-memory streaming (the refinement that replaces whole-file buffering):

  READS are ranged. The reader holds only (committed version, total size, chunk
  size, cursor). read(n) fetches just the chunks covering [pos, pos+n) via
  read_chunks_range and trims the edge chunks — so reading one window of a 15 GB
  file touches a handful of chunks, never the whole file. A version's chunks are
  uniform chunk_size except the final one (true for every write path: write_all,
  streaming, copy carry, pack), so byte_offset // chunk_size is the chunk index.

  WRITES stream forward. The writer keeps a pending buffer under one chunk; each
  time a write pushes it past a chunk boundary, the complete chunk is staged +
  committed in its OWN short transaction (CV-5) and dropped from memory, and
  chunk_index advances. The new version number is allocated lazily on the first
  flush (or at close for sub-chunk files) so a same-mount write_all that lands
  between open and close can't collide on the version. At close() the final
  (short) chunk is staged and the committed-version pointer is swapped in one
  transaction, firing node_touch_content and releasing the lock. A crash
  mid-stream leaves staged chunks at a version ABOVE the committed pointer; the
  prior committed version is intact, and prune_content reclaims the orphans once
  the write lock is no longer valid (the live-lock guard keeps an in-progress
  write safe from a concurrent prune).

  SEQUENTIAL CONTRACT. Streaming-flush is exact for sequential writes (offset 0
  forward — the large-copy / cp pattern, and append). A write that lands inside
  the current pending window (a small reorder, or an overwrite of not-yet-
  flushed bytes) is absorbed in memory. A write that targets an ALREADY-FLUSHED,
  immutable region cannot be cheaply rewritten and raises Unsupported rather
  than silently corrupting — random-access rewrites of huge files are out of
  scope for the oracle. A streaming writer is write-forward only; read() on a
  writer raises Unsupported.

Three independent context managers compose and none knows about the others: the
driver's file-IO handle (physical connection / WAL), the txn (atomicity), and
THIS descriptor (lock lifecycle). The descriptor owns the lock; the connection
owns the bytes; the txn owns atomicity.
"""

from __future__ import annotations

from .errors import LockInvalid, Unsupported
from .types import FdId, LockId, NodeId, VolumeId, Whence


class Descriptor:
    """An open read or write handle to an entry's content.

    Construct via operations.open_read / operations.open_write — not directly.
    """

    def __init__(
        self,
        db: "object",  # aloefs.db.Db (avoid import cycle)
        node: NodeId,
        fd: FdId,
        *,
        writable: bool,
        volume: VolumeId | None = None,
        chunk_size: int = 0,
        lock: LockId | None = None,
        # read mode
        version: int = 0,
        size: int = 0,
        # write mode (append carry-forward of the prior version's full chunks)
        carry_src: int = 0,
        carry_full: int = 0,
        pending: bytes = b"",
        position: int = 0,
    ) -> None:
        self._db = db
        self.node = node
        self.fd = fd
        self.writable = writable
        self._volume = volume
        self._cs = chunk_size
        self._lock = lock
        self._pos = max(0, position)
        self._closed = False

        if writable:
            # The new version is allocated lazily (first flush / close) so a
            # same-mount write between open and close can't take our number.
            self._version: int | None = None
            self._carry_src = carry_src  # prior version to re-reference from
            self._carry_full = carry_full  # count of full leading chunks carried
            self._carry_done = False
            self._chunk_index = carry_full  # next chunk index to assign
            self._flushed = carry_full * chunk_size  # bytes already in committed chunks
            self._pending = bytearray(pending)  # bytes from self._flushed onward
        else:
            self._version = version
            self._size = size

    # -- guards --------------------------------------------------------------
    def _check_open(self) -> None:
        if self._closed:
            raise ValueError("descriptor is closed")

    def _check_writable(self) -> None:
        if not self.writable:
            raise ValueError("descriptor is read-only")

    # -- ranged reads over the committed version -----------------------------
    def read(self, n: int = -1) -> bytes:
        self._check_open()
        if self.writable:
            raise Unsupported("streaming write descriptor is write-forward only")
        start = self._pos
        end = self._size if (n is None or n < 0) else min(self._size, start + n)
        if end <= start:
            return b""
        data = self._fetch_range(start, end)
        self._pos = end
        return data

    def _fetch_range(self, start: int, end: int) -> bytes:
        """Bytes [start, end) of the committed version, pulling only the chunks
        that cover the range. Chunks are uniform chunk_size except the last, so
        the first covering chunk begins exactly at (start // cs) * cs."""
        cs = self._cs
        lo_idx = start // cs
        hi_idx = (end - 1) // cs
        rows = self._db.read_chunk_range(self.node, self._version, lo_idx, hi_idx)
        buf = b"".join(d for _, d in rows)
        base = lo_idx * cs
        return buf[start - base : end - base]

    # -- streaming writes ----------------------------------------------------
    def write(self, data: bytes) -> int:
        self._check_open()
        self._check_writable()
        if not data:
            return 0
        end = self._pos + len(data)
        if self._pos < self._flushed:
            # target overlaps an already-flushed, immutable chunk region
            raise Unsupported(
                "write into an already-flushed region is not supported "
                "(streaming writer is sequential)"
            )
        lo = self._pos - self._flushed
        hi = end - self._flushed
        if lo > len(self._pending):  # sparse gap -> zero-fill
            self._pending.extend(b"\x00" * (lo - len(self._pending)))
        self._pending[lo:hi] = data
        self._pos = end
        while len(self._pending) >= self._cs:
            self._flush_one_chunk()
        return len(data)

    def _flush_one_chunk(self) -> None:
        """Peel one full chunk off the pending buffer and stage+commit it in its
        own short transaction (bounded memory + bounded WAL)."""
        chunk = bytes(self._pending[: self._cs])
        with self._db.txn():
            self._claim_version_locked()
            self._db.stage_chunk(self.node, self._version, self._chunk_index, chunk)
        self._chunk_index += 1
        self._flushed += self._cs
        del self._pending[: self._cs]

    def _claim_version_locked(self) -> None:
        """Allocate the new version on first use and carry the prior version's
        full leading chunks into it (append). Idempotent; runs inside a txn."""
        if self._version is None:
            self._version = self._db.alloc_version(self.node)
        if not self._carry_done:
            if self._carry_full > 0:
                self._db.run(
                    "mutation.copy_chunk_refs_range",
                    {
                        "node": self.node,
                        "dst_version": self._version,
                        "src_version": self._carry_src,
                        "lo": 0,
                        "hi": self._carry_full - 1,
                    },
                )
            self._carry_done = True

    # -- cursor --------------------------------------------------------------
    def seek(self, offset: int, whence: Whence = Whence.SET) -> int:
        self._check_open()
        total = self._size if not self.writable else self._flushed + len(self._pending)
        if whence is Whence.SET:
            new = offset
        elif whence is Whence.CUR:
            new = self._pos + offset
        elif whence is Whence.END:
            new = total + offset
        else:  # pragma: no cover - closed enum
            raise ValueError(f"bad whence {whence!r}")
        if new < 0:
            raise ValueError("negative seek position")
        self._pos = new
        return self._pos

    def tell(self) -> int:
        self._check_open()
        return self._pos

    # -- lifecycle -----------------------------------------------------------
    def close(self) -> None:
        """Commit (writers) and release the lock. Idempotent.

        Stage the final (short) chunk and swap the committed-version pointer in
        one transaction, after re-validating the lock. Full chunks were already
        staged + committed during the stream. A lock that went invalid mid-stream
        raises LockInvalid and the pointer is NOT advanced (the already-staged
        chunks become orphans above the committed pointer, reclaimable by
        prune_content once the lock is gone).
        """
        if self._closed:
            return
        try:
            if self.writable:
                size = self._flushed + len(self._pending)
                with self._db.txn():
                    if self._lock is not None:
                        row = self._db.one(
                            "resolution.get_lock_valid", {"lock": self._lock}
                        )
                        if row is None or not row["valid"]:
                            raise LockInvalid(lock=self._lock, node=self.node)
                    self._claim_version_locked()
                    if len(self._pending) > 0:
                        self._db.stage_chunk(
                            self.node,
                            self._version,
                            self._chunk_index,
                            bytes(self._pending),
                        )
                    self._db.rowcount(
                        "mutation.update_content",
                        {
                            "node": self.node,
                            "version": self._version,
                            "size": size,
                            "hash": None,
                        },
                    )
                    if self._lock is not None:
                        self._db.rowcount("mutation.release_lock", {"lock": self._lock})
            else:
                # read descriptor holds no lock; nothing to commit
                pass
        finally:
            self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    # -- the descriptor's own context manager (lock lifecycle) ---------------
    def __enter__(self) -> "Descriptor":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = ["Descriptor"]
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
