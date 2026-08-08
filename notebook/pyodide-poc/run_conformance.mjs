// Boot Pyodide and run aloelite's own pytest suite (portable subset) in wasm.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { loadPyodide } from "pyodide";

const here = (p) => fileURLToPath(new URL(p, import.meta.url));
const REPO = process.env.ALOELITE_REPO ?? resolve(here("../.."));

const py = await loadPyodide();
py.FS.mkdir("/repo");
py.FS.mount(py.FS.filesystems.NODEFS, { root: REPO }, "/repo");
py.FS.mkdir("/poc");
py.FS.mount(py.FS.filesystems.NODEFS, { root: here(".") }, "/poc");

await py.loadPackage(["cryptography", "pydantic", "pyyaml", "msgpack", "pytest"], {
  messageCallback: () => {},
});

try {
  await py.runPythonAsync(readFileSync(here("./conformance_in_wasm.py"), "utf8"));
  console.log("[done] conformance suite in wasm PASSED");
} catch (err) {
  console.error("[done] conformance suite in wasm FAILED");
  console.error(err.message?.split("\n").slice(-15).join("\n") ?? err);
  process.exit(1);
}
