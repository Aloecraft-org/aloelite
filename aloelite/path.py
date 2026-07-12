# ./aloelite/path.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
AloelitePath — a pathlib-style ergonomic surface over a Mount.

Pure sugar: every method forwards to the bound Mount (and through it to the
flat operations layer), adding nothing to the contract. An AloelitePath is an
immutable (mount, normalized-path) pair; the `/` operator builds new ones:

    with fs.mount(vol.id) as m:
        note = m / "docs" / "note.txt"
        note.write_text("hello")
        for txt in m.path("/").rglob("*.txt"): ...

Semantics follow pathlib where they map cleanly (name/parent/parts/suffix,
mkdir(parents, exist_ok), glob with '*' and '**', open('rb'|'wb'|'ab')) and
follow the Mount API where they don't:
  * rename(target) is a full move to a mount-relative target path and returns
    the new AloelitePath (pathlib.Path.rename semantics, Mount.move underneath);
  * descriptors are binary-only; use read_text/write_text for str;
  * metadata is a first-class property (NODE-6).
"""

from __future__ import annotations

from fnmatch import fnmatch
from typing import TYPE_CHECKING, Iterator

from .errors import NotAContainer, NotFound
from .types import NodeType, WriteMode

if TYPE_CHECKING:  # avoid a runtime import cycle with aloelite.py
    from .aloelite import Mount
    from .descriptor import Descriptor
    from .models import NodeInfo


class AloelitePath:
    """An immutable mount-relative path. Construct via Mount.path() or `/`."""

    __slots__ = ("_m", "_p")

    def __init__(self, mount: "Mount", path: str = "/") -> None:
        self._m = mount
        self._p = self._norm(path)

    @staticmethod
    def _norm(path: str) -> str:
        segs = [s for s in str(path).split("/") if s]
        return "/" + "/".join(segs)

    # -- path algebra ---------------------------------------------------------
    def __truediv__(self, other) -> "AloelitePath":
        return AloelitePath(self._m, f"{self._p}/{other}")

    def __str__(self) -> str:
        return self._p

    def __repr__(self) -> str:
        return f"AloelitePath({self._p!r})"

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, AloelitePath)
            and other._p == self._p
            and other._m is self._m
        )

    def __hash__(self) -> int:
        return hash((id(self._m), self._p))

    @property
    def name(self) -> str:
        return "" if self._p == "/" else self._p.rsplit("/", 1)[1]

    @property
    def parent(self) -> "AloelitePath":
        if self._p == "/":
            return self
        head = self._p.rsplit("/", 1)[0]
        return AloelitePath(self._m, head or "/")

    @property
    def parts(self) -> tuple[str, ...]:
        return tuple(s for s in self._p.split("/") if s)

    @property
    def suffix(self) -> str:
        i = self.name.rfind(".")
        return self.name[i:] if i > 0 else ""

    @property
    def stem(self) -> str:
        s = self.suffix
        return self.name[: -len(s)] if s else self.name

    # -- inspection -------------------------------------------------------------
    def exists(self) -> bool:
        return self._m.exists(self._p)

    def stat(self) -> "NodeInfo":
        return self._m.stat(self._p)

    def _type(self) -> NodeType | None:
        try:
            return self.stat().type
        except (NotFound, NotAContainer):
            return None

    def is_dir(self) -> bool:
        return self._type() is NodeType.CONTAINER

    def is_file(self) -> bool:
        return self._type() is NodeType.ENTRY

    @property
    def metadata(self) -> dict[str, str]:
        """The node's shallow {str:str} annotation map (NODE-6)."""
        return self.stat().metadata

    def set_metadata(self, metadata: dict[str, str]) -> None:
        self._m.set_metadata(self._p, metadata)

    # -- directory listing --------------------------------------------------------
    def iterdir(self) -> Iterator["AloelitePath"]:
        for e in self._m.list(self._p):
            if e.visible:
                yield self / e.name

    def glob(self, pattern: str) -> Iterator["AloelitePath"]:
        """Match children by pattern. '*' matches within a segment (fnmatch);
        a '**' segment matches zero or more directories."""
        segs = [s for s in pattern.split("/") if s]
        if segs:
            yield from _walk(self, segs)

    def rglob(self, pattern: str) -> Iterator["AloelitePath"]:
        yield from self.glob("**/" + pattern)

    # -- content ----------------------------------------------------------------
    def read_bytes(self) -> bytes:
        return self._m.read_all(self._p)

    def read_text(self, encoding: str = "utf-8") -> str:
        return self.read_bytes().decode(encoding)

    def write_bytes(self, data: bytes) -> None:
        """Create-or-replace the entry's content atomically."""
        if self._m.exists(self._p):
            self._m.write_all(self._p, data)
        else:
            self._m.create_entry(self._p, data)

    def write_text(self, text: str, encoding: str = "utf-8") -> None:
        self.write_bytes(text.encode(encoding))

    def append_bytes(self, data: bytes) -> int:
        """Atomic bounded-memory append; returns the new size."""
        return self._m.append(self._p, data)

    def open(self, mode: str = "rb") -> "Descriptor":
        """A streaming descriptor (binary only). 'rb' = ranged reads;
        'wb' = truncate-write; 'ab' = append (created if missing)."""
        if mode in ("r", "rb"):
            return self._m.open_read(self._p)
        if mode in ("w", "wb"):
            return self._m.open_write(self._p, WriteMode.TRUNCATE)
        if mode in ("a", "ab"):
            if not self._m.exists(self._p):
                self._m.create_entry(self._p)
            return self._m.open_write(self._p, WriteMode.APPEND)
        raise ValueError(f"unsupported mode {mode!r} (use 'rb', 'wb', or 'ab')")

    # -- structure ----------------------------------------------------------------
    def mkdir(self, parents: bool = False, exist_ok: bool = False) -> "AloelitePath":
        if self.exists():
            if exist_ok and self.is_dir():
                return self
            raise FileExistsError(self._p)
        if parents and self._p != "/":
            par = self.parent
            if str(par) != "/" and not par.exists():
                par.mkdir(parents=True, exist_ok=True)
        self._m.create_container(self._p)
        return self

    def rename(self, target) -> "AloelitePath":
        """Move/rename to a mount-relative `target` path; returns the new
        AloelitePath (pathlib.Path.rename semantics)."""
        dst = AloelitePath(self._m, str(target))
        self._m.move(self._p, str(dst))
        return dst

    def copy(self, target) -> "AloelitePath":
        dst = AloelitePath(self._m, str(target))
        self._m.copy(self._p, str(dst))
        return dst

    def unlink(self) -> None:
        self._m.remove(self._p)

    def rmdir(self) -> None:
        """Remove an empty container (NotEmpty otherwise)."""
        self._m.remove(self._p)

    def rmtree(self) -> None:
        self._m.remove_recursive(self._p)


def _walk(base: AloelitePath, segs: list[str]) -> Iterator[AloelitePath]:
    head, rest = segs[0], segs[1:]
    if head == "**":
        if rest:
            yield from _walk(base, rest)  # zero directories consumed
        else:
            yield base
        for child in base.iterdir():
            if child.is_dir():
                yield from _walk(child, segs)
        return
    for child in base.iterdir():
        if not fnmatch(child.name, head):
            continue
        if not rest:
            yield child
        elif child.is_dir():
            yield from _walk(child, rest)


__all__ = ["AloelitePath"]
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
