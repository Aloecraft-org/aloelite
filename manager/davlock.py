# ./manager/davlock.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
manager.davlock — WebDAV class 2 lock state, and the `If:` header.

WHY THIS IS NOT JUST ops.lock()
-------------------------------
The engine's lock is MOUNT-SCOPED and `check_lock_held` excludes the caller's
own mount, which is exactly right for the engine and useless for DAV-vs-DAV on
its own: every DAV client on a volume shares ONE engine mount. An unencrypted
volume serves them all from the primary mount, and an encrypted one hands out a
mount token cached against the PIN (`_PinCache`), not against the client — so
two Windows machines with the same PIN are one mount as far as the engine is
concerned. Relying on engine exclusion alone would produce a lock that appears
to work and silently permits exactly the conflict it was taken to prevent.

So locks live in BOTH places, each doing what only it can:

  * here, in the manager — DAV-vs-DAV exclusion, plus everything the DAV
    protocol needs that the engine has no column for: the lock token, the
    <owner> XML to echo back, Depth 0 vs infinity, and the refreshable timeout.
  * in the engine, via ops.lock — CROSS-FRONTEND exclusion. A FUSE mount and
    the browser UI are genuinely different mount rows, so an engine lock is
    what stops them writing under a DAV lock holder.

The manager side is authoritative for DAV; the engine lock is best-effort
company. If the engine refuses (something else already holds it), the DAV LOCK
fails too — that is a real conflict, not an implementation detail.

NOT PERSISTED, AND THAT IS A CHOICE
-----------------------------------
Locks live in memory and die with the manager process. RFC 4918 4918 §6.2
explicitly permits this ("a server MAY discard locks at any time"), clients
must already handle a lock vanishing, and the alternative is a `lock.owner`
column plus a schema era bump for state whose natural lifetime is minutes. The
timeout does the same job across a restart: a client that comes back finds its
token unknown and takes a new lock.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import dataclass, field

# RFC 4918 10.7: a server SHOULD NOT grant an unbounded timeout. Ten minutes is
# what a client that has genuinely gone away costs everyone else, and every
# real client refreshes far more often than that.
DEFAULT_TIMEOUT = 600
MAX_TIMEOUT = 3600


@dataclass
class DavLock:
    token: str
    volume: str
    path: str
    depth: str  # "0" or "infinity"
    owner_xml: str  # verbatim <owner> children, echoed back in lockdiscovery
    timeout: int
    expires_at: float
    engine_lock: str | None = None

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self.expires_at

    def refresh(self, timeout: int | None = None) -> None:
        if timeout is not None:
            self.timeout = timeout
        self.expires_at = time.monotonic() + self.timeout

    def covers(self, path: str) -> bool:
        """Does this lock govern `path`?

        A Depth: 0 lock governs exactly its own resource. A Depth: infinity
        lock governs the whole subtree, which is the shape Explorer and Finder
        take on a folder, and the reason a flat token check is not enough.
        """
        if self.path == path:
            return True
        if self.depth != "infinity":
            return False
        prefix = self.path if self.path.endswith("/") else self.path + "/"
        return path.startswith(prefix)


class LockRegistry:
    """Every live DAV lock, across volumes. Safe for concurrent requests."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_token: dict[str, DavLock] = {}

    # -- internals ---------------------------------------------------------
    def _reap(self) -> None:
        """Drop expired locks. Lazy rather than swept by a timer: a lock only
        matters when someone asks about it, and a background thread here would
        be state to shut down for no benefit."""
        dead = [t for t, lk in self._by_token.items() if lk.expired]
        for token in dead:
            self._by_token.pop(token, None)

    def _live(self, volume: str) -> list[DavLock]:
        return [lk for lk in self._by_token.values() if lk.volume == volume]

    # -- queries -----------------------------------------------------------
    def find(self, volume: str, path: str) -> DavLock | None:
        """The lock governing `path`, whether taken on it or inherited from a
        Depth: infinity lock above it."""
        with self._lock:
            self._reap()
            for lk in self._live(volume):
                if lk.covers(path):
                    return lk
        return None

    def covered_by_subtree(self, volume: str, path: str) -> list[DavLock]:
        """Every lock at or BELOW `path`.

        The mirror of find(): that one looks upward for a lock governing a
        resource, this looks downward for locks a recursive operation would
        destroy. DELETE and MOVE on a collection are Depth: infinity by
        definition, so a lock three levels down is a lock on something the
        request would remove -- the same transitive-destruction rule ACC-11
        applies inside the engine.
        """
        with self._lock:
            self._reap()
            return [
                lk
                for lk in self._live(volume)
                if lk.path == path or _is_descendant(lk.path, path)
            ]

    def get(self, token: str) -> DavLock | None:
        with self._lock:
            self._reap()
            return self._by_token.get(token)

    def discover(self, volume: str, path: str) -> DavLock | None:
        """What `lockdiscovery` should report for this resource."""
        return self.find(volume, path)

    # -- mutation ----------------------------------------------------------
    def acquire(
        self,
        volume: str,
        path: str,
        *,
        depth: str,
        owner_xml: str,
        timeout: int | None,
    ) -> DavLock:
        """Take a lock, or raise LockConflict.

        Conflicts run in BOTH directions, which is the part worth stating: a
        new lock is refused when something already governs the path (itself, or
        an ancestor locked to infinity) AND when the new lock is Depth:
        infinity and anything is already locked BELOW it. Checking only the
        first direction lets a client lock a whole tree while another client
        holds a file inside it -- two holders believing they have exclusive
        rights to the same bytes.
        """
        seconds = (
            DEFAULT_TIMEOUT if timeout is None else max(1, min(timeout, MAX_TIMEOUT))
        )
        with self._lock:
            self._reap()
            for lk in self._live(volume):
                if lk.covers(path):
                    raise LockConflict(lk.path)
                if depth == "infinity" and _is_descendant(lk.path, path):
                    raise LockConflict(lk.path)
            lock = DavLock(
                token=f"opaquelocktoken:{uuid.uuid4()}",
                volume=volume,
                path=path,
                depth=depth,
                owner_xml=owner_xml,
                timeout=seconds,
                expires_at=time.monotonic() + seconds,
            )
            self._by_token[lock.token] = lock
            return lock

    def release(self, token: str) -> DavLock | None:
        with self._lock:
            self._reap()
            return self._by_token.pop(token, None)

    def release_volume(self, volume: str) -> list[DavLock]:
        """Drop every lock on a volume — it was just locked or unmounted, so
        no token that referenced it can still be meaningful."""
        with self._lock:
            gone = [lk for lk in self._by_token.values() if lk.volume == volume]
            for lk in gone:
                self._by_token.pop(lk.token, None)
            return gone


def _is_descendant(candidate: str, ancestor: str) -> bool:
    prefix = ancestor if ancestor.endswith("/") else ancestor + "/"
    return candidate != ancestor and candidate.startswith(prefix)


class LockConflict(Exception):
    """Something already governs the requested resource."""

    def __init__(self, holder_path: str) -> None:
        super().__init__(f"locked by an existing lock on {holder_path}")
        self.holder_path = holder_path


# ===========================================================================
# The If: header (RFC 4918 10.4)
# ===========================================================================
# Grammar, reduced to what a server has to honour:
#
#   If = 1*No-tag-list | 1*Tagged-list
#   Tagged-list = Resource-Tag 1*List
#   List = "(" 1*Condition ")"
#   Condition = ["Not"] (State-token | "[" entity-tag "]")
#
# Lists are OR'd, conditions within a list are AND'd. Getting that backwards is
# the classic bug and produces "the file is locked and I cannot unlock it",
# because the client's UNLOCK is refused by the very header meant to authorise
# it.
_TAG_RE = re.compile(r"<([^>]*)>\s*(?=\()")
_LIST_RE = re.compile(r"\(([^)]*)\)")
_COND_RE = re.compile(r"(Not\s+)?(?:<([^>]*)>|\[\s*(W/)?\"([^\"]*)\"\s*\])", re.I)


@dataclass
class IfList:
    conditions: list[tuple[bool, str | None, str | None]] = field(default_factory=list)
    # (negated, state_token, entity_tag)


@dataclass
class IfHeader:
    # resource tag (or None for an untagged list) -> the lists that apply
    tagged: list[tuple[str | None, IfList]] = field(default_factory=list)

    def tokens(self) -> set[str]:
        """Every state token mentioned, negated or not.

        Lock authorisation asks a different question from precondition
        evaluation: "did the client demonstrate knowledge of this token",
        not "does this condition pass". A client that submits its token in a
        list that fails for an unrelated ETag reason still HOLDS the lock.
        """
        out: set[str] = set()
        for _, lst in self.tagged:
            for _neg, token, _etag in lst.conditions:
                if token:
                    out.add(token)
        return out

    def evaluate(
        self, state_for: dict[str | None, tuple[set[str], str | None]]
    ) -> bool:
        """True when at least one list passes.

        `state_for` maps a resource tag (None = the request-URI) to the tokens
        held on it and its current ETag.
        """
        if not self.tagged:
            return True
        for tag, lst in self.tagged:
            held, etag = state_for.get(tag, state_for.get(None, (set(), None)))
            if _list_passes(lst, held, etag):
                return True
        return False


def _list_passes(lst: IfList, held: set[str], etag: str | None) -> bool:
    for negated, token, entity in lst.conditions:
        if token is not None:
            matched = token in held
        else:
            matched = entity is not None and etag is not None and _etag_eq(entity, etag)
        if matched == negated:  # AND across the list; Not inverts
            return False
    return True


def _etag_eq(candidate: str, current: str) -> bool:
    return candidate.strip('"') == current.strip().lstrip("W/").strip('"')


def parse_if(raw: str) -> IfHeader:
    """Parse an If: header. A malformed header yields no lists, which
    evaluates as 'no condition' rather than raising -- the header is a
    precondition, and inventing a failure for a parse slip would lock a client
    out of its own resource."""
    header = IfHeader()
    if not raw or not raw.strip():
        return header
    pos = 0
    current_tag: str | None = None
    text = raw.strip()
    while pos < len(text):
        tag_match = _TAG_RE.match(text, pos)
        if tag_match:
            current_tag = tag_match.group(1)
            pos = tag_match.end()
            continue
        list_match = _LIST_RE.match(text, pos)
        if list_match:
            header.tagged.append((current_tag, _parse_list(list_match.group(1))))
            pos = list_match.end()
            continue
        pos += 1
    return header


def _parse_list(body: str) -> IfList:
    out = IfList()
    for match in _COND_RE.finditer(body):
        negated = bool(match.group(1))
        token = match.group(2)
        entity = match.group(4)
        out.conditions.append(
            (negated, token or None, entity if entity is not None else None)
        )
    return out


__all__ = [
    "DEFAULT_TIMEOUT",
    "MAX_TIMEOUT",
    "DavLock",
    "IfHeader",
    "LockConflict",
    "LockRegistry",
    "parse_if",
]
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
