// @ts-check
// In-memory backend implementing the api.js contract, for UI development and
// UI tests without the ~20 MB runtime. `?engine=mock` selects it. Behavior
// mirrors the real adapter's semantics: closed error codes, read-only
// enforcement, an encrypted volume that unlocks with PIN "1234".
import { EngineError } from "./errors.js";

const delay = (ms) => new Promise((r) => setTimeout(r, ms));
const enc = (s) => new TextEncoder().encode(s);
const now = () => Date.now();

const SAMPLE_SVG = `<svg xmlns="http://www.w3.org/2000/svg" width="240" height="120">
<rect width="240" height="120" rx="12" fill="#2b6cb0"/>
<text x="120" y="66" font-family="sans-serif" font-size="20" fill="white" text-anchor="middle">aloelite</text></svg>`;

function sampleTree() {
  const t = now();
  /** @type {Map<string, {type: 'container'|'entry', bytes?: Uint8Array, meta?: any, created: number, modified: number}>} */
  const nodes = new Map([
    ["/", { type: "container", created: t, modified: t }],
    ["/docs", { type: "container", created: t, modified: t }],
    ["/docs/README.md", { type: "entry", bytes: enc("# Mock volume\n\nThis tree comes from the **mock engine** (`?engine=mock`).\nNo Python runtime was loaded.\n"), created: t, modified: t }],
    ["/docs/notes.txt", { type: "entry", bytes: enc("plain text preview works\nline two\n"), created: t, modified: t, meta: { author: "mock" } }],
    ["/media", { type: "container", created: t, modified: t }],
    ["/media/logo.svg", { type: "entry", bytes: enc(SAMPLE_SVG), created: t, modified: t }],
    ["/data.bin", { type: "entry", bytes: new Uint8Array(4096).fill(7), created: t, modified: t }],
  ]);
  return nodes;
}

const parent = (p) => p.slice(0, p.lastIndexOf("/")) || "/";
const baseName = (p) => p.slice(p.lastIndexOf("/") + 1);
const norm = (p) => (p.startsWith("/") ? p : "/" + p).replace(/\/+$/, "") || "/";

/** @returns {import("./api.js").Engine} */
export function createMockEngine() {
  let seq = 0;
  let booted = false;
  // Fixture hooks so UI tests can reach paths the happy mock hides:
  //   ?bootfail        -> boot rejects (boot_failed)
  //   open "corrupt*"  -> open fails (fs_error)
  //   open "single*"   -> one unencrypted volume (exercises auto-advance)
  //   open "badmount*" -> one unencrypted volume whose mount always fails
  const search = typeof location === "undefined" ? "" : location.search;
  return {
    async boot(onProgress = () => {}) {
      if (new URLSearchParams(search).has("bootfail"))
        throw new EngineError("boot_failed", "forced by ?bootfail fixture");
      if (booted) {
        onProgress("ready", 1);
        return;
      }
      for (const [stage, f] of [["loading Python runtime", 0.3], ["loading packages", 0.6], ["loading aloelite", 0.9], ["ready", 1]]) {
        await delay(120);
        onProgress(String(stage), Number(f));
      }
      booted = true;
    },
    async openVolumeFile(fileName) {
      await this.boot();
      await delay(80);
      if (fileName.startsWith("corrupt"))
        throw new EngineError("fs_error", "file is not a database");
      if (fileName.startsWith("single"))
        return makeSession(fileName, [
          { id: `mock-${++seq}-s1`, name: "only", encrypted: false, encMode: "none", createdAt: now() },
        ]);
      if (fileName.startsWith("badmount"))
        return makeSession(fileName, [
          { id: `mock-${++seq}-bm`, name: "cursed", encrypted: false, encMode: "none", createdAt: now(), _alwaysFail: true },
        ]);
      return makeSession(fileName, [
        { id: `mock-${++seq}-a`, name: "documents", encrypted: true, encMode: "convergent", createdAt: now() },
        { id: `mock-${seq}-b`, name: "public", encrypted: false, encMode: "none", createdAt: now() },
      ]);
    },
    async createScratch(volumeName, pin = null) {
      await this.boot();
      return makeSession("scratch.fs", [
        { id: `mock-${++seq}-s`, name: volumeName, encrypted: !!pin, encMode: pin ? "convergent" : "none", createdAt: now() },
      ], pin);
    },
  };

  /** @param {string} fileName @param {import("./api.js").VolumeDesc[]} volumes @param {string|null} [scratchPin] */
  function makeSession(fileName, volumes, scratchPin) {
    const trees = new Map(volumes.map((v) => [v.id, sampleTree()]));
    let sessionClosed = false;
    const liveSession = () => {
      if (sessionClosed) throw new EngineError("mount_invalid", "session is closed");
    };
    return {
      fileName,
      volumes,
      async mount(volumeId, opts = {}) {
        liveSession();
        await delay(scratchPin !== undefined ? 60 : 450); // fake Argon2id stall
        const vol = volumes.find((v) => v.id === volumeId);
        if (!vol) throw new EngineError("not_found");
        if (vol._alwaysFail) throw new EngineError("corrupt", "forced by badmount fixture");
        const pinWanted = scratchPin !== undefined ? scratchPin : "1234";
        if (vol.encrypted && !opts.pin) throw new EngineError("encryption_required");
        if (!vol.encrypted && opts.pin) throw new EngineError("encryption_required");
        if (vol.encrypted && opts.pin !== pinWanted) throw new EngineError("bad_key");
        const nodes = trees.get(volumeId) ?? sampleTree();
        const readOnly = opts.readOnly ?? true;
        let unmounted = false;
        const live = () => {
          liveSession();
          if (unmounted) throw new EngineError("mount_invalid", "mount is closed");
        };

        const get = (path, type = null) => {
          live();
          const n = nodes.get(norm(path));
          if (!n) throw new EngineError("not_found");
          if (type === "container" && n.type !== "container") throw new EngineError("not_a_container");
          if (type === "entry" && n.type !== "entry") throw new EngineError("not_an_entry");
          return n;
        };
        const writable = () => {
          if (readOnly) throw new EngineError("read_only");
        };

        return {
          readOnly,
          volumeId,
          async list(path) {
            get(path, "container");
            const p = norm(path);
            return [...nodes.entries()]
              .filter(([k]) => k !== "/" && parent(k) === p)
              .map(([k, n]) => ({ name: baseName(k), type: n.type, node: k }))
              // mirror bootstrap.py's sort key exactly: (type != container,
              // name.lower()) in CODEPOINT order — not locale collation
              .sort((a, b) => {
                if (a.type !== b.type) return a.type === "container" ? -1 : 1;
                const [x, y] = [a.name.toLowerCase(), b.name.toLowerCase()];
                return x < y ? -1 : x > y ? 1 : 0;
              });
          },
          async stat(path) {
            const n = get(path);
            return {
              id: norm(path), type: n.type, name: baseName(norm(path)) || "/",
              size: n.type === "entry" ? (n.bytes?.length ?? 0) : null,
              createdAt: n.created, modifiedAt: n.modified, metadata: n.meta ?? {},
            };
          },
          async readFile(path) {
            return new Uint8Array(get(path, "entry").bytes ?? new Uint8Array());
          },
          async putFile(path, bytes) {
            writable();
            get(parent(path), "container");
            const t = now();
            const prev = nodes.get(norm(path));
            nodes.set(norm(path), { type: "entry", bytes: new Uint8Array(bytes), created: prev?.created ?? t, modified: t });
          },
          async mkdir(path) {
            live();
            writable();
            const parts = norm(path).split("/").filter(Boolean);
            let p = "";
            for (const part of parts) {
              p += "/" + part;
              if (!nodes.has(p)) nodes.set(p, { type: "container", created: now(), modified: now() });
            }
          },
          async remove(path, opts = {}) {
            writable();
            const p = norm(path);
            get(p);
            const children = [...nodes.keys()].filter((k) => k !== p && k.startsWith(p + "/"));
            if (children.length && !opts.recursive) throw new EngineError("not_empty");
            children.forEach((k) => nodes.delete(k));
            nodes.delete(p);
          },
          async unmount() {
            unmounted = true;
          },
        };
      },
      async exportBytes() {
        liveSession();
        return enc(`mock export of ${fileName} @ ${new Date().toISOString()}\n`);
      },
      async close() {
        sessionClosed = true;
      },
    };
  }
}
