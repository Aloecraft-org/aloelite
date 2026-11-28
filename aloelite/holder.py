# ./aloelite/holder.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
Who holds a mount, and whether they are provably gone (#15).

A daemon killed with SIGKILL never unmounts, so its rw mount row blocked the
next daemon on the volume until the row's lease ran out. Each daemon now
records itself as the mount's holder, and the next one retires any mount
whose holder is a process on this same kernel and pid namespace that no
longer exists. Anything it cannot judge is left to the lease.

Surface:
    identity()                -> this process as a holder string, or None
    is_gone(holder)           -> True only when the holder is provably dead
    retire_dead(fs, volume)   -> unmount every such mount on the volume

Configurable: none. The format is fixed by mount-api.yaml `mount.holder`
and shared with rust/aloelite-fuse/src/holder.rs:

    linux boot=<boot_id> pidns=<inode> pid=<pid> start=<starttime> host=<name>
"""

from __future__ import annotations

import errno
import os
import socket

_SCHEME = "linux"
_BOOT_ID = "/proc/sys/kernel/random/boot_id"
_PIDNS = "/proc/self/ns/pid"


def identity() -> str | None:
    """This process as a holder, or None where there is no /proc to say so."""
    here = _here()
    start = _starttime(os.getpid())
    if here is None or start is None:
        return None
    boot, pidns = here
    host = socket.gethostname().replace(" ", "_") or "-"
    return (
        f"{_SCHEME} boot={boot} pidns={pidns} pid={os.getpid()} "
        f"start={start} host={host}"
    )


def is_gone(holder: str | None) -> bool:
    """True only when `holder` names a process of this boot and pid namespace
    that has exited (or whose pid now belongs to a later process). A holder
    from another host, boot or namespace, or none at all, cannot be judged
    from here, and is never called gone."""
    fields = _parse(holder)
    here = _here()
    if fields is None or here is None:
        return False
    if (fields.get("boot"), fields.get("pidns")) != here:
        return False
    try:
        pid, start = int(fields["pid"]), int(fields["start"])
    except (KeyError, ValueError):
        return False
    if pid == os.getpid():
        return start != _starttime(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        pass  # it exists; the start time below says whether it is the same one
    except OSError as e:  # pragma: no cover - kill(0) has no other failure
        return e.errno == errno.ESRCH
    now = _starttime(pid)
    return now is not None and now != start


def retire_dead(fs, volume) -> int:
    """Unmount every mount on `volume` whose holder is_gone. Returns how many.
    Unmount marks the row retired; its locks go invalid with it and prune
    reclaims them later (ACC-10)."""
    retired = 0
    for info in fs.list_mounts(volume):
        if is_gone(info.holder):
            fs.attach(info.id).unmount()
            retired += 1
    return retired


# depth: /proc parsing
def _here() -> tuple[str, str] | None:
    try:
        with open(_BOOT_ID) as f:
            boot = f.read().strip()
        pidns = os.readlink(_PIDNS)  # "pid:[4026531836]"
    except OSError:
        return None
    inode = pidns[pidns.find("[") + 1 : pidns.rfind("]")]
    if not boot or not inode.isdigit():
        return None
    return boot, inode


def _starttime(pid: int) -> int | None:
    """Field 22 of /proc/<pid>/stat: clock ticks after boot when the process
    started. The command name (field 2) may hold spaces and parentheses, so
    count from the last ')'."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            stat = f.read()
    except OSError:
        return None
    rest = stat[stat.rfind(")") + 2 :].split()
    try:
        return int(rest[19])  # rest[0] is field 3
    except (IndexError, ValueError):
        return None


def _parse(holder: str | None) -> dict[str, str] | None:
    if not holder:
        return None
    scheme, _, rest = holder.partition(" ")
    if scheme != _SCHEME:
        return None
    return dict(tok.split("=", 1) for tok in rest.split() if "=" in tok)


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
