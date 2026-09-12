#!/usr/bin/env python3
"""Aloelite's own changelog invariant, for technoproj-changelog.

Everything else the engine checks generalises across the org and lives in
`technoproj`: the version in four places, the tag against its entry, each
spelling against the others. This does not.

The schema era is a number the ENGINE writes into `PRAGMA user_version`. An
entry claiming an era the build does not write is the mistake worth catching
before a migration ships, because it is the one field here that decides
whether a volume someone already has still opens. It is aloelite's
break-once field the way `LUAC_FORMAT` is diluvium's, and
`doc/ALIGNMENT.md` §1 is the rule it serves: compatibility is checked by
name, never by reading digits out of a version.

Surface
-------
Entry points
  consistency(doc, ctx)   called by technoproj-changelog when this file
                          exists; `ctx` carries read(path), root and base
"""

import re


def consistency(doc, ctx):
    """The newest entry's `api_version` against what `db.py` stamps.

    -> list of problems, empty when they agree. An entry with no
    `api_version` is not checked: predating the era stamp is a real state
    (0.3.0 and 0.3.1 have none) rather than a missing field.
    """
    bad = []
    newest = doc["releases"][0]
    claimed = newest.get("api_version")
    if claimed is None:
        return bad

    found = re.search(r"^SCHEMA_ERA\s*=\s*(\d+)", ctx["read"]("aloelite/db.py"), re.M)
    if not found:
        bad.append("aloelite/db.py: no SCHEMA_ERA")
    elif int(found.group(1)) != claimed:
        bad.append(
            "db.py SCHEMA_ERA is %s but the newest entry (%s) says schema "
            "era %s" % (found.group(1), newest["version"], claimed)
        )
    return bad


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
