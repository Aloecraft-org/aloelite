# ./tests/test_holder.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""Mount holders (#15): a mount records the process holding it, and a daemon
retires a mount whose holder is provably gone instead of waiting out its
lease. Anything it cannot judge is never called gone."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from aloelite import holder
from aloelite.aloelite import Aloelite

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="holders are read from /proc"
)


def _exited_holder() -> str:
    child = subprocess.Popen(["sleep", "30"])
    start = holder._starttime(child.pid)
    child.kill()
    child.wait()
    boot, pidns = holder._here()
    return f"linux boot={boot} pidns={pidns} pid={child.pid} start={start} host=x"


def test_this_process_is_its_own_live_holder():
    me = holder.identity()
    assert me is not None and me.startswith("linux boot=")
    assert not holder.is_gone(me)


def test_an_exited_or_reused_pid_is_gone():
    assert holder.is_gone(_exited_holder())
    boot, pidns = holder._here()
    start = holder._starttime(os.getpid())
    reused = f"linux boot={boot} pidns={pidns} pid={os.getpid()} start={start + 1}"
    assert holder.is_gone(reused)


@pytest.mark.parametrize(
    "unjudgeable",
    [
        None,
        "",
        "windows pid=1",
        "linux boot=other pidns={ns} pid=1 start=1",
        "linux boot={boot} pidns=1 pid=1 start=1",
        "linux boot={boot} pidns={ns} host=x",
    ],
)
def test_what_cannot_be_judged_is_never_gone(unjudgeable):
    boot, ns = holder._here()
    if unjudgeable:
        unjudgeable = unjudgeable.format(boot=boot, ns=ns)
    assert not holder.is_gone(unjudgeable)


def test_retire_dead_frees_the_subtree_and_spares_the_living(tmp_path: Path):
    with Aloelite(tmp_path / "h.fs") as fs:
        vol = fs.create_volume("v").id
        fs.mount(vol, ttl_ms=300_000, holder=_exited_holder())
        assert holder.retire_dead(fs, vol) == 1
        live = fs.mount(vol, ttl_ms=300_000, holder=holder.identity())
        assert fs.list_mounts(vol)[0].holder == holder.identity()
        assert holder.retire_dead(fs, vol) == 0
        live.unmount()


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
