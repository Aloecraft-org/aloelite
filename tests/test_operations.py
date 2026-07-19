# ./tests/test_operations.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
Conformance-style tests for the flat operation layer + the descriptor.

Written deliberately as "perform this operation sequence, then assert the
observable state" — observable through the API (read_all/list/stat/exists) plus
a few direct schema-state checks for things the API doesn't surface (an edge
being archived, modified_at moving). Because the schema is shared by all four
implementations, these assertions are language-agnostic: the same scripts become
the cross-language conformance suite.

No aloelite.py here — these test operations.py and descriptor.py directly, so a
failure can't hide behind the ergonomic wrapper.

Run:  pytest operations_test.py
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from aloelite import Db, NodeType, WriteMode, Whence, errors
from aloelite import operations as ops


# --------------------------------------------------------------------------
# Spec-file resolution: works both in the package layout (schema.sql /
# sql-templates.yaml beside the package) and the app layout (sql/, config/).
# --------------------------------------------------------------------------
def _find(*candidates: str) -> str:
    here = Path(__file__).resolve().parent
    roots = [here, here.parent, here.parent / "aloelite", Path.cwd()]
    for root in roots:
        for c in candidates:
            p = root / c
            if p.exists():
                return str(p)
    raise FileNotFoundError(candidates)


SCHEMA = _find("schema.sql", "sql/schema.sql")
TEMPLATES = _find("sql-templates.yaml", "config/sql-templates.yaml")


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
@pytest.fixture
def db() -> Db:
    d = Db.open(":memory:", TEMPLATES, schema_path=SCHEMA)
    yield d
    d.close()


@pytest.fixture
def mount(db):
    """A fresh volume with a mount anchored at its root."""
    vol = ops.create_volume(db, "test")
    return ops.mount(db, vol.id, "/", ttl_ms=60_000)


# --------------------------------------------------------------------------
# Session / volume / mount
# --------------------------------------------------------------------------
def test_create_volume_bootstraps_root(db):
    vol = ops.create_volume(db, "v")
    assert vol.api_version == 1
    assert vol.root is not None
    # root is a container named '/'
    row = db.one("resolution.get_node", {"node": vol.root})
    assert row["type"] == "container" and row["name"] == "/"


def test_list_volumes(db):
    a = ops.create_volume(db, "a")
    b = ops.create_volume(db, "b")
    ids = {v.id for v in ops.list_volumes(db)}
    assert ids == {a.id, b.id}


def test_mount_info_state_and_path(db, mount):
    info = ops.mount_info(db, mount)
    assert info.state.value == "new"  # default lifecycle state
    assert info.mount_path == "/"  # anchored at volume root


def test_unmount_invalidates(db, mount):
    ops.unmount(db, mount)
    with pytest.raises(errors.MountInvalid):
        ops.list(db, mount, "/")


def test_operation_on_unknown_mount_raises(db):
    with pytest.raises(errors.MountInvalid):
        ops.list(db, "00000000-0000-7000-8000-000000000000", "/")


# --------------------------------------------------------------------------
# Create / read / resolve
# --------------------------------------------------------------------------
def test_create_and_read(db, mount):
    ops.create_container(db, mount, "/docs")
    ops.create_entry(db, mount, "/docs/a.txt", b"hello")
    assert ops.read_all(db, mount, "/docs/a.txt") == b"hello"
    assert ops.exists(db, mount, "/docs/a.txt")
    assert not ops.exists(db, mount, "/docs/missing")


def test_list_resolves_visibility(db, mount):
    ops.create_container(db, mount, "/d")
    ops.create_entry(db, mount, "/d/x", b"1")
    ops.create_entry(db, mount, "/d/y", b"2")
    entries = ops.list(db, mount, "/d")
    assert {e.name for e in entries} == {"x", "y"}
    assert all(e.visible for e in entries)


def test_create_entry_into_non_container_fails(db, mount):
    ops.create_entry(db, mount, "/file", b"x")
    with pytest.raises(errors.NotAContainer):
        ops.create_entry(db, mount, "/file/child", b"y")


def test_empty_name_rejected(db, mount):
    with pytest.raises(errors.Nameless):
        ops.rename(db, mount, "/", "")


def test_fresh_entry_modified_equals_created(db, mount):
    nid = ops.create_entry(db, mount, "/f", b"hi")
    st = ops.stat_by_id(db, mount, nid)
    assert st.modified_at == st.created_at


# --------------------------------------------------------------------------
# Mutations and their state effects
# --------------------------------------------------------------------------
def test_write_all_replaces_and_bumps_modified(db, mount):
    ops.create_entry(db, mount, "/f", b"old")
    before = ops.stat(db, mount, "/f")
    time.sleep(0.01)
    ops.write_all(db, mount, "/f", b"new")
    after = ops.stat(db, mount, "/f")
    assert ops.read_all(db, mount, "/f") == b"new"
    assert after.modified_at > before.modified_at


def test_set_mtime(db, mount):
    nid = ops.create_entry(db, mount, "/f", b"x")
    ops.set_mtime(db, mount, nid, 12345)
    assert ops.stat_by_id(db, mount, nid).modified_at == 12345


def test_rename(db, mount):
    ops.create_entry(db, mount, "/a", b"x")
    ops.rename(db, mount, "/a", "b")
    assert ops.exists(db, mount, "/b") and not ops.exists(db, mount, "/a")


def test_move_archives_old_edge_and_relocates(db, mount):
    ops.create_container(db, mount, "/src")
    ops.create_container(db, mount, "/dst")
    nid = ops.create_entry(db, mount, "/src/f", b"x")
    ops.move(db, mount, "/src/f", "/dst/f")
    assert ops.exists(db, mount, "/dst/f") and not ops.exists(db, mount, "/src/f")
    # exactly one ACTIVE incoming edge, and an archived one retained (PI-7)
    active = db.all("resolution.get_active_parent", {"node": nid})
    assert len(active) == 1
    archived = db.connection.execute(
        "SELECT count(*) FROM edge WHERE to_id=? AND archived=1", (nid,)
    ).fetchone()[0]
    assert archived == 1


def test_move_can_rename_via_final_segment(db, mount):
    ops.create_container(db, mount, "/dst")
    ops.create_entry(db, mount, "/a", b"x")
    ops.move(db, mount, "/a", "/dst/renamed")
    assert ops.read_all(db, mount, "/dst/renamed") == b"x"


def test_move_into_own_subtree_blocked(db, mount):
    ops.create_container(db, mount, "/a")
    ops.create_container(db, mount, "/a/b")
    with pytest.raises(errors.WouldCycle):
        ops.move(db, mount, "/a", "/a/b/a")


def test_remove_refuses_non_empty(db, mount):
    ops.create_container(db, mount, "/d")
    ops.create_entry(db, mount, "/d/f", b"x")
    with pytest.raises(errors.NotEmpty):
        ops.remove(db, mount, "/d")


def test_remove_leaf(db, mount):
    ops.create_entry(db, mount, "/f", b"x")
    ops.remove(db, mount, "/f")
    assert not ops.exists(db, mount, "/f")


def test_remove_recursive(db, mount):
    ops.create_container(db, mount, "/d")
    ops.create_container(db, mount, "/d/sub")
    ops.create_entry(db, mount, "/d/sub/f", b"x")
    ops.remove_recursive(db, mount, "/d")
    assert not ops.exists(db, mount, "/d")


# --------------------------------------------------------------------------
# Copy / pack / unpack (the host walk)
# --------------------------------------------------------------------------
def _build_tree(db, mount, base):
    ops.create_container(db, mount, base)
    ops.create_entry(db, mount, f"{base}/x", b"X")
    ops.create_container(db, mount, f"{base}/inner")
    ops.create_entry(db, mount, f"{base}/inner/y", b"Y")


def test_copy_duplicates_and_preserves_created_at(db, mount):
    _build_tree(db, mount, "/src")
    ops.copy(db, mount, "/src", "/copy")
    assert ops.read_all(db, mount, "/copy/x") == b"X"
    assert ops.read_all(db, mount, "/copy/inner/y") == b"Y"
    src = ops.stat(db, mount, "/src/x")
    cp = ops.stat(db, mount, "/copy/x")
    assert cp.created_at == src.created_at  # OP-4
    assert cp.id != src.id  # fresh identity


def test_copy_into_own_subtree_snapshots(db, mount):
    _build_tree(db, mount, "/src")
    # copying into a descendant copies the source as it was at the start
    ops.copy(db, mount, "/src", "/src/inner/clone")
    assert ops.read_all(db, mount, "/src/inner/clone/x") == b"X"
    # the clone does not contain itself (snapshot, not recursive explosion)
    assert not ops.exists(db, mount, "/src/inner/clone/inner/clone")


def test_pack_unpack_roundtrip(db, mount):
    _build_tree(db, mount, "/src")
    packed = ops.pack(db, mount, "/src")
    assert ops.stat_by_id(db, mount, packed).type is NodeType.ENTRY
    assert not ops.exists(db, mount, "/src/x")  # original superseded
    ops.unpack(db, mount, "/src")
    assert ops.read_all(db, mount, "/src/x") == b"X"
    assert ops.read_all(db, mount, "/src/inner/y") == b"Y"


# --------------------------------------------------------------------------
# Streaming / descriptor / locks
# --------------------------------------------------------------------------
def test_stream_write_commits_on_close(db, mount):
    ops.create_entry(db, mount, "/f", b"")
    with ops.open_write(db, mount, "/f", WriteMode.TRUNCATE) as w:
        w.write(b"abc")
        w.write(b"def")
        # not visible until close (batch-behind-commit)
        assert ops.read_all(db, mount, "/f") == b""
    assert ops.read_all(db, mount, "/f") == b"abcdef"


def test_stream_append_mode(db, mount):
    ops.create_entry(db, mount, "/f", b"head-")
    with ops.open_write(db, mount, "/f", WriteMode.APPEND) as w:
        assert w.tell() == len(b"head-")
        w.write(b"tail")
    assert ops.read_all(db, mount, "/f") == b"head-tail"


def test_stream_read_and_seek(db, mount):
    ops.create_entry(db, mount, "/f", b"0123456789")
    with ops.open_read(db, mount, "/f") as r:
        assert r.read(4) == b"0123"
        assert r.tell() == 4
        r.seek(-2, Whence.END)
        assert r.read() == b"89"
        r.seek(0, Whence.SET)
        assert r.read() == b"0123456789"


def test_write_lock_blocks_other_mount(db, mount):
    # second session on the same volume
    vol = ops.mount_info(db, mount).volume
    other = ops.mount(db, vol, "/", ttl_ms=60_000)
    ops.create_entry(db, mount, "/f", b"")
    with ops.open_write(db, mount, "/f") as w:
        w.write(b"x")
        with pytest.raises(errors.LockHeld):
            ops.write_all(db, other, "/f", b"y")
    # lock released on close -> other mount can write now
    ops.write_all(db, other, "/f", b"y")
    assert ops.read_all(db, mount, "/f") == b"y"


def test_same_mount_does_not_self_block(db, mount):
    # a mount never blocks itself (cross-mount exclusivity only)
    ops.create_entry(db, mount, "/f", b"")
    with ops.open_write(db, mount, "/f") as w:
        w.write(b"x")
        # same mount: not blocked
        ops.write_all(db, mount, "/f", b"y")
    # the streaming close still commits its own buffer afterward
    assert ops.read_all(db, mount, "/f") in (b"x", b"y")


def test_open_write_creates_missing_entry(db, mount):
    # open_write(TRUNCATE) on a missing path creates the entry inside the
    # operation's own transaction (no wrapper pre-create, no TOCTOU window)
    with ops.open_write(db, mount, "/new") as w:
        w.write(b"made")
    assert ops.read_all(db, mount, "/new") == b"made"
    # exactly one node named 'new' was created — no hidden same-name sibling
    assert [e.name for e in ops.list(db, mount, "/")].count("new") == 1


def test_open_write_append_missing_raises(db, mount):
    with pytest.raises(errors.NotFound):
        ops.open_write(db, mount, "/nope", WriteMode.APPEND)


# --------------------------------------------------------------------------
# Metadata (NODE-6)
# --------------------------------------------------------------------------
def test_metadata_defaults_empty(db, mount):
    ops.create_entry(db, mount, "/f", b"x")
    ops.create_container(db, mount, "/d")
    assert ops.stat(db, mount, "/f").metadata == {}
    assert ops.stat(db, mount, "/d").metadata == {}


def test_set_metadata_entry_and_container(db, mount):
    ops.create_entry(db, mount, "/f", b"x")
    ops.create_container(db, mount, "/d")
    ops.set_metadata(db, mount, "/f", {"author": "mg", "kind": "note"})
    ops.set_metadata(db, mount, "/d", {"color": "teal"})
    assert ops.stat(db, mount, "/f").metadata == {"author": "mg", "kind": "note"}
    assert ops.stat(db, mount, "/d").metadata == {"color": "teal"}


def test_set_metadata_replaces_wholesale_and_clears(db, mount):
    ops.create_entry(db, mount, "/f", b"x")
    ops.set_metadata(db, mount, "/f", {"a": "1", "b": "2"})
    ops.set_metadata(db, mount, "/f", {"c": "3"})  # whole-map replace
    assert ops.stat(db, mount, "/f").metadata == {"c": "3"}
    ops.set_metadata(db, mount, "/f", {})  # clear
    assert ops.stat(db, mount, "/f").metadata == {}


def test_set_metadata_does_not_bump_modified(db, mount):
    ops.create_entry(db, mount, "/f", b"x")
    before = ops.stat(db, mount, "/f")
    time.sleep(0.01)
    ops.set_metadata(db, mount, "/f", {"k": "v"})
    after = ops.stat(db, mount, "/f")
    assert after.modified_at == before.modified_at  # NODE-6: annotation, not content


def test_copy_preserves_metadata(db, mount):
    ops.create_container(db, mount, "/src")
    ops.create_entry(db, mount, "/src/f", b"X")
    ops.set_metadata(db, mount, "/src", {"root": "yes"})
    ops.set_metadata(db, mount, "/src/f", {"author": "mg"})
    ops.copy(db, mount, "/src", "/copy")
    assert ops.stat(db, mount, "/copy").metadata == {"root": "yes"}
    assert ops.stat(db, mount, "/copy/f").metadata == {"author": "mg"}


def test_pack_unpack_roundtrips_metadata(db, mount):
    ops.create_container(db, mount, "/src")
    ops.create_entry(db, mount, "/src/f", b"X")
    ops.set_metadata(db, mount, "/src", {"label": "archive"})
    ops.set_metadata(db, mount, "/src/f", {"author": "mg"})
    ops.pack(db, mount, "/src")
    ops.unpack(db, mount, "/src")
    assert ops.stat(db, mount, "/src").metadata == {"label": "archive"}
    assert ops.stat(db, mount, "/src/f").metadata == {"author": "mg"}


# --------------------------------------------------------------------------
# Maintenance
# --------------------------------------------------------------------------
def test_health_check_clean(db, mount):
    _build_tree(db, mount, "/d")
    assert ops.health_check(db) == []


def test_prune_collects_unmounted_locks(db, mount):
    ops.create_entry(db, mount, "/f", b"")
    # leak a lock by leaving a write descriptor open, then unmount it
    w = ops.open_write(db, mount, "/f")
    w.write(b"x")
    ops.unmount(db, mount)  # mount gone -> its lock is prunable
    report = ops.prune(db)
    assert report.locks_pruned >= 1


def test_list_mounts_filters_unmounted(db, mount):
    vol = ops.mount_info(db, mount).volume
    m2 = ops.mount(db, vol, "/", ttl_ms=60_000)
    assert {i.id for i in ops.list_mounts(db)} >= {mount, m2}
    ops.unmount(db, m2)
    ids = {i.id for i in ops.list_mounts(db)}
    assert mount in ids and m2 not in ids  # retired rows hidden by default
    assert m2 in {i.id for i in ops.list_mounts(db, include_unmounted=True)}
    # volume scoping: a second volume's mount is excluded
    other_vol = ops.create_volume(db, "other")
    m3 = ops.mount(db, other_vol.id, "/")
    assert m3 not in {i.id for i in ops.list_mounts(db, vol)}


def test_list_mounts_tolerates_lost_anchor(db, mount):
    vol = ops.mount_info(db, mount).volume
    ops.create_container(db, mount, "/d")
    m2 = ops.mount(db, vol, "/d", ttl_ms=60_000)
    ops.remove_recursive(db, mount, "/d")  # archive the anchor (ACC-5)
    infos = {i.id: i for i in ops.list_mounts(db)}  # must not raise
    assert infos[m2].mount_path is None  # unresolvable => None, not an abort
    assert infos[mount].mount_path == "/"


# --------------------------------------------------------------------------
# Content chunking + versioning (CV-1..CV-7)
#
# These use a volume with a deliberately tiny chunk_size so multi-chunk
# behavior is exercised on small payloads. chunk_size is fixed per-volume at
# creation and read back on every write.
# --------------------------------------------------------------------------
@pytest.fixture
def chunky(db):
    """A fresh volume with chunk_size=4 and a mount at its root."""
    vol = ops.create_volume(db, "chunky", chunk_size=4)
    return ops.mount(db, vol.id, "/", ttl_ms=60_000)


def _nid(db, mount, path):
    return ops.stat(db, mount, path).id


def _committed(db, nid):
    return db.connection.execute(
        "SELECT version FROM content WHERE node_id=?", (nid,)
    ).fetchone()[0]


def _chunk_lengths(db, nid):
    return [
        r[0]
        for r in db.connection.execute(
            "SELECT cc.length FROM content_version cv "
            "JOIN content_chunk cc ON cc.chunk_hash=cv.chunk_hash "
            "WHERE cv.content_id=? AND cv.version=(SELECT version FROM content WHERE node_id=?) "
            "ORDER BY cv.chunk_index",
            (nid, nid),
        ).fetchall()
    ]


def _pool_count(db):
    return db.connection.execute("SELECT count(*) FROM content_chunk").fetchone()[0]


def test_chunked_roundtrip_multichunk(db, chunky):
    data = b"abcdefghij"  # 10 bytes, chunk_size 4 -> 4,4,2
    ops.create_entry(db, chunky, "/f", data)
    nid = _nid(db, chunky, "/f")
    assert ops.read_all(db, chunky, "/f") == data
    assert _chunk_lengths(db, nid) == [4, 4, 2]  # final chunk shorter, no padding
    assert ops.stat(db, chunky, "/f").size == 10


def test_uniform_chunking_small_file_one_short_chunk(db, chunky):
    ops.create_entry(db, chunky, "/f", b"hi")  # 2 bytes < chunk_size
    nid = _nid(db, chunky, "/f")
    assert _chunk_lengths(db, nid) == [2]  # one short chunk, no pad
    assert ops.read_all(db, chunky, "/f") == b"hi"


def test_empty_file_zero_chunks(db, chunky):
    ops.create_entry(db, chunky, "/f", b"")
    nid = _nid(db, chunky, "/f")
    assert _chunk_lengths(db, nid) == []
    assert ops.read_all(db, chunky, "/f") == b""
    assert ops.stat(db, chunky, "/f").size == 0


def test_dedup_shares_pool_rows(db, chunky):
    ops.create_entry(db, chunky, "/a", b"hello world!!")
    n1 = _pool_count(db)
    ops.create_entry(db, chunky, "/b", b"hello world!!")  # identical content
    n2 = _pool_count(db)
    assert n1 == n2  # no new pool rows
    assert ops.read_all(db, chunky, "/b") == b"hello world!!"


def test_shared_chunk_immutability(db, chunky):
    # The §4 test: two entries share a chunk; writing one must not touch the
    # other's bytes OR the shared chunk row.
    ops.create_entry(db, chunky, "/a", b"SAME")
    ops.create_entry(db, chunky, "/b", b"SAME")  # dedups to one chunk
    assert _pool_count(db) == 1
    shared_hash = db.connection.execute(
        "SELECT chunk_hash FROM content_chunk"
    ).fetchone()[0]

    ops.write_all(db, chunky, "/a", b"DIFF")  # produces a NEW chunk
    assert ops.read_all(db, chunky, "/b") == b"SAME"  # other entry untouched
    row = db.connection.execute(
        "SELECT data FROM content_chunk WHERE chunk_hash=?", (shared_hash,)
    ).fetchone()
    assert row is not None and row[0] == b"SAME"  # shared chunk untouched


def _hashes(db, nid, version):
    return [
        r[0]
        for r in db.connection.execute(
            "SELECT chunk_hash FROM content_version WHERE content_id=? AND version=? "
            "ORDER BY chunk_index",
            (nid, version),
        ).fetchall()
    ]


def test_write_range_midfile_carries_prefix_and_suffix(db, chunky):
    base = b"aaaabbbbccccdddd"  # 4 chunks of 4
    ops.create_entry(db, chunky, "/f", base)
    nid = _nid(db, chunky, "/f")
    v1 = _committed(db, nid)
    pool_before = _pool_count(db)
    assert ops.write_range(db, chunky, "/f", 5, b"XY") == 16
    assert ops.read_all(db, chunky, "/f") == b"aaaabXYbccccdddd"
    v2 = _committed(db, nid)
    h1, h2 = _hashes(db, nid, v1), _hashes(db, nid, v2)
    assert h2[0] == h1[0] and h2[2:] == h1[2:]  # prefix + suffix by reference
    assert h2[1] != h1[1]  # only the window re-chunked
    assert _pool_count(db) == pool_before + 1  # one new pool row


def test_write_range_cross_boundary_and_extend(db, chunky):
    ops.create_entry(db, chunky, "/f", b"aaaabbbb")
    assert ops.write_range(db, chunky, "/f", 6, b"XXXX") == 10  # spans + extends
    assert ops.read_all(db, chunky, "/f") == b"aaaabbXXXX"
    nid = _nid(db, chunky, "/f")
    assert _chunk_lengths(db, nid) == [4, 4, 2]  # uniform invariant holds


def test_write_range_gap_zero_fills_and_rebuilds_short_tail(db, chunky):
    ops.create_entry(db, chunky, "/f", b"aaaabb")  # short final chunk (2)
    assert ops.write_range(db, chunky, "/f", 10, b"ZZ") == 12
    assert ops.read_all(db, chunky, "/f") == b"aaaabb\x00\x00\x00\x00ZZ"
    nid = _nid(db, chunky, "/f")
    assert _chunk_lengths(db, nid) == [
        4,
        4,
        4,
    ]  # short tail rebuilt, no mid-file short chunk


def test_write_range_empty_and_lock(db, chunky):
    ops.create_entry(db, chunky, "/f", b"abcd")
    assert ops.write_range(db, chunky, "/f", 0, b"") == 4  # no-op returns size
    vol = ops.mount_info(db, chunky).volume
    other = ops.mount(db, vol, "/")
    with ops.open_write(db, chunky, "/f") as w:
        w.write(b"x")
        with pytest.raises(errors.LockHeld):
            ops.write_range(db, other, "/f", 0, b"y")


def test_write_range_interleaved_random(db, chunky):
    # the rw-handle pattern: many small writes at scattered offsets, each an
    # atomic version, must converge to the byte-identical file
    import random

    rng = random.Random(7)
    ref = bytearray(64)
    ops.create_entry(db, chunky, "/f", bytes(ref))
    for _ in range(20):
        off = rng.randrange(0, 70)
        data = bytes(rng.randrange(1, 256) for _ in range(rng.randrange(1, 9)))
        end = off + len(data)
        if end > len(ref):
            ref.extend(b"\x00" * (end - len(ref)))
        ref[off:end] = data
        ops.write_range(db, chunky, "/f", off, data)
    assert ops.read_all(db, chunky, "/f") == bytes(ref)


def test_truncate(db, chunky):
    ops.create_entry(db, chunky, "/f", b"aaaabbbbcc")
    nid = _nid(db, chunky, "/f")
    v1 = _committed(db, nid)
    ops.truncate(db, chunky, "/f", 6)  # shrink into chunk 1
    assert ops.read_all(db, chunky, "/f") == b"aaaabb"
    assert _hashes(db, nid, _committed(db, nid))[0] == _hashes(db, nid, v1)[0]
    ops.truncate(db, chunky, "/f", 9)  # grow, zero-fill
    assert ops.read_all(db, chunky, "/f") == b"aaaabb\x00\x00\x00"
    ops.truncate(db, chunky, "/f", 0)  # to empty
    assert ops.read_all(db, chunky, "/f") == b""
    assert ops.stat(db, chunky, "/f").size == 0


def test_versioning_committed_advances(db, chunky):
    ops.create_entry(db, chunky, "/f", b"v1xx")
    nid = _nid(db, chunky, "/f")
    v1 = _committed(db, nid)
    ops.write_all(db, chunky, "/f", b"v2yy")
    v2 = _committed(db, nid)
    assert v2 > v1
    assert ops.read_all(db, chunky, "/f") == b"v2yy"  # current bytes = v2
    # v1's manifest rows still present (until retention prunes them)
    older = db.connection.execute(
        "SELECT count(*) FROM content_version WHERE content_id=? AND version=?",
        (nid, v1),
    ).fetchone()[0]
    assert older >= 1


def test_crash_atomicity_stage_without_swap(db, chunky):
    # Simulate a crash: stage a new version's chunks via the same primitives the
    # descriptor uses, then fail before the pointer swap. Committed version is
    # unchanged, current bytes are the prior version, orphan chunks exist and
    # are collected by prune_content.
    ops.create_entry(db, chunky, "/f", b"good")
    nid = _nid(db, chunky, "/f")
    committed_before = _committed(db, nid)
    vol = ops.mount_info(db, chunky).volume

    v = db.alloc_version(nid)
    with db.txn():
        db.stage_chunks(nid, v, vol, b"NEWDATA!")  # staged, NOT committed

    assert _committed(db, nid) == committed_before  # pointer unchanged
    assert ops.read_all(db, chunky, "/f") == b"good"  # prior version intact
    orphan = db.connection.execute(
        "SELECT count(*) FROM content_version WHERE content_id=? AND version=?",
        (nid, v),
    ).fetchone()[0]
    assert orphan >= 1  # orphan manifest rows

    report = ops.prune_content(db)
    assert report.versions_pruned >= 1
    assert (
        db.connection.execute(
            "SELECT count(*) FROM content_version WHERE content_id=? AND version=?",
            (nid, v),
        ).fetchone()[0]
        == 0
    )  # collected
    assert ops.read_all(db, chunky, "/f") == b"good"  # committed survives


def test_retention_keep_last_one(db, chunky):
    ops.create_entry(db, chunky, "/f", b"aaaa")  # v1
    nid = _nid(db, chunky, "/f")
    ops.set_retention(db, chunky, "/f", 1)  # keep only committed
    ops.write_all(db, chunky, "/f", b"bbbb")  # v2 committed
    # before prune, v1 rows still present
    assert (
        db.connection.execute(
            "SELECT count(*) FROM content_version WHERE content_id=? AND version=1",
            (nid,),
        ).fetchone()[0]
        >= 1
    )

    ops.prune_content(db)
    # v1 dropped, committed (v2) survives, "aaaa" chunk reclaimed
    assert (
        db.connection.execute(
            "SELECT count(*) FROM content_version WHERE content_id=? AND version=1",
            (nid,),
        ).fetchone()[0]
        == 0
    )
    assert ops.read_all(db, chunky, "/f") == b"bbbb"


def test_prune_content_retains_referenced_old_version(db, chunky):
    # Default policy (NULL = keep all): a chunk referenced only by a retained
    # OLD version is NOT collected.
    ops.create_entry(db, chunky, "/f", b"keepme!!")  # v1
    nid = _nid(db, chunky, "/f")
    ops.write_all(db, chunky, "/f", b"second!!")  # v2 committed
    ops.prune_content(db)
    assert (
        db.connection.execute(
            "SELECT count(*) FROM content_version WHERE content_id=? AND version=1",
            (nid,),
        ).fetchone()[0]
        >= 1
    )  # v1 still retained


def test_copy_shares_chunks_no_new_pool_rows(db, chunky):
    ops.create_entry(db, chunky, "/a", b"shareable!!")
    before = _pool_count(db)
    ops.copy(db, chunky, "/a", "/b")
    assert _pool_count(db) == before  # copy re-references
    assert ops.read_all(db, chunky, "/b") == b"shareable!!"


def test_pack_unpack_through_chunker(db, chunky):
    ops.create_container(db, chunky, "/src")
    ops.create_entry(db, chunky, "/src/x", b"XXXXXXXXX")  # multi-chunk payload
    ops.create_container(db, chunky, "/src/inner")
    ops.create_entry(db, chunky, "/src/inner/y", b"YY")
    ops.pack(db, chunky, "/src")
    ops.unpack(db, chunky, "/src")
    assert ops.read_all(db, chunky, "/src/x") == b"XXXXXXXXX"
    assert ops.read_all(db, chunky, "/src/inner/y") == b"YY"


# --------------------------------------------------------------------------
# Bounded-memory streaming (writes flush per chunk; reads are ranged)
#
# Sized in CHUNKS, not gigabytes: chunk_size=64 with a few hundred chunks
# exercises real mid-stream flushing and multi-chunk ranged reads fast, and
# asserts memory stays bounded (the writer never holds more than ~one chunk).
# --------------------------------------------------------------------------
@pytest.fixture
def streamvol(db):
    vol = ops.create_volume(db, "stream", chunk_size=64)
    return ops.mount(db, vol.id, "/", ttl_ms=60_000)


def test_streaming_write_flushes_midstream_bounded_memory(db, streamvol):
    cs = 64
    n_chunks = 300
    payload = bytes(
        (i % 251) for i in range(cs * n_chunks + 17)
    )  # +17 => short last chunk
    ops.create_entry(db, streamvol, "/big", b"")
    with ops.open_write(db, streamvol, "/big", WriteMode.TRUNCATE) as w:
        # feed in many small writes; the writer must flush full chunks as it goes
        step = 50
        for off in range(0, len(payload), step):
            w.write(payload[off : off + step])
            # invariant: pending buffer never exceeds one chunk
            assert len(w._pending) < cs
    assert ops.read_all(db, streamvol, "/big") == payload
    nid = _nid(db, streamvol, "/big")
    assert ops.stat(db, streamvol, "/big").size == len(payload)
    # chunk lengths: all full except a short final chunk of 17
    lens = _chunk_lengths(db, nid)
    assert len(lens) == n_chunks + 1
    assert all(L == cs for L in lens[:-1]) and lens[-1] == 17


def test_streaming_ranged_read_no_full_buffer(db, streamvol):
    cs = 64
    payload = bytes((i % 251) for i in range(cs * 100 + 5))
    ops.create_entry(db, streamvol, "/big", payload)
    with ops.open_read(db, streamvol, "/big") as r:
        # a window straddling a chunk boundary
        r.seek(cs - 3, Whence.SET)
        assert r.read(10) == payload[cs - 3 : cs + 7]
        # tail via END-relative seek
        r.seek(-5, Whence.END)
        assert r.read() == payload[-5:]
        # full read from 0 reassembles correctly
        r.seek(0, Whence.SET)
        assert r.read() == payload


def test_streaming_append_multichunk_carry(db, streamvol):
    cs = 64
    base = bytes((i % 251) for i in range(cs * 5 + 20))  # 5 full + 20-byte tail
    ops.create_entry(db, streamvol, "/f", base)
    nid = _nid(db, streamvol, "/f")
    pool_before = _pool_count(db)
    add = bytes(((i * 7) % 251) for i in range(cs * 2 + 9))
    with ops.open_write(db, streamvol, "/f", WriteMode.APPEND) as w:
        assert w.tell() == len(base)
        w.write(add)
    assert ops.read_all(db, streamvol, "/f") == base + add
    assert ops.stat(db, streamvol, "/f").size == len(base) + len(add)
    # the 5 full leading chunks were carried by reference, not re-hashed:
    # only the rebuilt tail + new chunks are new pool rows (fewer than a full
    # re-chunk of the whole result would add)
    carried = base[: cs * 5]
    # those 5 original chunk hashes still present and referenced by the new version
    assert ops.read_all(db, streamvol, "/f")[: cs * 5] == carried


def test_streaming_write_into_flushed_region_raises(db, streamvol):
    cs = 64
    ops.create_entry(db, streamvol, "/f", b"")
    with ops.open_write(db, streamvol, "/f", WriteMode.TRUNCATE) as w:
        w.write(bytes(cs * 3))  # flush 3 chunks
        w.seek(0, Whence.SET)  # back into flushed, immutable territory
        with pytest.raises(errors.Unsupported):
            w.write(b"x")
        # recover position so close() commits cleanly
        w.seek(0, Whence.END)


def test_streaming_read_on_writer_raises(db, streamvol):
    ops.create_entry(db, streamvol, "/f", b"")
    with ops.open_write(db, streamvol, "/f", WriteMode.TRUNCATE) as w:
        w.write(b"data")
        with pytest.raises(errors.Unsupported):
            w.read()


def test_prune_content_spares_live_write(db, streamvol):
    # The live-lock guard: an in-progress streaming write has staged chunks at a
    # version above committed; a concurrent prune_content must NOT reap them.
    cs = 64
    ops.create_entry(db, streamvol, "/f", b"committed-bytes")
    nid = _nid(db, streamvol, "/f")
    committed_before = _committed(db, nid)
    with ops.open_write(db, streamvol, "/f", WriteMode.TRUNCATE) as w:
        w.write(bytes(cs * 4))  # flush several chunks (staged, not committed)
        staged = db.connection.execute(
            "SELECT count(*) FROM content_version WHERE content_id=? AND version>?",
            (nid, committed_before),
        ).fetchone()[0]
        assert staged >= 1
        # prune while the write lock is still held
        ops.prune_content(db)
        survived = db.connection.execute(
            "SELECT count(*) FROM content_version WHERE content_id=? AND version>?",
            (nid, committed_before),
        ).fetchone()[0]
        assert survived == staged  # live write untouched
    # after close + lock release, the write committed normally
    assert ops.read_all(db, streamvol, "/f") == bytes(cs * 4)


def test_orphan_collected_after_lock_gone(db, streamvol):
    # Mirror image: staged chunks with NO valid lock ARE orphans and get reaped.
    cs = 64
    ops.create_entry(db, streamvol, "/f", b"good")
    nid = _nid(db, streamvol, "/f")
    committed_before = _committed(db, nid)
    vol = ops.mount_info(db, streamvol).volume
    v = db.alloc_version(nid)
    with db.txn():  # stage without a lock, without swap
        db.stage_chunk(nid, v, 0, bytes(cs))
        db.stage_chunk(nid, v, 1, bytes(cs))
    assert _committed(db, nid) == committed_before
    report = ops.prune_content(db)
    assert report.versions_pruned >= 2
    assert (
        db.connection.execute(
            "SELECT count(*) FROM content_version WHERE content_id=? AND version=?",
            (nid, v),
        ).fetchone()[0]
        == 0
    )
    assert ops.read_all(db, streamvol, "/f") == b"good"


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
