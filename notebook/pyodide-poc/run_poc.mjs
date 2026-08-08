// Node harness: boot Pyodide, mount the aloelite repo read-only via NODEFS,
// mount ./out for the produced .fs file, load wheels locally, run smoke.py.
import { mkdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { loadPyodide } from "pyodide";

const here = (p) => fileURLToPath(new URL(p, import.meta.url));
const REPO = process.env.ALOELITE_REPO ?? resolve(here("../.."));
mkdirSync(here("./out"), { recursive: true });

let t = Date.now();
const py = await loadPyodide();
console.log(`[boot] pyodide ${py.version} in ${(Date.now() - t) / 1000}s`);

py.FS.mkdir("/repo");
py.FS.mount(py.FS.filesystems.NODEFS, { root: REPO }, "/repo");
py.FS.mkdir("/out");
py.FS.mount(py.FS.filesystems.NODEFS, { root: here("./out") }, "/out");
py.FS.mkdir("/poc");
py.FS.mount(py.FS.filesystems.NODEFS, { root: here(".") }, "/poc");

t = Date.now();
await py.loadPackage(["cryptography", "pydantic", "pyyaml", "msgpack"], {
  messageCallback: () => {},
});
console.log(`[deps] wheels loaded in ${(Date.now() - t) / 1000}s`);

try {
  await py.runPythonAsync(readFileSync(here("./smoke.py"), "utf8"));
  console.log("[done] smoke test PASSED");
} catch (err) {
  console.error("[done] smoke test FAILED");
  console.error(err);
  process.exit(1);
}
