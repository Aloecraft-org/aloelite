"""Real-browser verification of the manager UI's session handling.

Flask test clients are not browsers: per-client sessions once passed every
API test while being unusable in a real browser (the cookie was not reliably
stored/sent). This drives the actual admin UI in Chromium and is the only
check that covers that gap. It also caught two bootstrap modal races that
the API tests called green. Run it after any change to admin.html or to the
manager's auth paths; CI runs it as well, in main.yml's `manager-browser`
job, which stands a manager up with an encrypted volume and then runs this
file.

    pip install playwright              # browsers: see PLAYWRIGHT_BROWSERS_PATH
    ALOELITE_ROOT=/tmp/checkroot ALOELITE_API_PORT=8437 \
        ALOELITE_AUTH=cookie aloelite-web &
    curl -s -X POST http://127.0.0.1:8437/volumes \
        -H 'Content-Type: application/json' \
        -d '{"name":"vault","encrypted":true,"pin":"s3cret"}'
    python script/browser_check.py      # CHROMIUM=/path/to/chrome to override

Exit status is 0 only if every check passes.

Scenario (requires ALOELITE_AUTH=cookie):
  A1. Browser A: unlock encrypted volume via the PIN modal -> file explorer
      works; badge is green 'open' (NOT 'open elsewhere').
  A2. Close the explorer, click Open again -> NO PIN prompt (session held).
  A3. Create a file through the editor and save -> appears in the listing.
  B1. Browser B (separate profile): badge shows 'open elsewhere'; Open ->
      PIN modal directly.
  B2. After PIN: the listing is NOT empty (shows A's file).
  B3. B creates its own file -> save succeeds (no 'unlock required').
"""

import os
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get("ALOELITE_BASE", "http://127.0.0.1:8437")
PIN = os.environ.get("ALOELITE_PIN", "s3cret")
CHROMIUM = os.environ.get("CHROMIUM")  # None => playwright's own download
failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        failures.append(name)


def open_vault(page):
    row = page.locator("div.border.rounded", has_text="vault").first
    row.get_by_role("button", name="Open").click()


def unlock(page):
    modal = page.locator("#unlockModal")
    modal.wait_for(state="visible", timeout=5000)
    modal.locator("input").first.fill(PIN)
    modal.get_by_role("button", name="Unlock & open").click()


def explorer_visible(page):
    page.locator("#expModal").wait_for(state="visible", timeout=5000)


def close_explorer(page):
    page.locator("#expModal .btn-close").click()
    page.locator("#expModal").wait_for(state="hidden", timeout=5000)


def listed_files(page):
    page.wait_for_timeout(400)
    rows = page.locator("#expModal tbody tr td:first-child span")
    return [t for t in rows.all_inner_texts() if t.strip()]


def new_file(page, name, text):
    page.locator("#expModal").get_by_role("button", name="New file").click()
    ask = page.locator("#askModal")
    ask.wait_for(state="visible", timeout=5000)
    ask.locator("input").fill(name)
    ask.get_by_role("button", name="Create").click()
    ask.wait_for(state="hidden", timeout=5000)
    ed = page.locator("#edModal")
    ed.wait_for(state="visible", timeout=5000)
    ed.locator("textarea").fill(text)
    ed.get_by_role("button", name="Save").click()
    page.wait_for_timeout(600)
    err = ed.locator(".alert-danger")
    save_err = err.inner_text() if err.is_visible() else ""
    ed.locator(".btn-close").click()
    ed.wait_for(state="hidden", timeout=5000)
    return save_err


with sync_playwright() as pw:
    browser = pw.chromium.launch(**({"executable_path": CHROMIUM} if CHROMIUM else {}))
    # ---- browser A --------------------------------------------------------
    A = browser.new_context()
    a = A.new_page()
    a.goto(f"{BASE}/admin")
    a.wait_for_selector("text=vault", timeout=8000)

    open_vault(a)
    unlock(a)  # not mounted yet: unlock path
    explorer_visible(a)
    check("A1: explorer opens after unlock", True)

    close_explorer(a)
    a.reload()
    a.wait_for_selector("text=vault", timeout=8000)
    badge = a.locator("div.border.rounded", has_text="vault").locator(".badge").first
    check("A1: badge is green 'open' for the unlocking browser",
          badge.inner_text().strip() == "open")

    open_vault(a)
    pin_visible = False
    try:
        a.locator("#unlockModal").wait_for(state="visible", timeout=1500)
        pin_visible = True
    except Exception:
        pass
    check("A2: reopen does NOT ask for the PIN again", not pin_visible)
    explorer_visible(a)

    err = new_file(a, "from-a.txt", "written by browser A")
    check("A3: editor save in A succeeds", err == "")
    close_explorer(a)

    # ---- browser B (fresh profile: no cookies, no localStorage) -----------
    B = browser.new_context()
    b = B.new_page()
    b.goto(f"{BASE}/admin")
    b.wait_for_selector("text=vault", timeout=8000)
    badge_b = b.locator("div.border.rounded", has_text="vault").locator(".badge").first
    check("B1: other browser sees grey 'open elsewhere'",
          badge_b.inner_text().strip() == "open elsewhere")

    open_vault(b)
    unlock(b)  # attach path: PIN modal directly
    explorer_visible(b)
    names = listed_files(b)
    check("B2: listing after attach is NOT empty (sees A's file)",
          any("from-a.txt" in n for n in names))

    err = new_file(b, "from-b.txt", "written by browser B")
    check("B3: editor save in B succeeds (no 'unlock required')", err == "")
    names = listed_files(b)
    check("B3: B's file appears in the listing",
          any("from-b.txt" in n for n in names))

    browser.close()

print("ALL PASS" if not failures else f"FAILURES: {failures}")
sys.exit(1 if failures else 0)
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
