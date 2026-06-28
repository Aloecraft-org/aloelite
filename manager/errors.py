# ./manager/errors.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
manager.errors — manager-level exceptions.

These decouple the API layer from the supervisor: the supervisor raises these,
the API maps them to HTTP status codes, and neither needs to import the other's
internals. Distinct from aloelite.errors (which the supervisor catches and
translates into BadPin / EncryptionMismatch).
"""

from __future__ import annotations


class ManagerError(Exception):
    """Base for all manager errors."""


class AlreadyMounted(ManagerError):
    """Mount requested for a volume that is already mounted (-> 409)."""


class NotMounted(ManagerError):
    """Unmount/status requested for a volume that is not mounted (-> 404)."""


class MountTimeout(ManagerError):
    """FUSE mount did not become ready within the readiness window (-> 503)."""


class MountFailed(ManagerError):
    """FUSE thread died before/while becoming ready (-> 500)."""


class BadPin(ManagerError):
    """Wrong PIN for an encrypted volume (-> 400)."""


class EncryptionMismatch(ManagerError):
    """PIN supplied for a plain volume, or omitted for an encrypted one (-> 400)."""


__all__ = [
    "ManagerError",
    "AlreadyMounted",
    "NotMounted",
    "MountTimeout",
    "MountFailed",
    "BadPin",
    "EncryptionMismatch",
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
