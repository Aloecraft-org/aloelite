# ./aloelite/errors.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
Error model.

Mirrors the closed `errors` enum in mount-api.yaml as a Python exception
hierarchy. Every fault the Mount API can raise is an FsError subclass carrying a
stable `code` string equal to the enum variant. Modeling these as a hierarchy
(rather than raising bare ValueErrors) is what makes the eventual FFI projection
mechanical: Python exception -> matching closed-enum variant by `.code`.

The set is CLOSED. Do not raise anything outside this hierarchy from the
function layer; if a genuinely new failure mode appears, add a variant here and
to mount-api.yaml together.
"""

from __future__ import annotations


class FsError(Exception):
    """Base for every Mount API fault. `code` matches the YAML error variant."""

    code: str = "error"

    def __init__(self, message: str | None = None, **context: object) -> None:
        self.context = context
        super().__init__(message or self.__class__.__doc__ or self.code)


class NotFound(FsError):
    """path or id does not resolve"""

    code = "not_found"


class NotAContainer(FsError):
    """operation needs a container, target is an entry"""

    code = "not_a_container"


class NotAnEntry(FsError):
    """operation needs an entry, target is a container"""

    code = "not_an_entry"


class Nameless(FsError):
    """a name was required and was empty (NODE-3)"""

    code = "nameless"


class WouldCycle(FsError):
    """reparent would place a container under its own descendant (PI-5)"""

    code = "would_cycle"


class VolumeMismatch(FsError):
    """operation would span volumes (PI-6)"""

    code = "volume_mismatch"


class NotEmpty(FsError):
    """container is non-empty; use remove_recursive (caller policy)"""

    code = "not_empty"


class MountInvalid(FsError):
    """mount is unmounted, expired, or its mount point vanished (ACC-4/5)"""

    code = "mount_invalid"


class MountPointArchived(FsError):
    """mount point node is archived; reads may still proceed (ACC-5)"""

    code = "mount_point_archived"


class LockHeld(FsError):
    """an exclusive lock is held by another mount (ACC-7)"""

    code = "lock_held"


class LockInvalid(FsError):
    """the descriptor's lock has expired or its mount ended (ACC-9)"""

    code = "lock_invalid"


class Corrupt(FsError):
    """an invariant the schema cannot enforce was found violated"""

    code = "corrupt"


class Unsupported(FsError):
    """operation not available in this build/target"""

    code = "unsupported"


class BadKey(FsError):
    """the supplied PIN/token did not unlock the volume (AEAD tag mismatch)"""

    code = "bad_key"


class EncryptionRequired(FsError):
    """an encrypted volume was mounted without a PIN, or vice versa"""

    code = "encryption_required"


# Registry: code -> class. Used by the FFI projection (and tests) to map a code
# string back to its exception type. Built by walking the hierarchy so it can
# never drift from the classes above.
def _build_registry() -> dict[str, type[FsError]]:
    seen: dict[str, type[FsError]] = {}
    stack: list[type[FsError]] = [FsError]
    while stack:
        cls = stack.pop()
        seen[cls.code] = cls
        stack.extend(cls.__subclasses__())
    return seen


BY_CODE: dict[str, type[FsError]] = _build_registry()


__all__ = [
    "FsError",
    "NotFound",
    "NotAContainer",
    "NotAnEntry",
    "Nameless",
    "WouldCycle",
    "VolumeMismatch",
    "NotEmpty",
    "MountInvalid",
    "MountPointArchived",
    "LockHeld",
    "LockInvalid",
    "Corrupt",
    "Unsupported",
    "BadKey",
    "EncryptionRequired",
    "BY_CODE",
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
