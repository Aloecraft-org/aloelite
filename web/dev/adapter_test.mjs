// Adapter contract test: drives web/static/engine/pyodide-engine.js exactly
// as the browser would, but in Node, with the runtime and asset fetches
// injected through the adapter's test seam. Covers the full surface plus the
// closed-error mapping, and round-trips an exported .fs back through the
// engine to prove the download path yields a valid file.
//
// Prereqs: a pyodide runtime with wheels (fetch_runtime.sh or the PoC copy)
// and python3 for the source-zip build.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = (p) => fileURLToPath(new URL(p, import.meta.url));
const webRoot = resolve(here(".."));
const repoRoot = resolve(webRoot, "..");

const runtimeDir = [
  process.env.PYODIDE_RUNTIME_DIR,
  resolve(webRoot, ".runtime/node_modules/pyodide"),
  resolve(repoRoot, "notebook/pyodide-poc/node_modules/pyodide"),
].find((p) => p && existsSync(p));
assert.ok(runtimeDir, "no pyodide runtime found — run web/dev/fetch_runtime.sh");

// drift guard: the served compat shim must match the PoC's proven copy
const shimA = readFileSync(resolve(webRoot, "static/engine/pyodide_compat.py"));
const shimB = readFileSync(resolve(repoRoot, "notebook/pyodide-poc/pyodide_compat.py"));
assert.equal(
  createHash("sha256").update(shimA).digest("hex"),
  createHash("sha256").update(shimB).digest("hex"),
  "web/static/engine/pyodide_compat.py has drifted from notebook/pyodide-poc/pyodide_compat.py",
);

mkdirSync(resolve(webRoot, "dist"), { recursive: true });
execFileSync("python3", [resolve(webRoot, "dev/build_src_zip.py"), resolve(webRoot, "dist/aloelite_src.zip")]);

const { loadPyodide } = await import(pathToFileURL(resolve(runtimeDir, "pyodide.mjs")).href);
const { createPyodideEngine } = await import(pathToFileURL(resolve(webRoot, "static/engine/pyodide-engine.js")).href);
const { EngineError } = await import(pathToFileURL(resolve(webRoot, "static/engine/errors.js")).href);

const local = {
  "/aloelite_src.zip": resolve(webRoot, "dist/aloelite_src.zip"),
  "/engine/bootstrap.py": resolve(webRoot, "static/engine/bootstrap.py"),
  "/engine/pyodide_compat.py": resolve(webRoot, "static/engine/pyodide_compat.py"),
};
const engine = createPyodideEngine({
  loadPyodide,
  indexURL: runtimeDir + "/",
  fetchBytes: async (u) => readFileSync(local[u]).buffer.slice(0),
  fetchText: async (u) => readFileSync(local[u], "utf8"),
});

const expectCode = async (code, fn) => {
  try {
    await fn();
  } catch (e) {
    assert.ok(e instanceof EngineError, `expected EngineError, got ${e}`);
    assert.equal(e.code, code, `expected ${code}, got ${e.code}: ${e.message}`);
    return;
  }
  assert.fail(`expected EngineError ${code}, nothing thrown`);
};

let t = Date.now();
const stages = [];
await engine.boot((stage, f) => stages.push([stage, f]));
console.log(`boot ${(Date.now() - t) / 1000}s, stages: ${stages.map(([s]) => s).join(" → ")}`);
assert.ok(stages.length >= 3 && stages.at(-1)[1] === 1, "progress reached 1.0");

// --- scratch: full writable surface ----------------------------------------
const s1 = await engine.createScratch("demo", "9999");
assert.equal(s1.volumes.length, 1);
assert.equal(s1.volumes[0].encrypted, true);
assert.equal(s1.volumes[0].encMode, "convergent");

const rw = await s1.mount(s1.volumes[0].id, { pin: "9999", readOnly: false });
assert.equal(rw.readOnly, false);
await rw.mkdir("/a/b");
const payload = new TextEncoder().encode("adapter test payload ✨\n".repeat(40));
await rw.putFile("/a/b/x.txt", payload);
assert.deepEqual([...(await rw.readFile("/a/b/x.txt"))], [...payload]);

const rootRows = await rw.list("/");
assert.deepEqual(rootRows.map((r) => [r.name, r.type]), [["a", "container"]]);
const st = await rw.stat("/a/b/x.txt");
assert.equal(st.type, "entry");
assert.equal(st.size, payload.length);
assert.ok(st.createdAt > 1e12, "createdAt is epoch ms");

// closed-error taxonomy
await expectCode("not_found", () => rw.stat("/nope"));
await expectCode("not_a_container", () => rw.list("/a/b/x.txt"));
await expectCode("not_an_entry", () => rw.readFile("/a"));
await expectCode("not_empty", () => rw.remove("/a"));
await rw.remove("/a", { recursive: true });
await expectCode("not_found", () => rw.stat("/a"));
await rw.putFile("/keep.txt", payload);

// --- export -> reopen round trip --------------------------------------------
const exported = await s1.exportBytes();
assert.ok(exported.length > 10_000, `exported ${exported.length} bytes`);
await rw.unmount();
await s1.close();

const s2 = await engine.openVolumeFile("reopened.fs", exported);
assert.equal(s2.volumes[0].encrypted, true);
await expectCode("bad_key", () => s2.mount(s2.volumes[0].id, { pin: "0000" }));
await expectCode("encryption_required", () => s2.mount(s2.volumes[0].id));
const ro = await s2.mount(s2.volumes[0].id, { pin: "9999" }); // readOnly defaults true
assert.equal(ro.readOnly, true);
assert.deepEqual([...(await ro.readFile("/keep.txt"))], [...payload]);
await expectCode("read_only", () => ro.putFile("/nope.txt", payload));
await expectCode("read_only", () => ro.mkdir("/nope"));
await expectCode("read_only", () => ro.remove("/keep.txt"));
await ro.unmount();
await expectCode("mount_invalid", () => ro.list("/"));
await s2.close();

// --- the PoC's demo file, when present (created by notebook/pyodide-poc) ----
const demoFs = resolve(repoRoot, "notebook/pyodide-poc/out/pyodide-demo.fs");
if (existsSync(demoFs)) {
  const s3 = await engine.openVolumeFile("pyodide-demo.fs", new Uint8Array(readFileSync(demoFs)));
  const m3 = await s3.mount(s3.volumes[0].id, { pin: "1234" });
  const names = (await m3.list("/docs")).map((r) => r.name);
  assert.ok(names.includes("hello.txt"), `demo listing: ${names}`);
  const meta = (await m3.stat("/docs/hello.txt")).metadata;
  assert.equal(meta.author, "wasm");
  await s3.close();
  console.log("demo .fs cross-open: ok");
} else {
  console.log("demo .fs not present — skipped (run notebook/pyodide-poc first for this leg)");
}

console.log("ADAPTER TEST PASSED");
