# ./aloelite/pin.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
Shared PIN resolution for front-ends (fuse.py, cli.py).

Precedence: --pin > --pin-file > --pin-env. Returns None when no flag was
given (unencrypted mount, or defer to an interactive prompt). Errors are
raised as PinError so each front-end reports them its own way.
"""

from __future__ import annotations

import os


class PinError(Exception):
    """A PIN source was specified but could not be read."""


def read_pin(
    pin: str | None, pin_file: str | None, pin_env: str | None
) -> bytes | None:
    """Resolve a PIN from the three standard flags, in precedence order.
    Returns None if none were given."""
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
        help="PIN (plaintext; prefer --pin-file or --pin-env)",
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
