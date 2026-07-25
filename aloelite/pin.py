# ./aloelite/pin.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
Shared PIN resolution for front-ends (fuse.py, cli.py).

Precedence: --pin > --pin-file > --pin-env. A bare --pin (no SECRET)
prompts interactively via getpass (terminal required). Returns None when
no flag was given (unencrypted mount, or defer to a front-end's own
prompt). Errors are raised as PinError so each front-end reports them
its own way.
"""

from __future__ import annotations

import getpass
import os
import sys

# Sentinel: --pin was given with no SECRET => prompt interactively.
_PROMPT = object()


class PinError(Exception):
    """A PIN source was specified but could not be read."""


def read_pin(
    pin: object | None,
    pin_file: str | None,
    pin_env: str | None,
    *,
    confirm: bool = False,
) -> bytes | None:
    """Resolve a PIN from the three standard flags, in precedence order.
    A bare --pin (no SECRET) prompts via getpass on a terminal. Returns
    None if no flag was given."""
    if pin is _PROMPT:
        # Prompt on the controlling terminal (/dev/tty), like ssh/sudo do,
        # so a piped stdin (data flowing through the command) still allows
        # an interactive prompt. Only a truly detached process (no ctty:
        # cron, CI, a daemon) has no way to ask.
        try:
            tty_in = open("/dev/tty", "r")
        except OSError:
            raise PinError(
                "--pin with no value requires a controlling terminal to "
                "prompt; use --pin-file or --pin-env in non-interactive "
                "contexts"
            ) from None
        try:
            first = getpass.getpass("PIN: ", stream=tty_in)
        finally:
            tty_in.close()
        if confirm:
            try:
                tty_in = open("/dev/tty", "r")
            except OSError:
                raise PinError("PINs did not match (no terminal to confirm)") from None
            try:
                if getpass.getpass("Confirm PIN: ", stream=tty_in) != first:
                    raise PinError("PINs did not match")
            finally:
                tty_in.close()
        return first.encode()
    if pin is not None:
        return pin.encode()
    if pin_file is not None:
        p = os.path.expanduser(pin_file)
        try:
            return open(p, "rb").read().rstrip(b"\n")
        except OSError as e:
            raise PinError(f"cannot read --pin-file {p!r}: {e}") from None
    if pin_env is not None:
        val = os.environ.get(pin_env)
        if val is None:
            raise PinError(f"environment variable {pin_env!r} is not set")
        return val.encode()
    return None


def add_pin_arguments(parser) -> None:
    """Attach the standard --pin/--pin-file/--pin-env group to an argparse
    parser (same flags and semantics across all front-ends)."""
    grp = parser.add_argument_group("encryption")
    grp.add_argument(
        "--pin",
        metavar="SECRET",
        nargs="?",
        const=_PROMPT,
        help="PIN; with no SECRET, prompt interactively (put bare --pin "
        "last on the line). Plaintext; prefer --pin-file or --pin-env "
        "for scripts.",
    )
    grp.add_argument(
        "--pin-file", metavar="PATH", help="file whose contents are the PIN"
    )
    grp.add_argument(
        "--pin-env", metavar="VAR", help="environment variable holding the PIN"
    )


__all__ = ["read_pin", "add_pin_arguments", "PinError"]
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
