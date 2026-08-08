// Drive the browser PoC in headless Chromium and screenshot the result.
import { chromium } from "playwright-core";

const URL_ = process.env.POC_URL ?? "http://127.0.0.1:8871/index.html";
const browser = await chromium.launch({ executablePath: "/opt/pw-browsers/chromium" });
const page = await browser.newPage({ viewport: { width: 1000, height: 1180 } });
page.on("console", (m) => m.type() === "error" && console.error("[page]", m.text()));
await page.goto(URL_, { waitUntil: "domcontentloaded" });
await page.waitForSelector("#resultCard:not([hidden])", { timeout: 180_000 });
await page.waitForTimeout(300);
const status = await page.textContent("#status");
console.log("status:", status.trim());
await page.screenshot({ path: "browser_poc.png", fullPage: true });
console.log("screenshot: browser_poc.png");
await browser.close();
if (!/PASSED/.test(status)) process.exit(1);
