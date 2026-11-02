// aloelite-explorer.js: a file explorer over an Aloelite handle, as one
// ES module with no dependencies. It speaks only Mount API operations
// (config/mount-api.yaml), so it works on anything `aloelite-wasm` hands a
// page: an `Fs` (`Fs.openMemory()`, `pool.open(name)`) used directly, or a
// Worker running `serve(fs, self)` reached through `workerCall`.
//
// ---------------------------------------------------------------------------
// surface
// ---------------------------------------------------------------------------
//
// Entry points
//   renderExplorer(target, handle, opts) -> Promise<Explorer>
//     target  an element id, or an Element
//     handle  an `Fs`, anything with `call(op, args)`, or a function
//             `(op, args) => result | Promise<result>` (what workerCall returns)
//     opts    see DEFAULTS
//   workerCall(endpoint) -> (op, args) => Promise
//     the page side of `serve`'s protocol, over a Worker or a MessagePort
//
// The returned Explorer: navigate(path), refresh(), open(path), destroy(),
// and the read-only properties path, mount, volume.

/** Options to renderExplorer, and what each defaults to. */
export const DEFAULTS = Object.freeze({
  // An existing mount id. When given, the explorer uses it and leaves it
  // mounted on destroy(); `volume` is ignored.
  mount: null,
  // A volume id or name to mount. Null shows a picker over list_volumes.
  volume: null,
  // Where to start, mount-relative.
  path: "/",
  // Show upload, new folder, new file, rename, delete and text editing,
  // and mount `rw` rather than `ro`.
  editable: false,
  // PIN for an encrypted volume (string or Uint8Array).
  pin: null,
  // Lifetime of the explorer's own mount, renewed every third of it while
  // the explorer lives. A page that closes without destroy() leaves a mount
  // row behind; the ttl lets it expire (ACC-3) instead of holding the
  // subtree's rw admission forever. Null for a mount that never expires.
  ttlMs: 60_000,
  // Extra `mount` arguments for the explorer's own mount, such as
  // { allow_overlap: true } or { at: "/sub" }.
  mountArgs: {},
  // Inject the default stylesheet once per document. False to bring your own.
  styles: true,
  // Called when a file is selected: (path, info) => void. Return false to
  // suppress the built-in preview.
  onOpen: null,
  // Called after any change the explorer makes: (op, path) => void.
  onChange: null,
  // Called on any failed operation: (error) => void. Default shows it in the
  // explorer's status line.
  onError: null,
  // Asks yes or no before replacing or deleting: (message) => boolean, or a
  // Promise of one. Replace the browser's dialog with an inline one here.
  confirm: (message) => globalThis.confirm(message),
  // Asks for a name: (message, defaultValue) => string | null, or a Promise
  // of one. Null or empty cancels.
  prompt: (message, value) => globalThis.prompt(message, value),
});

/** Largest file the preview reads whole. Bigger files offer download only. */
export const PREVIEW_MAX_BYTES = 4 * 1024 * 1024;

/** Largest file the text editor opens. */
export const EDIT_MAX_BYTES = 1024 * 1024;

/** Prefix on every class and CSS custom property the explorer emits. */
export const CSS_PREFIX = "alx";

/** Extensions previewed as images, and the MIME type their Blob gets. */
export const IMAGE_TYPES = {
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  gif: "image/gif",
  webp: "image/webp",
  svg: "image/svg+xml",
  avif: "image/avif",
  bmp: "image/bmp",
  ico: "image/x-icon",
};

/** Glyph per NodeType (mount-api.yaml enums.NodeType). */
export const TYPE_ICONS = {
  container: "\u{1F4C1}",
  entry: "\u{1F4C4}",
  symlink: "\u{1F517}",
  fifo: "\u{2195}",
  socket: "\u{1F50C}",
};

// Fan-out: what selecting a row does, by NodeType. Containers navigate,
// entries preview, everything else shows its NodeInfo.
const ON_SELECT = {
  container: (ex, path) => ex.navigate(path),
  entry: (ex, path) => ex.open(path),
  symlink: (ex, path) => ex.open(path),
  fifo: (ex, path) => ex.open(path),
  socket: (ex, path) => ex.open(path),
};

// Fan-out: how the preview pane renders an entry's bytes, tried in order;
// the first whose test passes renders.
const PREVIEWERS = [
  { name: "image", test: (name) => ext(name) in IMAGE_TYPES, render: previewImage },
  { name: "text", test: (_name, bytes) => decodeText(bytes) !== null, render: previewText },
  { name: "binary", test: () => true, render: previewBinary },
];

// Fan-out: the toolbar and row actions shown when `editable` is set.
const TOOLBAR_ACTIONS = [
  { label: "Upload", run: (ex) => ex._upload() },
  { label: "New folder", run: (ex) => ex._newFolder() },
  { label: "New file", run: (ex) => ex._newFile() },
];
const ROW_ACTIONS = [
  { label: "Rename", run: (ex, path) => ex._rename(path) },
  { label: "Delete", run: (ex, path, type) => ex._delete(path, type) },
];

export async function renderExplorer(target, handle, opts = {}) {
  const root = typeof target === "string" ? document.getElementById(target) : target;
  if (!root) throw new Error(`aloelite-explorer: no element ${JSON.stringify(target)}`);
  const ex = new Explorer(root, toCall(handle), { ...DEFAULTS, ...opts });
  await ex._start();
  return ex;
}

export function workerCall(endpoint) {
  let seq = 0;
  const pending = new Map();
  endpoint.addEventListener("message", ({ data }) => {
    const waiter = data && pending.get(data.id);
    if (!waiter) return;
    pending.delete(data.id);
    if ("error" in data) waiter.reject(aloeliteError(data.error));
    else waiter.resolve(data.ok);
  });
  endpoint.start?.(); // a MessagePort only delivers once started
  return (op, args) =>
    new Promise((resolve, reject) => {
      const id = `alx-${++seq}`;
      pending.set(id, { resolve, reject });
      endpoint.postMessage({ id, op, args });
    });
}

// ---------------------------------------------------------------------------
// depth: the Explorer
// ---------------------------------------------------------------------------

class Explorer {
  constructor(root, call, opts) {
    this._root = root;
    this._call = call;
    this._opts = opts;
    this._mount = opts.mount;
    this._ownsMount = false;
    this._volume = null;
    this._path = normalize(opts.path);
    this._objectUrls = [];
    this._generation = 0; // stale-response guard for navigate()
    this._destroyed = false;
    this._renewTimer = null;
  }

  get path() {
    return this._path;
  }
  get mount() {
    return this._mount;
  }
  get volume() {
    return this._volume;
  }

  async navigate(path) {
    const target = normalize(path);
    const gen = ++this._generation;
    this._setStatus("Loading…");
    try {
      const listing = await this._list(target);
      if (gen !== this._generation || this._destroyed) return;
      this._path = target;
      this._renderCrumbs();
      this._renderRows(listing);
      this._clearPreview();
      this._setStatus(`${listing.length} item${listing.length === 1 ? "" : "s"}`);
    } catch (e) {
      if (gen === this._generation) this._fail(e);
    }
  }

  refresh() {
    return this.navigate(this._path);
  }

  async open(path) {
    try {
      const info = await this._op("stat", { path });
      if (this._opts.onOpen && this._opts.onOpen(path, info) === false) return;
      await this._preview(path, info);
    } catch (e) {
      this._fail(e);
    }
  }

  async destroy() {
    if (this._destroyed) return;
    this._destroyed = true;
    this._revokeUrls();
    clearInterval(this._renewTimer);
    this._root.replaceChildren();
    this._root.classList.remove(CSS_PREFIX);
    if (this._ownsMount && this._mount) {
      const mount = this._mount;
      this._mount = null;
      await this._call("unmount", { mount });
    }
  }

  // -- startup --------------------------------------------------------------

  async _start() {
    if (this._opts.styles) injectStyles(this._root.ownerDocument);
    this._buildShell();
    try {
      if (this._mount) {
        this._volume = (await this._call("mount_info", { mount: this._mount })).volume;
      } else if (this._opts.volume) {
        await this._mountVolume(await this._resolveVolume(this._opts.volume));
      } else {
        await this._pickVolume();
        return;
      }
      await this.navigate(this._path);
    } catch (e) {
      this._fail(e);
    }
  }

  async _resolveVolume(nameOrId) {
    const byName = await this._call("resolve_volume_name", { name: nameOrId });
    if (byName) return byName;
    const all = await this._call("list_volumes", {});
    if (all.some((v) => v.id === nameOrId)) return nameOrId;
    throw aloeliteError({ code: "not_found", message: `no volume named or with id ${nameOrId}` });
  }

  async _mountVolume(volume) {
    clearInterval(this._renewTimer);
    if (this._ownsMount && this._mount) {
      await this._call("unmount", { mount: this._mount });
    }
    const { ttlMs, pin, editable, mountArgs } = this._opts;
    const args = { volume, access: editable ? "rw" : "ro", ...mountArgs };
    if (pin != null) args.pin = pin;
    if (ttlMs != null) args.ttl_ms = ttlMs;
    this._mount = await this._call("mount", args);
    this._ownsMount = true;
    this._volume = volume;
    if (ttlMs != null) {
      const mount = this._mount;
      this._renewTimer = setInterval(() => {
        this._call("renew_mount", { mount, ttl_ms: ttlMs }).catch((e) => this._fail(e));
      }, ttlMs / 3);
    }
  }

  async _pickVolume() {
    const volumes = await this._call("list_volumes", {});
    const select = this._els.volumes;
    select.replaceChildren(
      el("option", { value: "", textContent: volumes.length ? "Choose a volume…" : "No volumes" }),
      ...volumes.map((v) => el("option", { value: v.id, textContent: v.name ?? v.id })),
    );
    select.hidden = false;
    select.onchange = async () => {
      if (!select.value) return;
      try {
        await this._mountVolume(select.value);
        this._path = "/";
        await this.navigate("/");
      } catch (e) {
        this._fail(e);
      }
    };
    this._setStatus(volumes.length ? "Choose a volume" : "This store has no volumes");
    if (volumes.length === 1) {
      select.value = volumes[0].id;
      await select.onchange();
    }
  }

  // -- operations -----------------------------------------------------------

  // Every mount-scoped call goes through here, so `mount` is never forgotten.
  _op(op, args) {
    if (!this._mount) return Promise.reject(aloeliteError({ code: "usage", message: "no volume mounted" }));
    return this._call(op, { mount: this._mount, ...args });
  }

  async _list(path) {
    const entries = await this._op("list", { path });
    // Hidden same-name siblings (NODE-5) are not addressable by path.
    const shown = entries.filter((e) => e.visible);
    const infos = await Promise.all(
      shown.map((e) => this._op("stat", { path: join(path, e.name) }).catch(() => null)),
    );
    const rows = shown.map((e, i) => ({ ...e, path: join(path, e.name), info: infos[i] }));
    rows.sort((a, b) =>
      (a.type === "container") !== (b.type === "container")
        ? a.type === "container"
          ? -1
          : 1
        : a.name.localeCompare(b.name),
    );
    return rows;
  }

  async _changed(op, path) {
    this._opts.onChange?.(op, path);
    await this.refresh();
  }

  async _upload() {
    const input = el("input", { type: "file", multiple: true });
    input.onchange = async () => {
      try {
        for (const file of input.files) {
          const path = join(this._path, file.name);
          const data = new Uint8Array(await file.arrayBuffer());
          if (await this._op("exists", { path })) {
            if (!(await this._opts.confirm(`Replace ${file.name}?`))) continue;
            await this._op("write_all", { path, data });
          } else {
            await this._op("create_entry", { path, data });
          }
        }
        await this._changed("upload", this._path);
      } catch (e) {
        this._fail(e);
      }
    };
    input.click();
  }

  async _newFolder() {
    const name = await this._askName("New folder name");
    if (!name) return;
    const path = join(this._path, name);
    await this._try(() => this._op("create_container", { path }), "create_container", path);
  }

  async _newFile() {
    const name = await this._askName("New file name");
    if (!name) return;
    const path = join(this._path, name);
    await this._try(() => this._op("create_entry", { path }), "create_entry", path);
  }

  async _rename(path) {
    const name = await this._askName("Rename to", basename(path));
    if (!name || name === basename(path)) return;
    await this._try(() => this._op("rename", { path, name }), "rename", path);
  }

  async _delete(path, type) {
    const message = `Delete ${basename(path)}${type === "container" ? " and everything in it" : ""}?`;
    if (!(await this._opts.confirm(message))) return;
    const op = type === "container" ? "remove_recursive" : "remove";
    await this._try(() => this._op(op, { path }), op, path);
  }

  async _save(path, text) {
    await this._try(() => this._op("write_all", { path, data: text }), "write_all", path);
    await this.open(path);
  }

  async _askName(message, value = "") {
    const name = (await this._opts.prompt(message, value))?.trim();
    if (!name) return null;
    if (name.includes("/")) {
      this._setStatus("A name cannot contain '/'.", true);
      return null;
    }
    return name;
  }

  async _download(path) {
    try {
      const bytes = await this._op("read_all", { path });
      const a = el("a", { href: this._url(new Blob([bytes])), download: basename(path) });
      a.click();
    } catch (e) {
      this._fail(e);
    }
  }

  async _try(fn, op, path) {
    try {
      await fn();
      await this._changed(op, path);
    } catch (e) {
      this._fail(e);
    }
  }

  _fail(e) {
    if (this._destroyed) return;
    if (this._opts.onError) this._opts.onError(e);
    this._setStatus(`${e.code ? `${e.code}: ` : ""}${e.message ?? e}`, true);
  }

  // -- rendering ------------------------------------------------------------

  _buildShell() {
    const p = CSS_PREFIX;
    this._root.classList.add(p);
    this._root.replaceChildren();
    const volumes = el("select", { className: `${p}-volumes`, hidden: true, ariaLabel: "Volume" });
    const crumbs = el("nav", { className: `${p}-crumbs`, ariaLabel: "Path" });
    const tools = el("div", { className: `${p}-tools` });
    if (this._opts.editable) {
      for (const action of TOOLBAR_ACTIONS) {
        tools.append(button(action.label, () => action.run(this)));
      }
    }
    tools.append(button("Refresh", () => this.refresh()));
    const rows = el("tbody");
    const table = el("table", { className: `${p}-list` });
    table.append(
      el("thead", {}, [
        el("tr", {}, [
          el("th", { textContent: "Name" }),
          el("th", { textContent: "Size", className: `${p}-num` }),
          el("th", { textContent: "Modified" }),
          ...(this._opts.editable ? [el("th", { className: `${p}-actions` })] : []),
        ]),
      ]),
      rows,
    );
    const preview = el("section", { className: `${p}-preview`, hidden: true });
    const status = el("div", { className: `${p}-status`, role: "status" });
    this._root.append(
      el("header", { className: `${p}-bar` }, [volumes, crumbs, tools]),
      el("div", { className: `${p}-body` }, [el("div", { className: `${p}-scroll` }, [table]), preview]),
      status,
    );
    this._els = { volumes, crumbs, rows, preview, status };
  }

  _renderCrumbs() {
    const parts = this._path.split("/").filter(Boolean);
    const links = [this._crumb("/", "/")];
    parts.forEach((part, i) => {
      if (i > 0) links.push(el("span", { className: `${CSS_PREFIX}-sep`, textContent: "/" }));
      links.push(this._crumb(part, "/" + parts.slice(0, i + 1).join("/")));
    });
    this._els.crumbs.replaceChildren(...links);
  }

  _crumb(label, path) {
    return button(label, () => this.navigate(path), `${CSS_PREFIX}-crumb`);
  }

  _renderRows(listing) {
    const p = CSS_PREFIX;
    const rows = [];
    if (this._path !== "/") {
      rows.push(
        el("tr", { className: `${p}-row` }, [
          el("td", { colSpan: this._opts.editable ? 4 : 3 }, [
            button("↑ ..", () => this.navigate(parent(this._path)), `${p}-name`),
          ]),
        ]),
      );
    }
    for (const row of listing) {
      const select = () => ON_SELECT[row.type]?.(this, row.path);
      const cells = [
        el("td", {}, [button(`${TYPE_ICONS[row.type] ?? "?"} ${row.name}`, select, `${p}-name`)]),
        el("td", { className: `${p}-num`, textContent: row.info ? formatSize(row.info.size) : "" }),
        el("td", { textContent: row.info ? formatTime(row.info.modified_at) : "" }),
      ];
      if (this._opts.editable) {
        cells.push(
          el(
            "td",
            { className: `${p}-actions` },
            ROW_ACTIONS.map((a) => button(a.label, () => a.run(this, row.path, row.type))),
          ),
        );
      }
      rows.push(el("tr", { className: `${p}-row`, dataset: { type: row.type, path: row.path } }, cells));
    }
    if (!listing.length) {
      rows.push(el("tr", {}, [el("td", { colSpan: 4, className: `${p}-empty`, textContent: "Empty folder" })]));
    }
    this._els.rows.replaceChildren(...rows);
  }

  async _preview(path, info) {
    const p = CSS_PREFIX;
    this._revokeUrls();
    const pane = this._els.preview;
    const head = el("div", { className: `${p}-preview-head` }, [
      el("strong", { textContent: basename(path) }),
      el("span", { textContent: `${info.type} · ${formatSize(info.size)} · ${formatTime(info.modified_at)}` }),
    ]);
    const tools = el("div", { className: `${p}-tools` });
    const body = el("div", { className: `${p}-preview-body` });
    head.append(tools);
    tools.append(button("Close", () => this._clearPreview()));
    pane.replaceChildren(head, body);
    pane.hidden = false;
    this._root.classList.add(`${p}-previewing`);

    if (info.type !== "entry") {
      body.append(el("pre", { textContent: describe(info) }));
      return;
    }
    tools.prepend(button("Download", () => this._download(path)));
    const size = Number(info.size ?? 0);
    if (size > PREVIEW_MAX_BYTES) {
      body.append(el("p", { textContent: `Too large to preview (${formatSize(info.size)}).` }));
      return;
    }
    const bytes = await this._op("read_all", { path });
    const previewer = PREVIEWERS.find((v) => v.test(path, bytes));
    previewer.render(this, body, { path, info, bytes, tools });
  }

  _clearPreview() {
    this._revokeUrls();
    this._els.preview.hidden = true;
    this._els.preview.replaceChildren();
    this._root.classList.remove(`${CSS_PREFIX}-previewing`);
  }

  _setStatus(text, isError = false) {
    if (!this._els) return;
    this._els.status.textContent = text;
    this._els.status.classList.toggle(`${CSS_PREFIX}-error`, isError);
  }

  _url(blob) {
    const url = URL.createObjectURL(blob);
    this._objectUrls.push(url);
    return url;
  }

  _revokeUrls() {
    for (const url of this._objectUrls.splice(0)) URL.revokeObjectURL(url);
  }
}

// ---------------------------------------------------------------------------
// depth: previewers (the PREVIEWERS table above)
// ---------------------------------------------------------------------------

function previewImage(ex, body, { path, bytes }) {
  const type = IMAGE_TYPES[ext(path)];
  body.append(el("img", { src: ex._url(new Blob([bytes], { type })), alt: basename(path) }));
}

function previewText(ex, body, { path, info, bytes, tools }) {
  const text = decodeText(bytes);
  const pre = el("pre", { textContent: text });
  body.append(pre);
  if (!ex._opts.editable || Number(info.size ?? 0) > EDIT_MAX_BYTES) return;
  tools.prepend(
    button("Edit", () => {
      const area = el("textarea", { value: text, spellcheck: false, ariaLabel: `Edit ${basename(path)}` });
      body.replaceChildren(area);
      tools.replaceChildren(
        button("Save", () => ex._save(path, area.value)),
        button("Cancel", () => ex.open(path)),
      );
      area.focus();
    }),
  );
}

function previewBinary(_ex, body, { bytes }) {
  const head = Array.from(bytes.subarray(0, 256), (b) => b.toString(16).padStart(2, "0"));
  const lines = [];
  for (let i = 0; i < head.length; i += 16) {
    lines.push(`${i.toString(16).padStart(6, "0")}  ${head.slice(i, i + 16).join(" ")}`);
  }
  body.append(
    el("p", { textContent: "Binary file. First bytes:" }),
    el("pre", { textContent: lines.join("\n") + (bytes.length > 256 ? "\n…" : "") }),
  );
}

// ---------------------------------------------------------------------------
// depth: helpers
// ---------------------------------------------------------------------------

function toCall(handle) {
  let raw;
  if (typeof handle === "function") raw = handle;
  else if (handle && typeof handle.call === "function") raw = (op, args) => handle.call(op, args);
  else throw new TypeError("aloelite-explorer: handle must be an Fs, have call(op, args), or be a function");
  // An Fs throws synchronously; a Worker client rejects. Make both a rejection.
  return (op, args) => Promise.resolve().then(() => raw(op, args));
}

function aloeliteError({ code, message }) {
  const e = new Error(message);
  e.name = "AloeliteError";
  e.code = code;
  return e;
}

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "dataset") Object.assign(node.dataset, v);
    else if (k === "ariaLabel") node.setAttribute("aria-label", v);
    else node[k] = v;
  }
  for (const child of children) if (child) node.append(child);
  return node;
}

function button(label, onClick, className = "") {
  return el("button", { type: "button", textContent: label, className, onclick: onClick });
}

function normalize(path) {
  const parts = [];
  for (const part of String(path ?? "/").split("/")) {
    if (!part || part === ".") continue;
    if (part === "..") parts.pop();
    else parts.push(part);
  }
  return "/" + parts.join("/");
}

function join(dir, name) {
  return dir === "/" ? `/${name}` : `${dir}/${name}`;
}

function parent(path) {
  return normalize(path + "/..");
}

function basename(path) {
  return path.split("/").filter(Boolean).pop() ?? "/";
}

function ext(name) {
  const base = basename(name);
  const dot = base.lastIndexOf(".");
  return dot > 0 ? base.slice(dot + 1).toLowerCase() : "";
}

const utf8 = new TextDecoder("utf-8", { fatal: true });

function decodeText(bytes) {
  if (bytes.length === 0) return "";
  try {
    const text = utf8.decode(bytes);
    // NUL and most C0 controls mean binary even when the bytes are valid UTF-8.
    return /[\u0000-\u0008\u000e-\u001f]/.test(text) ? null : text;
  } catch {
    return null;
  }
}

function formatSize(size) {
  if (size == null) return "";
  let n = Number(size);
  const units = ["B", "KB", "MB", "GB", "TB"];
  let u = 0;
  while (n >= 1024 && u < units.length - 1) {
    n /= 1024;
    u++;
  }
  return u === 0 ? `${n} B` : `${n.toFixed(n < 10 ? 1 : 0)} ${units[u]}`;
}

// Timestamps are BigInt nanoseconds (aloelite-wasm README, "What crosses").
function formatTime(ns) {
  if (ns == null) return "";
  const ms = Number(BigInt(ns) / 1_000_000n);
  return new Date(ms).toLocaleString();
}

function describe(info) {
  return Object.entries(info)
    .map(([k, v]) => `${k}: ${typeof v === "object" && v !== null ? JSON.stringify(v) : String(v)}`)
    .join("\n");
}

// ---------------------------------------------------------------------------
// depth: default stylesheet. Theme by overriding the --alx-* properties on
// .alx or any ancestor.
// ---------------------------------------------------------------------------

function injectStyles(doc) {
  const id = `${CSS_PREFIX}-styles`;
  if (doc.getElementById(id)) return;
  const style = doc.createElement("style");
  style.id = id;
  style.textContent = STYLES;
  doc.head.append(style);
}

const STYLES = `
.alx {
  --alx-bg: #ffffff; --alx-fg: #1d2125; --alx-muted: #626f7a; --alx-line: #dde1e5;
  --alx-hover: #f1f4f7; --alx-accent: #2563c9; --alx-error: #b42318;
  --alx-min-height: 240px;
  --alx-font: system-ui, sans-serif; --alx-mono: ui-monospace, SFMono-Regular, Menlo, monospace;
  display: flex; flex-direction: column; min-height: var(--alx-min-height); height: 100%;
  border: 1px solid var(--alx-line); border-radius: 8px; overflow: hidden;
  background: var(--alx-bg); color: var(--alx-fg); font: 14px/1.4 var(--alx-font);
}
@media (prefers-color-scheme: dark) {
  .alx:not(.alx-light) {
    --alx-bg: #16191c; --alx-fg: #e3e6e9; --alx-muted: #9aa5af; --alx-line: #2d3338;
    --alx-hover: #20252a; --alx-accent: #7aa7f0; --alx-error: #f97066;
  }
}
.alx button {
  font: inherit; color: inherit; background: none; border: 1px solid var(--alx-line);
  border-radius: 6px; padding: 2px 10px; cursor: pointer;
}
.alx button:hover { background: var(--alx-hover); }
.alx button:focus-visible { outline: 2px solid var(--alx-accent); outline-offset: 1px; }
.alx-bar {
  display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
  padding: 8px; border-bottom: 1px solid var(--alx-line);
}
.alx-volumes { font: inherit; }
.alx-crumbs { display: flex; flex-wrap: wrap; align-items: center; gap: 2px; flex: 1; min-width: 0; }
.alx-crumbs .alx-crumb { border: none; padding: 2px 4px; color: var(--alx-accent); }
.alx-sep { color: var(--alx-muted); }
.alx-tools { display: flex; flex-wrap: wrap; gap: 6px; }
.alx-body { display: flex; flex: 1; min-height: 0; }
.alx-scroll { flex: 1; overflow: auto; min-width: 0; }
.alx-list { width: 100%; border-collapse: collapse; }
.alx-list th {
  text-align: left; font-weight: 600; color: var(--alx-muted); font-size: 12px;
  padding: 6px 8px; border-bottom: 1px solid var(--alx-line); position: sticky; top: 0;
  background: var(--alx-bg);
}
.alx-list td { padding: 2px 8px; border-bottom: 1px solid var(--alx-line); white-space: nowrap; }
.alx-row:hover { background: var(--alx-hover); }
.alx-list .alx-name { border: none; padding: 4px 0; text-align: left; max-width: 100%; }
.alx-list .alx-name:hover { background: none; color: var(--alx-accent); }
.alx-num { text-align: right; font-variant-numeric: tabular-nums; }
.alx-actions { text-align: right; }
.alx-actions button { font-size: 12px; margin-left: 4px; }
.alx-empty { color: var(--alx-muted); text-align: center; padding: 24px; }
.alx-preview {
  flex: 1; min-width: 0; display: flex; flex-direction: column;
  border-left: 1px solid var(--alx-line);
}
.alx-preview-head {
  display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
  padding: 8px; border-bottom: 1px solid var(--alx-line);
}
.alx-preview-head span { color: var(--alx-muted); font-size: 12px; flex: 1; }
.alx-preview-body { flex: 1; overflow: auto; padding: 8px; }
.alx-preview-body pre { margin: 0; font: 13px/1.45 var(--alx-mono); white-space: pre-wrap; word-break: break-word; }
.alx-preview-body img { max-width: 100%; height: auto; }
.alx-preview-body textarea {
  width: 100%; height: 100%; min-height: 200px; box-sizing: border-box; resize: vertical;
  font: 13px/1.45 var(--alx-mono); color: inherit; background: var(--alx-bg);
  border: 1px solid var(--alx-line); border-radius: 6px; padding: 6px;
}
.alx-status { padding: 4px 8px; font-size: 12px; color: var(--alx-muted); border-top: 1px solid var(--alx-line); }
.alx-status.alx-error { color: var(--alx-error); }
@media (max-width: 640px) {
  .alx-body { flex-direction: column; }
  .alx-previewing .alx-scroll { display: none; }
  .alx-preview { border-left: none; }
  .alx-list th:nth-child(3), .alx-list td:nth-child(3) { display: none; }
}
`;
