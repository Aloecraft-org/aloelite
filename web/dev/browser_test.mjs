// Browser test: drives the viewer end-to-end in headless Chromium against a
// locally served dist/ — the mock UI path first (cheap), then the real
// engine: open the PoC's demo .fs, unlock with the PIN, browse, preview,
// then the scratch flow with upload + .fs export (download captured).
// CSP violations anywhere fail the test — the privacy claim is enforced by
// policy, so a runtime that needs more than 'wasm-unsafe-eval' must show up
// here, not in production.
//
// Prereqs: pyodide runtime (PoC copy or fetch_runtime.sh), python3,
// playwright-core + chromium (the PoC dir has playwright-core; chromium is
// the preinstalled /opt/pw-browsers build unless PW_CHROMIUM is set).
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = (p) => fileURLToPath(new URL(p, import.meta.url));
const webRoot = resolve(here(".."));
const repoRoot = resolve(webRoot, "..");
const PORT = process.env.PORT ?? "8873";
const BASE = `http://127.0.0.1:${PORT}`;

const pwPath = [
  process.env.PLAYWRIGHT_CORE_DIR,
  resolve(repoRoot, "notebook/pyodide-poc/node_modules/playwright-core"),
  resolve(webRoot, ".runtime/node_modules/playwright-core"),
].find((p) => p && existsSync(p));
assert.ok(pwPath, "playwright-core not found (npm install it in notebook/pyodide-poc)");
const { chromium } = await import(pathToFileURL(join(pwPath, "index.mjs")).href);

const server = spawn(resolve(webRoot, "dev/serve.sh"), [], {
  env: { ...process.env, PORT },
  stdio: "ignore",
});
const stopServer = () => { try { server.kill(); } catch { /* gone */ } };
process.on("exit", stopServer);
await new Promise((r) => setTimeout(r, 1200)); // let assembly + bind happen

const browser = await chromium.launch({
  executablePath: process.env.PW_CHROMIUM ?? "/opt/pw-browsers/chromium",
});
const violations = [];
const pageErrors = [];

async function newPage() {
  const page = await browser.newPage({ viewport: { width: 1100, height: 900 } });
  await page.addInitScript(() => {
    window.addEventListener("securitypolicyviolation", (e) => {
      console.error(`CSPVIOLATION ${e.violatedDirective} ${e.blockedURI}`);
    });
  });
  page.on("console", (m) => {
    if (m.text().startsWith("CSPVIOLATION")) violations.push(m.text());
  });
  page.on("pageerror", (e) => pageErrors.push(String(e)));
  return page;
}

// --- 1. mock UI path ---------------------------------------------------------
{
  const page = await newPage();
  await page.goto(`${BASE}/?engine=mock`, { waitUntil: "domcontentloaded" });
  await page.click('[data-testid="try"]');
  await page.waitForSelector('[data-testid="rows"] tr', { timeout: 15_000 });
  const names = await page.$$eval('[data-testid="rows"] .name', (els) => els.map((e) => e.textContent));
  assert.ok(names.some((n) => n?.includes("docs")), `mock rows: ${names}`);
  await page.click('[data-testid="export"]'); // mock export must produce a download
  await page.screenshot({ path: join(webRoot, "dev/shot_mock.png"), fullPage: true });
  await page.close();
  console.log("[1] mock UI: ok");
}

// --- 2. real engine: open the PoC demo file ---------------------------------
const demoFs = resolve(repoRoot, "notebook/pyodide-poc/out/pyodide-demo.fs");
if (existsSync(demoFs)) {
  const page = await newPage();
  await page.goto(BASE, { waitUntil: "domcontentloaded" });
  await page.setInputFiles("#fileInput", demoFs);
  await page.waitForSelector('[data-testid="pin"]', { timeout: 120_000 }); // boot + open
  await page.fill('[data-testid="pin"]', "0000");
  await page.click('[data-testid="unlock"]');
  await page.waitForSelector('[data-testid="toast"]:has-text("Wrong PIN")', { timeout: 30_000 });
  await page.fill('[data-testid="pin"]', "1234");
  await page.click('[data-testid="unlock"]');
  await page.waitForSelector('[data-testid="ro-badge"]:not(.hidden)', { timeout: 60_000 });
  await page.click('[data-testid="rows"] .name:has-text("docs")');
  await page.waitForSelector('[data-testid="rows"] .name:has-text("hello.txt")');
  await page.click('[data-testid="rows"] .name:has-text("hello.txt")');
  await page.waitForSelector('[data-testid="preview"] pre', { timeout: 30_000 });
  assert.match(await page.textContent('[data-testid="preview"] pre'), /hello from wasm/);
  await page.screenshot({ path: join(webRoot, "dev/shot_real.png"), fullPage: true });
  await page.close();
  console.log("[2] real open+unlock+browse+preview: ok");
} else {
  console.log("[2] SKIPPED (no demo .fs — run notebook/pyodide-poc first)");
}

// --- 3. real engine: scratch -> upload -> export -----------------------------
{
  const page = await newPage();
  await page.goto(BASE, { waitUntil: "domcontentloaded" });
  await page.fill("#scratchPin", "4321");
  await page.click('[data-testid="try"]');
  await page.waitForSelector("#writeTools:not(.hidden)", { timeout: 120_000 });
  const up = mkdtempSync(join(tmpdir(), "aloe-up-"));
  writeFileSync(join(up, "note.txt"), "scratch upload via browser\n");
  await page.setInputFiles("#uploadInput", join(up, "note.txt"));
  await page.waitForSelector('[data-testid="rows"] .name:has-text("note.txt")');
  const [download] = await Promise.all([
    page.waitForEvent("download", { timeout: 60_000 }),
    page.click('[data-testid="export"]'),
  ]);
  const saved = join(up, "exported.fs");
  await download.saveAs(saved);
  const head = readFileSync(saved).subarray(0, 15).toString("latin1");
  assert.equal(head, "SQLite format 3", "export is a real sqlite file");
  await page.screenshot({ path: join(webRoot, "dev/shot_scratch.png"), fullPage: true });
  await page.close();
  console.log(`[3] scratch+upload+export: ok (${readFileSync(saved).length} bytes, valid sqlite header)`);
}

await browser.close();
stopServer();

assert.deepEqual(violations, [], `CSP violations:\n${violations.join("\n")}`);
const realErrors = pageErrors.filter((e) => !/ResizeObserver/.test(e));
assert.deepEqual(realErrors, [], `page errors:\n${realErrors.join("\n")}`);
console.log("BROWSER TEST PASSED (no CSP violations, no page errors)");
