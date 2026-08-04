# ./aloelite/_sqlite.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
The ONE place aloelite chooses its sqlite binding.

Prefers pysqlite3 (the `bundled-sqlite` extra: a statically linked modern
sqlite, independent of whatever the host ships) and falls back to the
stdlib module, whose library is capability-checked at Db open.

Everything in aloelite and manager that touches sqlite must import it from
here -- `from aloelite._sqlite import sqlite3` -- and never `import sqlite3`
directly. This is correctness, not style: pysqlite3's exception classes are
DISTINCT from the stdlib's (pysqlite3.OperationalError is not
sqlite3.OperationalError), so a module that imports the stdlib while the
engine runs on pysqlite3 has except-clauses that silently never match.
"""

try:
    import pysqlite3 as sqlite3  # type: ignore[import-not-found]
except ImportError:
    import sqlite3  # type: ignore[no-redef]

__all__ = ["sqlite3"]
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
