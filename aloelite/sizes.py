# ./aloelite/sizes.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
Values as a command line spells them, shared by every front-end that takes
one: byte sizes (`--chunk-size`) and version counts (`--retention`,
`--retention-on-delete`) on `aloelite` and `aloelite-fuse`. The Rust ports are
`aloelite_core::types::parse_size` and `parse_keep`; each pair accepts exactly
the same strings.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# surface
# ---------------------------------------------------------------------------
# Entry points: parse_size, parse_keep. Configurable: SUFFIXES, KEEP_ALL.

# Binary multiples only, case-insensitive. `64K` is 65,536 bytes: a chunk size
# is a power-of-two decision in practice, and a decimal `K` would make the
# obvious spelling of 64 KiB land on 64,000.
SUFFIXES: dict[str, int] = {"": 1, "K": 1 << 10, "M": 1 << 20}

# How "keep every version" is spelled where a count is expected.
KEEP_ALL = "all"


def parse_size(text: str) -> int:
    """`65536`, `64K` or `1M` as a positive byte count. ValueError otherwise."""
    digits = text.rstrip("KkMm")
    suffix = text[len(digits) :].upper()
    if not digits.isascii() or not digits.isdigit() or suffix not in SUFFIXES:
        raise ValueError(
            f"{text!r} is not a size: use bytes, or a K or M suffix (64K, 1M)"
        )
    size = int(digits) * SUFFIXES[suffix]
    if size <= 0:
        raise ValueError("a size must be greater than zero")
    return size


def parse_keep(text: str) -> int:
    """`all` as -1, or a version count (`0`, `1`, `25`). ValueError otherwise.
    Whether 0 is meaningful is the caller's business: it is for
    --retention-on-delete and not for --retention."""
    if text.lower() == KEEP_ALL:
        return -1
    if not text.isascii() or not text.isdigit():
        raise ValueError(f"{text!r} is not a count: use a number, or {KEEP_ALL!r}")
    return int(text)


__all__ = ["parse_size", "parse_keep", "SUFFIXES", "KEEP_ALL"]
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
