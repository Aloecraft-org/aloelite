"""Real-browser verification of the manager's file explorer.

`browser_check.py` covers the session/auth scenario. This covers what the user
actually does once inside a volume — list, sort, filter, edit, upload, rename,
delete — plus the two UI invariants that are easy to re-break silently:

  * NO UNCAUGHT PAGE ERRORS. A thrown Alpine expression does not blank the page;
    it just quietly stops that subtree from updating, so the UI looks fine while
    a directive no longer runs. Two of these shipped unnoticed.
  * x-show ACTUALLY HIDES. Bootstrap's d-* utilities are `display:...!important`,
    which beats the inline display:none that x-show sets — an element carrying
    both is permanently visible. That shipped as an empty "Size:/Modified:" row
    on every volume card. Anything x-show controls must take its layout from a
    plain class, or use x-if instead.

It also drives the confirm/ask modals, which stack over an already-open
explorer or editor — the async-transition race that cost a session before.

    pip install playwright              # browsers: see PLAYWRIGHT_BROWSERS_PATH
    ALOELITE_ROOT=/tmp/checkroot ALOELITE_API_PORT=8437 aloelite-web &
    python script/ui_check.py           # CHROMIUM=/path/to/chrome to override

Fixtures (a plain volume with a folder of numbered files) are created through
the API on first run and reused after. Exit status is 0 only if every check
passes.
"""

import os
import sys
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright

BASE = os.environ.get("ALOELITE_BASE", "http://127.0.0.1:8437")
CHROMIUM = os.environ.get("CHROMIUM")
VOL_NAME = os.environ.get("ALOELITE_VOL", "uicheck")
FOLDER = "numbered"
N_FILES = 12

failures: list[str] = []
page_errors: list[str] = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        failures.append(name)


# ---------------------------------------------------------------------------
# Fixtures, over the API (same shape browser_check.py asks the caller to curl)
# ---------------------------------------------------------------------------
def _api(method, path, data=None, ctype="application/json"):
    req = urllib.request.Request(BASE + path, method=method)
    body = None
    if data is not None:
        req.add_header("Content-Type", ctype)
        body = data if isinstance(data, bytes) else data.encode()
    req.add_header("X-Aloelite", "1")
    try:
        with urllib.request.urlopen(req, body) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _quote(p):
    return urllib.parse.quote(p)


def ensure_fixtures():
    import json

    _, raw = _api("GET", "/filesystems")
    fss = json.loads(raw or b"[]")
    vol = None
    for f in fss:
        for v in f.get("volumes", []):
            if v["name"] == VOL_NAME:
                vol = v
    if vol is None:
        st, raw = _api("POST", "/volumes",
                       json.dumps({"name": VOL_NAME, "encrypted": False}))
        if st not in (200, 201):
            print("could not create fixture volume:", raw[:200])
            sys.exit(2)
        _, raw = _api("GET", "/filesystems")
        for f in json.loads(raw):
            for v in f.get("volumes", []):
                if v["name"] == VOL_NAME:
                    vol = v
    vid = vol["id"]
    _api("POST", f"/volumes/{vid}/mount", json.dumps({"mode": "direct"}))
    _api("POST", f"/volumes/{vid}/files/mkdir?path={_quote('/' + FOLDER)}")

    # numbered files: the names are the natural-sort case (item-2 < item-10)
    for i in range(1, N_FILES + 1):
        name = f"item-{i}.txt"
        payload = b"x" * (10 + i)  # distinct sizes, so sorting by size is real
        boundary = "----uicheck"
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{name}\"\r\nContent-Type: text/plain\r\n\r\n"
        ).encode() + payload + f"\r\n--{boundary}--\r\n".encode()
        _api("POST", f"/volumes/{vid}/files/upload?path={_quote('/' + FOLDER)}",
             body, f"multipart/form-data; boundary={boundary}")
    return vid


# ---------------------------------------------------------------------------
def names(page):
    """Visible entry names in the listing (the cell also holds an emoji)."""
    return page.evaluate("""() => [...document.querySelectorAll('#expModal tbody tr')]
        .filter(tr => tr.offsetParent !== null)
        .map(tr => { const s = tr.querySelector('td span'); return s ? s.innerText.trim() : ''; })
        .filter(Boolean)""")


def sizes(page):
    return page.evaluate("""() => [...document.querySelectorAll('#expModal tbody tr')]
        .filter(tr => tr.offsetParent !== null)
        .map(tr => tr.children[1] && tr.children[1].innerText.trim())
        .filter(Boolean).map(s => parseInt(s))""")


def confirm_button(page, label):
    return page.locator("#confirmModal .modal-footer").get_by_role("button", name=label)


def answer_ask(page, value):
    m = page.locator("#askModal")
    m.wait_for(state="visible", timeout=5000)
    page.wait_for_timeout(350)
    m.locator("input").first.fill(value)
    m.locator("button.btn-primary").click()
    page.wait_for_timeout(1100)


def run(page):
    page.goto(f"{BASE}/admin")
    page.wait_for_timeout(1500)

    # -- x-show vs bootstrap's !important -----------------------------------
    check("no stray stat row before Stat is clicked",
          not page.locator("div.stat-row").first.is_visible())
    card = page.locator("div.border.rounded", has_text=VOL_NAME).first
    card.locator("button[data-bs-toggle=dropdown]").click()
    page.wait_for_timeout(400)
    page.get_by_role("link", name="Stat").first.click()
    page.wait_for_timeout(1200)
    stat = card.locator("div.stat-row").first  # this card's, not the first on the page
    check("stat row appears once Stat has run", stat.is_visible())
    check("stat row carries real values",
          len(stat.inner_text().strip()) > len("Size: Modified:"))

    # -- explorer ------------------------------------------------------------
    card.get_by_role("button", name="Open").click()
    page.wait_for_timeout(1600)
    check("listing is not empty", len(names(page)) > 0)
    page.get_by_role("link", name=FOLDER).first.click()
    page.wait_for_timeout(1300)

    listed = names(page)
    check(f"folder lists all {N_FILES} files", len(listed) == N_FILES)
    check("natural sort: item-2 before item-10",
          listed.index("item-2.txt") < listed.index("item-10.txt"))

    # -- sorting -------------------------------------------------------------
    page.get_by_role("columnheader", name="Size").click()
    page.wait_for_timeout(500)
    asc = sizes(page)
    page.get_by_role("columnheader", name="Size").click()
    page.wait_for_timeout(500)
    desc = sizes(page)
    check("Size header sorts ascending", asc == sorted(asc) and len(asc) == N_FILES)
    check("Size header toggles to descending",
          desc == sorted(desc, reverse=True) and asc != desc)
    page.get_by_role("columnheader", name="Name").click()
    page.wait_for_timeout(500)

    # -- filter --------------------------------------------------------------
    page.locator("#expFilter").fill("item-1.")
    page.wait_for_timeout(600)
    check("filter narrows the listing", names(page) == ["item-1.txt"])
    check("summary reports filtered vs total",
          f"(of {N_FILES})" in page.locator("#expModal").inner_text())
    page.locator("#expFilter").fill("no-such-thing")
    page.wait_for_timeout(500)
    check("empty filter result explains itself",
          "Nothing matches" in page.locator("#expModal tbody").inner_text())
    page.locator("#expFilter").fill("")
    page.wait_for_timeout(500)

    # -- confirm modal, stacked over the open explorer -----------------------
    row = page.get_by_role("row", name="item-3.txt")
    row.locator("button[data-bs-toggle=dropdown]").click()
    page.wait_for_timeout(400)
    page.get_by_role("link", name="Delete").first.click()
    cm = page.locator("#confirmModal")
    cm.wait_for(state="visible", timeout=5000)
    page.wait_for_timeout(500)
    check("confirm modal paints above the explorer",
          cm.locator(".modal-content").first.is_visible()
          and "cannot be undone" in cm.inner_text())
    confirm_button(page, "Cancel").click()
    page.wait_for_timeout(1000)
    check("cancelling leaves the file alone", "item-3.txt" in names(page))
    check("explorer still usable after cancelling",
          page.locator("#expModal").is_visible() and len(names(page)) == N_FILES)

    row = page.get_by_role("row", name="item-3.txt")
    row.locator("button[data-bs-toggle=dropdown]").click()
    page.wait_for_timeout(400)
    page.get_by_role("link", name="Delete").first.click()
    cm.wait_for(state="visible", timeout=5000)
    page.wait_for_timeout(500)
    confirm_button(page, "Delete").click()
    page.wait_for_timeout(1600)
    check("confirming deletes", "item-3.txt" not in names(page))

    # -- create / edit / round-trip ------------------------------------------
    page.get_by_role("button", name="New file").click()
    answer_ask(page, "written-by-ui-check.txt")
    ta = page.locator("#edModal textarea")
    ta.wait_for(state="visible", timeout=5000)
    ta.fill("content from ui_check")
    page.locator("#edModal").get_by_role("button", name="Save").click()
    page.wait_for_timeout(1500)
    page.locator("#edModal").get_by_role("button", name="Close editor").click()
    page.wait_for_timeout(1000)
    check("editor-created file appears", "written-by-ui-check.txt" in names(page))

    page.get_by_role("row", name="written-by-ui-check.txt") \
        .get_by_role("button", name="Edit").click()
    page.wait_for_timeout(1300)
    check("editor round-trips content",
          page.locator("#edModal textarea").input_value() == "content from ui_check")

    # -- dirty-close guard ---------------------------------------------------
    page.locator("#edModal textarea").fill("edited but not saved")
    page.locator("#edModal").get_by_role("button", name="Close editor").click()
    cm.wait_for(state="visible", timeout=5000)
    page.wait_for_timeout(500)
    check("dirty editor asks before discarding", "unsaved edits" in cm.inner_text())
    confirm_button(page, "Keep editing").click()
    page.wait_for_timeout(900)
    check("'Keep editing' keeps the editor open",
          page.locator("#edModal textarea").is_visible())
    page.locator("#edModal").get_by_role("button", name="Close editor").click()
    cm.wait_for(state="visible", timeout=5000)
    page.wait_for_timeout(400)
    confirm_button(page, "Discard").click()
    page.wait_for_timeout(1200)
    check("'Discard' returns to the explorer",
          not page.locator("#edModal textarea").is_visible()
          and page.locator("#expModal").is_visible())

    # -- rename, then clean up ----------------------------------------------
    page.get_by_role("row", name="written-by-ui-check.txt") \
        .locator("button[data-bs-toggle=dropdown]").click()
    page.wait_for_timeout(400)
    page.get_by_role("link", name="Rename…").first.click()
    answer_ask(page, "renamed-by-ui-check.txt")
    check("rename takes effect", "renamed-by-ui-check.txt" in names(page))

    page.get_by_role("row", name="renamed-by-ui-check.txt") \
        .locator("button[data-bs-toggle=dropdown]").click()
    page.wait_for_timeout(400)
    page.get_by_role("link", name="Delete").first.click()
    cm.wait_for(state="visible", timeout=5000)
    page.wait_for_timeout(500)
    confirm_button(page, "Delete").click()
    page.wait_for_timeout(1400)
    check("cleanup removed the scratch file",
          "renamed-by-ui-check.txt" not in names(page))


def run_phone(page):
    """The columns fold away below md; the data must not go with them."""
    page.goto(f"{BASE}/admin")
    page.wait_for_timeout(1500)
    page.locator("div.border.rounded", has_text=VOL_NAME).first \
        .get_by_role("button", name="Open").click()
    page.wait_for_timeout(1600)
    page.get_by_role("link", name=FOLDER).first.click()
    page.wait_for_timeout(1300)
    check("phone: Size column is hidden",
          not page.locator("#expModal thead th", has_text="Size").first.is_visible())
    check("phone: size/date still shown under the name",
          page.locator("#expModal .exp-sub").first.is_visible())
    # every row's action menu must be reachable inside the viewport
    w = page.viewport_size["width"]
    boxes = page.evaluate("""() => [...document.querySelectorAll(
        "#expModal tbody tr button[data-bs-toggle='dropdown']")]
        .map(b => b.getBoundingClientRect().right)""")
    check("phone: every row's action menu is on-screen",
          bool(boxes) and max(boxes) <= w)


with sync_playwright() as p:
    launch = {"executable_path": CHROMIUM} if CHROMIUM else {}
    vid = ensure_fixtures()
    browser = p.chromium.launch(**launch)
    for label, vp, fn in (("desktop", {"width": 1280, "height": 900}, run),
                          ("phone", {"width": 390, "height": 844}, run_phone)):
        print(f"-- {label}")
        ctx = browser.new_context(viewport=vp)
        pg = ctx.new_page()
        pg.on("pageerror", lambda e, L=label: page_errors.append(f"{L}: {e}"))
        try:
            fn(pg)
        except Exception as exc:  # a locator blowing up is itself a failure
            print("EXCEPTION:", str(exc)[:300])
            failures.append(f"{label}: exception")
        ctx.close()
    browser.close()

check("no uncaught page errors", not page_errors)
for e in page_errors[:8]:
    print("   ", str(e)[:160])

print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
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
