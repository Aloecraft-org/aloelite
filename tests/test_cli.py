# ./tests/test_cli.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""Lean end-to-end tests for the aloelite CLI (main(argv) against a real file)."""

from __future__ import annotations

import pytest

from aloelite.aloelite import Aloelite
from aloelite.cli import main


@pytest.fixture
def fsfile(tmp_path):
    p = str(tmp_path / "t.fs")
    with Aloelite(p) as fs:
        fs.create_volume("vol")
    return p


def run(*argv):
    return main(list(argv))


def _tty_ok(real_open):
    import io

    def fake(path, *a, **k):
        if path == "/dev/tty":
            return io.StringIO()
        return real_open(path, *a, **k)

    return fake


def test_roundtrip(fsfile, tmp_path, capsys):
    src = tmp_path / "in.txt"
    src.write_bytes(b"hello cli")
    assert run("-f", fsfile, "put", str(src), "/a.txt") == 0
    assert run("-f", fsfile, "mkdir", "-p", "/d/e") == 0
    assert run("-f", fsfile, "mv", "/a.txt", "/d/a.txt") == 0
    out = tmp_path / "out.txt"
    assert run("-f", fsfile, "get", "/d/a.txt", str(out)) == 0
    assert out.read_bytes() == b"hello cli"
    assert run("-f", fsfile, "ls", "/d") == 0
    assert "/d/a.txt" in capsys.readouterr().out
    assert run("-f", fsfile, "rm", "-r", "/d") == 0


def test_volume_selection(fsfile, capsys):
    # sole volume: no -v needed (exercised above); add a second -> refusal
    with Aloelite(fsfile) as fs:
        fs.create_volume("second")
    assert run("-f", fsfile, "ls") == 1  # refuses to guess
    assert run("-f", fsfile, "-v", "second", "ls") == 0
    # id works, dashed or bare hex
    with Aloelite(fsfile) as fs:
        vid = fs.resolve_volume_name("second")
    assert run("-f", fsfile, "-v", vid, "ls") == 0
    assert run("-f", fsfile, "-v", vid.replace("-", ""), "ls") == 0
    assert run("-f", fsfile, "-v", "nope", "ls") == 1


def test_encrypted_pin_env(fsfile, tmp_path, monkeypatch):
    p = str(tmp_path / "enc.fs")
    with Aloelite(p) as fs:
        fs.create_volume("vault", pin=b"s3cret")
    monkeypatch.setenv("ALOE_PIN", "s3cret")
    assert run("-f", p, "--pin-env", "ALOE_PIN", "mkdir", "/d") == 0
    assert run("-f", p, "--pin", "wrong", "ls") == 1  # BadKey -> exit 1
    # pin flag against an UNencrypted volume: early, pointed error
    assert run("-f", fsfile, "--pin-env", "ALOE_PIN", "ls") == 1


def test_bare_pin_prompts(fsfile, tmp_path, monkeypatch, capsys):
    p = str(tmp_path / "enc2.fs")
    with Aloelite(p) as fs:
        fs.create_volume("vault", pin=b"s3cret")
    import aloelite.pin as pinmod

    monkeypatch.setattr(pinmod.getpass, "getpass", lambda *a, **k: "s3cret")
    monkeypatch.setattr("builtins.open", _tty_ok(open), raising=False)
    assert run("--pin", "-f", p, "ls") == 0


def test_new_verbs(fsfile, tmp_path, capsys):
    src = tmp_path / "in.txt"
    src.write_bytes(b"data")
    run("-f", fsfile, "put", str(src), "/a.txt")
    assert run("-f", fsfile, "cat", "/a.txt") == 0
    assert "data" in capsys.readouterr().out
    assert run("-f", fsfile, "cp", "/a.txt", "/b.txt") == 0
    assert run("-f", fsfile, "stat", "/b.txt") == 0
    assert "entry" in capsys.readouterr().out
    run("-f", fsfile, "mkdir", "-p", "/d/e")
    assert run("-f", fsfile, "tree") == 0
    out = capsys.readouterr().out
    assert "├── " in out or "└── " in out


def _tree(root):
    """A local sample tree: nesting, an empty dir, a big file, an empty file."""
    (root / "sub" / "deep").mkdir(parents=True)
    (root / "empty").mkdir()
    (root / "a.txt").write_bytes(b"alpha")
    (root / "sub" / "big.bin").write_bytes(b"x" * (3 << 20))  # > _CHUNK: streams
    (root / "sub" / "deep" / "zero").write_bytes(b"")
    return root


def test_put_get_recursive_roundtrip(fsfile, tmp_path, capsys):
    src = _tree(tmp_path / "proj")
    # dst does not exist -> dst IS the tree root
    assert run("-f", fsfile, "put", "-r", str(src), "/code") == 0
    assert "3 files, 3 containers" in capsys.readouterr().out
    assert run("-f", fsfile, "ls", "/code/sub") == 0
    assert "/code/sub/big.bin" in capsys.readouterr().out
    out = tmp_path / "out"
    assert run("-f", fsfile, "get", "-r", "/code", str(out)) == 0
    assert (out / "a.txt").read_bytes() == b"alpha"
    assert (out / "sub" / "big.bin").read_bytes() == b"x" * (3 << 20)
    assert (out / "sub" / "deep" / "zero").read_bytes() == b""
    assert (out / "empty").is_dir()  # empty containers survive the round trip


def test_recursive_nests_into_existing_destination(fsfile, tmp_path, capsys):
    src = _tree(tmp_path / "proj")
    run("-f", fsfile, "mkdir", "/backup")
    # dst exists -> src lands INSIDE it, cp -r style
    assert run("-f", fsfile, "put", "-r", str(src), "/backup") == 0
    assert run("-f", fsfile, "ls", "/backup") == 0
    assert "/backup/proj" in capsys.readouterr().out
    dest = tmp_path / "dest"
    dest.mkdir()
    assert run("-f", fsfile, "get", "-r", "/backup/proj", str(dest)) == 0
    assert (dest / "proj" / "a.txt").read_bytes() == b"alpha"


def test_put_recursive_skips_what_it_cannot_carry(fsfile, tmp_path, capsys):
    src = _tree(tmp_path / "proj")
    (src / "link.txt").symlink_to("a.txt")  # file symlink: content is copied
    (src / "loop").symlink_to(src)  # dir symlink: skipped, never descended
    assert run("-f", fsfile, "put", "-r", str(src), "/code") == 0
    out = capsys.readouterr()
    assert "4 files, 3 containers, 1 skipped" in out.out
    assert "symlinked directory, skipped" in out.err


def test_recursive_rejects_the_wrong_shape(fsfile, tmp_path, capsys):
    src = _tree(tmp_path / "proj")
    run("-f", fsfile, "put", "-r", str(src), "/code")
    assert run("-f", fsfile, "put", str(src), "/x") == 1  # dir without -r
    assert "use -r" in capsys.readouterr().err
    assert run("-f", fsfile, "get", "/code", str(tmp_path / "z")) == 1  # container
    assert "use -r" in capsys.readouterr().err
    assert run("-f", fsfile, "put", "-r", str(src / "a.txt"), "/x") == 1  # a file
    assert run("-f", fsfile, "put", "-r", "-", "/x") == 1  # stdin has no tree
    assert run("-f", fsfile, "put", "-r", str(src), "/x", "--append") == 1
    assert run("-f", fsfile, "get", "-r", "/code/a.txt", str(tmp_path / "z")) == 1
    assert run("-f", fsfile, "get", "-r", "/code", "-") == 1  # no tree to stdout
    assert run("-f", fsfile, "get", "-r", "/nope", str(tmp_path / "z")) == 1
    assert run("-f", fsfile, "put", "-r", str(src), "/code/a.txt") == 1  # dst entry


def test_get_recursive_never_escapes_the_destination(fsfile, tmp_path, capsys):
    """'..' is an ordinary name in the store; it must not become one locally."""
    with Aloelite(fsfile) as fs:
        with fs.mount("vol") as m:
            m.mkdir("/evil")
            m.mkdir("/evil/..")  # a container literally named '..'
            m.create_entry("/evil/../child", b"escaped")  # ...with a child in it
            m.create_entry("/evil/ok.txt", b"fine")
    out = tmp_path / "safe"
    assert run("-f", fsfile, "get", "-r", "/evil", str(out)) == 0
    cap = capsys.readouterr()
    assert "unsafe local name" in cap.err
    assert "2 skipped" in cap.out  # the '..' container AND its subtree
    assert [p.name for p in out.iterdir()] == ["ok.txt"]
    assert not (tmp_path / "child").exists()


def test_file_from_env(fsfile, monkeypatch, capsys):
    monkeypatch.setenv("ALOELITE_FILE", fsfile)
    assert run("volumes") == 0
    assert "vol" in capsys.readouterr().out
    monkeypatch.delenv("ALOELITE_FILE")
    assert run("volumes") == 1  # no file, no env -> clear error


def test_prune(fsfile, capsys):
    assert run("-f", fsfile, "ls") == 0  # retires a mount (prunable lock-side state)
    assert run("-f", fsfile, "prune", "--vacuum") == 0
    out = capsys.readouterr().out
    assert "pruned:" in out and "vacuumed" in out


def test_volumes_and_mounts(fsfile, capsys):
    assert run("-f", fsfile, "ls") == 0  # mints a mount row
    assert run("-f", fsfile, "volumes") == 0
    assert "vol" in capsys.readouterr().out
    assert run("-f", fsfile, "mounts", "--all") == 0
    assert "unmounted" in capsys.readouterr().out  # session-per-invocation retired it


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
