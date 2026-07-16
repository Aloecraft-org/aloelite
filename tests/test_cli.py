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


def test_volumes_and_mounts(fsfile, capsys):
    assert run("-f", fsfile, "ls") == 0          # mints a mount row
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
