# ./manager/preflight.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
manager.preflight — startup environment checks.

Each check is a pure function returning a CheckResult so it can be unit-tested
without exiting. `run_preflight` runs them all, logs each, and exits(1) if any
*fatal* check fails. Warnings (allow_other unverifiable, stale-mount recovery)
are logged but never fatal.

Two deliberate deviations from the spec's "every check is fatal" table, both
discussed beforehand:
  * allow_other: if /etc/fuse.conf is missing/unreadable or the directive is
    absent we WARN rather than die — the only authoritative test is an actual
    mount, which preflight can't do per-mount. Pass `strict_allow_other=True`
    to make an unverifiable result fatal.
  * stale mounts / pending unmounts: recovery, logged as warnings.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

from .store import VolumeStore

ALOELITE_ROOT = "/aloelite-root"
MANAGER_MNT = "/mnt"
CAP_SYS_ADMIN_BIT = 21


@dataclass
class CheckResult:
    name: str
    ok: bool
    fatal: bool
    detail: str = ""


# --- individual checks (pure; return CheckResult) --------------------------
def check_dev_fuse() -> CheckResult:
    ok = os.path.exists("/dev/fuse")
    return CheckResult(
        "/dev/fuse present",
        ok,
        True,
        "" if ok else "/dev/fuse missing — pass --device /dev/fuse",
    )


def check_cap_sys_admin() -> CheckResult:
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("CapEff:"):
                    cap = int(line.split()[1], 16)
                    ok = bool(cap & (1 << CAP_SYS_ADMIN_BIT))
                    return CheckResult(
                        "CAP_SYS_ADMIN available",
                        ok,
                        True,
                        ""
                        if ok
                        else "CAP_SYS_ADMIN not in CapEff — run --privileged "
                        "or --cap-add SYS_ADMIN",
                    )
    except OSError as e:
        return CheckResult(
            "CAP_SYS_ADMIN available",
            False,
            True,
            f"cannot read /proc/self/status: {e}",
        )
    return CheckResult(
        "CAP_SYS_ADMIN available",
        False,
        True,
        "CapEff line not found in /proc/self/status",
    )


def check_aloelite_root(root: str = ALOELITE_ROOT) -> CheckResult:
    ok = os.path.isdir(root) and os.access(root, os.W_OK)
    return CheckResult(
        f"{root} writable",
        ok,
        True,
        "" if ok else f"{root} missing or not writable — mount it -v",
    )


def _mountinfo_lines(path: str = "/proc/self/mountinfo") -> list[str]:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read().splitlines()


def check_mnt_rshared(
    mnt: str = MANAGER_MNT, *, mountinfo: list[str] | None = None
) -> CheckResult:
    """Confirm `mnt` carries shared propagation (rshared). In mountinfo the
    optional fields (between field 6 and the ' - ' separator) include a
    'shared:N' tag for shared mounts."""
    try:
        lines = mountinfo if mountinfo is not None else _mountinfo_lines()
    except OSError as e:
        return CheckResult(f"{mnt} rshared", False, True, f"cannot read mountinfo: {e}")
    for line in lines:
        # split off the optional-fields region before ' - '
        if " - " not in line:
            continue
        pre, _, _post = line.partition(" - ")
        fields = pre.split()
        if len(fields) < 5:
            continue
        mount_point = fields[4]
        if mount_point != mnt:
            continue
        optional = fields[6:]  # zero or more tags like 'shared:1' / 'master:1'
        shared = any(t.startswith("shared:") for t in optional)
        return CheckResult(
            f"{mnt} rshared",
            shared,
            True,
            "" if shared else f"{mnt} has no shared propagation — mount with :rshared",
        )
    return CheckResult(f"{mnt} rshared", False, True, f"no mountinfo entry for {mnt}")


def check_fusermount3() -> CheckResult:
    ok = shutil.which("fusermount3") is not None
    return CheckResult(
        "fusermount3 present",
        ok,
        True,
        "" if ok else "fusermount3 not on PATH — install fuse3",
    )


def check_allow_other(
    *, conf_path: str = "/etc/fuse.conf", strict: bool = False
) -> CheckResult:
    """Look for an uncommented `user_allow_other` in fuse.conf. Unverifiable
    (missing/unreadable file or absent directive) is a WARNING unless strict."""
    fatal = strict
    try:
        with open(conf_path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.split("#", 1)[0].strip()
                if line == "user_allow_other":
                    return CheckResult("allow_other permitted", True, fatal)
    except OSError:
        return CheckResult(
            "allow_other permitted",
            False,
            fatal,
            f"{conf_path} unreadable; can't confirm user_allow_other "
            "(consumer containers may not see the mount)",
        )
    return CheckResult(
        "allow_other permitted",
        False,
        fatal,
        f"user_allow_other not set in {conf_path} "
        "(consumer containers may not see the mount)",
    )


def check_volume_store(store: VolumeStore) -> CheckResult:
    """The store materializes/validates its file on construction; a successful
    list() confirms it is readable and the file is well-formed."""
    try:
        store.list()
        return CheckResult("VolumeStore readable/writable", True, True)
    except Exception as e:
        return CheckResult(
            "VolumeStore readable/writable", False, True, f"store unusable: {e}"
        )


# --- stale-mount + pending-unmount recovery (warnings) ---------------------
def _is_fuse_active(mountpoint: str) -> bool:
    """Heuristic: a live FUSE mount has a different st_dev than its parent."""
    try:
        parent = os.path.dirname(mountpoint.rstrip("/")) or "/"
        return os.stat(mountpoint).st_dev != os.stat(parent).st_dev
    except OSError:
        return False


def _lazy_unmount(mountpoint: str) -> None:
    subprocess.run(["fusermount3", "-uz", mountpoint], check=False, capture_output=True)


def recover_stale_mounts(store: VolumeStore) -> list[CheckResult]:
    """For each mounted=True record with no live FUSE session, clear the flag
    and defensively lazy-unmount. Also drain the pending-unmounts side list.
    All results are warnings."""
    results: list[CheckResult] = []
    for rec in store.list():
        if rec.mounted and rec.mountpoint and not _is_fuse_active(rec.mountpoint):
            _lazy_unmount(rec.mountpoint)
            rec.mounted = False
            rec.mountpoint = None
            store.put(rec)
            results.append(
                CheckResult(
                    f"stale mount {rec.id}", True, False, "cleared stale mounted flag"
                )
            )
    for mp in store.list_pending_unmounts():
        _lazy_unmount(mp)
        store.clear_pending_unmount(mp)
        results.append(
            CheckResult(f"pending unmount {mp}", True, False, "drained pending unmount")
        )
    return results


# --- runner ----------------------------------------------------------------
def run_preflight(
    store: VolumeStore,
    *,
    aloelite_root: str = ALOELITE_ROOT,
    mnt: str = MANAGER_MNT,
    strict_allow_other: bool = False,
    log=print,
) -> list[CheckResult]:
    """Run all checks; log each; exit(1) if any fatal check fails.
    Returns the results so the API can expose them (GET /health)."""
    # Direct-only mode ($ALOELITE_DIRECT_ONLY): the manager serves volumes over
    # the direct (browser) frontend only, so /dev/fuse, CAP_SYS_ADMIN, rshared,
    # and fusermount3 are not requirements — demote those checks to warnings.
    # A FUSE mount attempted in this mode fails at mount time with its own
    # error rather than at startup.
    direct_only = os.environ.get("ALOELITE_DIRECT_ONLY", "") not in ("", "0")
    if direct_only:
        # Browser frontend only: FUSE preconditions are not requirements, so
        # don't probe them — a FUSE mount attempted anyway fails at mount time
        # with its own error. One ok/info line documents the mode.
        fuse_checks = [
            CheckResult(
                "direct-only mode",
                True,
                False,
                "FUSE checks skipped (ALOELITE_DIRECT_ONLY)",
            )
        ]
    else:
        fuse_checks = [
            check_dev_fuse(),
            check_cap_sys_admin(),
            check_mnt_rshared(mnt),
            check_fusermount3(),
            check_allow_other(strict=strict_allow_other),
        ]
    results = fuse_checks + [
        check_aloelite_root(aloelite_root),
        check_volume_store(store),
    ]
    results.extend(recover_stale_mounts(store))

    failures = []
    for r in results:
        if r.ok:
            log(f"[preflight] OK   {r.name}" + (f" — {r.detail}" if r.detail else ""))
        elif r.fatal:
            log(f"[preflight] FAIL {r.name} — {r.detail}")
            failures.append(r)
        else:
            log(f"[preflight] WARN {r.name} — {r.detail}")

    if failures:
        log(f"[preflight] {len(failures)} fatal check(s) failed; exiting.")
        raise SystemExit(1)
    log("[preflight] all fatal checks passed.")
    return results


__all__ = [
    "CheckResult",
    "run_preflight",
    "recover_stale_mounts",
    "check_dev_fuse",
    "check_cap_sys_admin",
    "check_aloelite_root",
    "check_mnt_rshared",
    "check_fusermount3",
    "check_allow_other",
    "check_volume_store",
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
