// @ts-check
// Pyodide backend for the engine contract in api.js: boots the Python
// runtime lazily, loads the UNMODIFIED aloelite engine from a source zip,
// and drives it through bootstrap.py's single JSON entry point.
//
// The UI never sees any of this — swap this file's factory for the future
// TS/wasm or aloelite-rs backend and the viewer is unchanged.
//
// Proxy discipline: exactly two PyProxies are held (bootstrap.call and
// bootstrap.take_bytes) for the engine's lifetime. call() returns a plain
// string, take_bytes() returns a real Uint8Array built on the Python side,
// so per-operation proxies never exist and nothing needs destroy().
import { EngineError } from "./errors.js";

const ASSETS = {
  pyodideModule: "/pyodide/pyodide.mjs",
  indexURL: "/pyodide/",
  packages: ["cryptography", "pydantic", "pyyaml", "msgpack"],
  sourceZip: "/aloelite_src.zip",
  bootstrapPy: "/engine/bootstrap.py",
  compatPy: "/engine/pyodide_compat.py",
};

/**
 * @param {Partial<typeof ASSETS> & {
 *   loadPyodide?: Function,
 *   fetchBytes?: (url: string) => Promise<ArrayBuffer>,
 *   fetchText?: (url: string) => Promise<string>,
 * }} [overrides]  test seam: the Node harness injects loadPyodide + file readers
 * @returns {import("./api.js").Engine}
 */
export function createPyodideEngine(overrides = {}) {
  const cfg = { ...ASSETS, ...overrides };
  const fetchBytes =
    cfg.fetchBytes ?? (async (u) => (await fetch(u)).arrayBuffer());
  const fetchText = cfg.fetchText ?? (async (u) => (await fetch(u)).text());

  /** @type {any} */ let py = null;
  /** @type {any} */ let pyCall = null;
  /** @type {any} */ let pyTake = null;
  /** @type {Promise<void>|null} */ let booting = null;
  let workSeq = 0;

  /** @param {string} op @param {object} [args] @param {Uint8Array|null} [data] */
  const invoke = (op, args = {}, data = null) => {
    const envelope = JSON.parse(pyCall(op, JSON.stringify(args), data));
    if (envelope.err) throw new EngineError(envelope.err.code, envelope.err.message);
    return envelope.ok;
  };

  const takeBytes = () => /** @type {Uint8Array} */ (pyTake());

  async function bootOnce(onProgress = () => {}) {
    try {
      onProgress("loading Python runtime", 0.05);
      const { loadPyodide } = cfg.loadPyodide
        ? { loadPyodide: cfg.loadPyodide }
        : await import(/* @vite-ignore */ cfg.pyodideModule);
      py = await loadPyodide({ indexURL: cfg.indexURL });
      onProgress("loading packages", 0.45);
      await py.loadPackage(cfg.packages, { messageCallback: () => {} });
      onProgress("loading aloelite", 0.8);
      const zip = await fetchBytes(cfg.sourceZip);
      py.FS.mkdirTree("/repo");
      py.unpackArchive(zip, "zip", { extractDir: "/repo" });
      py.FS.mkdirTree("/app");
      py.FS.mkdirTree("/work");
      py.FS.writeFile("/app/pyodide_compat.py", await fetchText(cfg.compatPy));
      py.FS.writeFile("/app/bootstrap.py", await fetchText(cfg.bootstrapPy));
      onProgress("starting engine", 0.92);
      py.runPython('import sys; sys.path.insert(0, "/app"); import bootstrap');
      pyCall = py.runPython("bootstrap.call");
      pyTake = py.runPython("bootstrap.take_bytes");
      onProgress("ready", 1);
    } catch (e) {
      if (e instanceof EngineError) throw e;
      throw new EngineError("boot_failed", `engine runtime failed to load: ${e}`);
    }
  }

  /** @param {number} sessionId @param {{mount: number, readOnly: boolean}} m @param {string} volumeId */
  const makeMount = (sessionId, m, volumeId) =>
    /** @type {import("./api.js").EngineMount} */ ({
      readOnly: m.readOnly,
      volumeId,
      async list(path) {
        return invoke("list", { mount: m.mount, path });
      },
      async stat(path) {
        return invoke("stat", { mount: m.mount, path });
      },
      async readFile(path) {
        invoke("read_file", { mount: m.mount, path });
        return takeBytes();
      },
      async putFile(path, bytes) {
        invoke("put_file", { mount: m.mount, path }, bytes);
      },
      async mkdir(path) {
        invoke("mkdir", { mount: m.mount, path });
      },
      async remove(path, opts = {}) {
        invoke("remove", { mount: m.mount, path, recursive: !!opts.recursive });
      },
      async unmount() {
        invoke("unmount", { mount: m.mount });
      },
    });

  /** @param {string} fileName @param {{session: number, volumes: any[]}} opened */
  const makeSession = (fileName, opened) =>
    /** @type {import("./api.js").EngineSession} */ ({
      fileName,
      volumes: opened.volumes,
      async mount(volumeId, opts = {}) {
        const m = invoke("mount", {
          session: opened.session,
          volume: volumeId,
          pin: opts.pin ?? null,
          readOnly: opts.readOnly ?? true,
        });
        return makeMount(opened.session, m, volumeId);
      },
      async exportBytes() {
        invoke("export", { session: opened.session });
        return takeBytes();
      },
      async close() {
        invoke("close_session", { session: opened.session });
      },
    });

  return {
    async boot(onProgress) {
      // The retry reset lives on the chain, NOT inside bootOnce: a failure
      // thrown before bootOnce's first await would otherwise run its catch
      // synchronously BEFORE this assignment, storing a permanently-rejected
      // promise and bricking every later boot() call.
      booting ??= bootOnce(onProgress).catch((e) => {
        booting = null;
        throw e;
      });
      return booting;
    },
    async openVolumeFile(fileName, bytes) {
      await this.boot();
      const safe = fileName.replace(/[^A-Za-z0-9._-]/g, "_") || "volume.fs";
      const path = `/work/f${++workSeq}_${safe}`;
      py.FS.writeFile(path, bytes);
      try {
        return makeSession(fileName, invoke("open_file", { path }));
      } catch (e) {
        // a failed open (not a sqlite file, newer schema era, ...) must not
        // strand the upload in MEMFS — retries of big files would stack up
        try {
          py.FS.unlink(path);
        } catch {
          /* already gone */
        }
        throw e;
      }
    },
    async createScratch(volumeName, pin = null) {
      await this.boot();
      const path = `/work/scratch${++workSeq}.fs`;
      return makeSession(
        "scratch.fs",
        invoke("create_scratch", { path, name: volumeName, pin }),
      );
    },
  };
}
