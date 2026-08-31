# ./aloelite/__main__.py
# License: Apache-2.0 (disclaimer at bottom of file)
"""
`python -m aloelite` — the same entry point as the `aloelite` console script.

This exists because the console script is not always reachable. A pip install
puts `aloelite.exe` in the interpreter's `Scripts\\` directory, which on
Windows is frequently not on PATH -- so the first thing a new install does is
fail with "'aloelite' is not recognized", and the obvious next guess,
`python -m aloelite`, used to fail too:

    No module named aloelite.__main__; 'aloelite' is a package and
    cannot be directly executed

Two dead ends in a row, on a working install. `python -m aloelite` now does
what the script does, including its `fuse` / `web` / `admin` sub-tool
dispatch, so `python -m aloelite web --webdav` brings the manager up with no
PATH surgery at all.
"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
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
