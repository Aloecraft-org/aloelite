#!/usr/bin/env bash
# scripts/precommit-py.sh
DISCLAIMER="# License: Apache-2.0 (disclaimer at bottom of file)"
LICENSE_BLOCK="# Copyright Michael Godfrey 2026 | aloecraft.org <michael@aloecraft.org>
#
# Licensed under the Apache License, Version 2.0 (the \"License\");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an \"AS IS\" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License."

for f in $(find . -name "*.py" -o -name "*.pyi"); do
    # Add path comment + disclaimer at top if missing
    if ! head -1 "$f" | grep -q "^# $f"; then
        sed -i "1s|^|# $f\n$DISCLAIMER\n|" "$f"
    fi
    # Add license block at bottom if missing
    if ! grep -q "Apache License" "$f"; then
        echo "$LICENSE_BLOCK" >> "$f"
    fi
done