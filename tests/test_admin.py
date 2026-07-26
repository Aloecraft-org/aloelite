import pytest

from aloelite.admin import main as admin_main
from aloelite.aloelite import Aloelite
from aloelite import errors


def test_change_pin_rotates(tmp_path):
    p = str(tmp_path / "e.fs")
    with Aloelite(p) as fs:
        v = fs.create_volume("vault", pin=b"old")
        with fs.mount(v.id, pin=b"old") as m:
            m.put("/a.txt", b"hi")
        fs.change_pin(v.id, b"old", b"new")
        with pytest.raises(errors.BadKey):
            fs.mount(v.id, pin=b"old")
        with fs.mount(v.id, pin=b"new") as m:
            assert m.read_all("/a.txt") == b"hi"


def test_change_pin_guards(tmp_path):
    p = str(tmp_path / "g.fs")
    with Aloelite(p) as fs:
        enc = fs.create_volume("vault", pin=b"pw")
        plain = fs.create_volume("plain")
        with pytest.raises(errors.BadKey):
            fs.change_pin(enc.id, b"wrong", b"new")
        with pytest.raises(errors.EncryptionRequired):
            fs.change_pin(plain.id, b"x", b"y")


def test_admin_cli(tmp_path, monkeypatch):
    p = str(tmp_path / "c.fs")
    with Aloelite(p) as fs:
        fs.create_volume("vault", pin=b"old")
    monkeypatch.setenv("OLD", "old")
    monkeypatch.setenv("NEW", "new")
    assert admin_main(
        ["-f", p, "--pin-env", "OLD", "pin", "change", "--new-pin-env", "NEW"]
    ) == 0
    assert admin_main(["-f", p, "--pin-env", "OLD", "pin", "change",
                       "--new-pin-env", "NEW"]) == 1  # old pin now wrong
    assert admin_main(["-f", p, "health"]) == 0
    assert admin_main(["-f", p, "info"]) == 0


def test_export_roundtrip(tmp_path):
    a = str(tmp_path / "a.fs")
    b = str(tmp_path / "b.fs")
    from aloelite.transfer import export_volume

    with Aloelite(a) as fs:
        v = fs.create_volume("vault", pin=b"old")
        with fs.mount(v.id, pin=b"old") as m:
            m.mkdir("/d", parents=True)
            m.put("/d/x.txt", b"payload")
            m.set_metadata("/d/x.txt", {"k": "v"})
    vid, c, e = export_volume(a, v.id, b, src_pin=b"old", dest_pin=b"new")
    assert (c, e) == (1, 1)
    with Aloelite(b) as fs:
        with fs.mount(vid, pin=b"new") as m:
            assert m.read_all("/d/x.txt") == b"payload"
            assert m.path("/d/x.txt").metadata == {"k": "v"}
            assert m.verify(deep=True).ok
        with pytest.raises(errors.BadKey):
            fs.mount(vid, pin=b"old")


def test_snapshot(tmp_path):
    import os

    from aloelite.transfer import snapshot_volume

    p = str(tmp_path / "s.fs")
    with Aloelite(p) as fs:
        v = fs.create_volume("live")
        with fs.mount(v.id) as m:
            m.put("/f", b"z" * (2 << 20))
    size_before = os.path.getsize(p)
    vid, _, e = snapshot_volume(p, v.id, "snap1")
    assert e == 1
    with Aloelite(p) as fs:
        # snapshot is independent: mutate live, snap unchanged
        with fs.mount(v.id) as m:
            m.put("/f", b"changed")
        with fs.mount(vid) as m:
            assert m.read_all("/f") == b"z" * (2 << 20)
    # plain snapshot dedups: growth far below a second 2 MiB copy
    assert os.path.getsize(p) - size_before < 1 << 20


def test_export_to_plain(tmp_path):
    a = str(tmp_path / "a2.fs")
    b = str(tmp_path / "b2.fs")
    from aloelite.transfer import export_volume

    with Aloelite(a) as fs:
        v = fs.create_volume("v", pin=b"pw")
        with fs.mount(v.id, pin=b"pw") as m:
            m.put("/f", b"x" * (3 << 20))  # multi-chunk, exercises streaming
    vid, _, e = export_volume(a, v.id, b, src_pin=b"pw")
    assert e == 1
    with Aloelite(b) as fs, fs.mount(vid) as m:
        assert len(m.read_all("/f")) == 3 << 20


def test_verify(tmp_path):
    p = str(tmp_path / "v.fs")
    with Aloelite(p) as fs:
        v = fs.create_volume("vault", pin=b"pw")
        with fs.mount(v.id, pin=b"pw") as m:
            m.put("/a.txt", b"hello world")
            rep = m.verify(deep=True)
            assert rep.ok and rep.entries_checked == 1 and rep.chunks_checked == 1
        # flip one ciphertext byte in the pool
        import sqlite3

        c = sqlite3.connect(p)
        (h, data) = c.execute(
            "SELECT chunk_hash, data FROM content_chunk LIMIT 1"
        ).fetchone()
        c.execute(
            "UPDATE content_chunk SET data = ? WHERE chunk_hash = ?",
            (bytes([data[0] ^ 1]) + data[1:], h),
        )
        c.commit()
        c.close()
        with fs.mount(v.id, pin=b"pw") as m:
            assert m.verify().ok            # shallow can't see bitrot
            rep = m.verify(deep=True)
            assert not rep.ok and any("address-mismatch" in x for x in rep.problems)
