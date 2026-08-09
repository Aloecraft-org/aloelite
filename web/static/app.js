// @ts-check
// aloelite viewer — UI controller. Talks ONLY to the engine contract in
// engine/api.js; no backend types leak in here. Views are sections toggled by
// showView(); state is one flat object.
import { createEngine } from "./engine/api.js";
import { EngineError, isEngineError } from "./engine/errors.js";

// Size guards, calibrated by the memory spike (web/HANDOFF.md, "Memory
// spike"). Measured on Pyodide 314: read_all costs ~2.9x the file in wasm
// heap; 1.2 GiB read_all works under the 4 GiB compiled max, 1.4 GiB raises
// a clean MemoryError; a constrained tab (iOS Safari) budgets ~1.5-2 GB.
// Streaming reads (not yet exposed here — HANDOFF next-step 2) are
// flat-memory and faster, and will lift READ_MAX when they land.
const FS_WARN_BYTES = 256 * 1024 * 1024; // confirm before opening
const FS_HARD_BYTES = 1024 * 1024 * 1024; // refuse to open
const READ_MAX_BYTES = 256 * 1024 * 1024; // refuse read/download of one file
const PREVIEW_TEXT_MAX = 2 * 1024 * 1024;
const PREVIEW_IMAGE_MAX = 32 * 1024 * 1024;
const PREVIEW_MEDIA_MAX = 64 * 1024 * 1024;
const LAZY_SIZE_ROWS = 500; // fetch per-row sizes for at most this many entries

const IMAGE_EXT = /\.(png|jpe?g|gif|webp|svg|avif|bmp|ico)$/i;
const AUDIO_EXT = /\.(mp3|ogg|oga|m4a|flac|wav|opus)$/i;
const VIDEO_EXT = /\.(mp4|webm|mov|m4v)$/i;

/** @type {{engine: import("./engine/api.js").Engine|null, session: import("./engine/api.js").EngineSession|null, mount: import("./engine/api.js").EngineMount|null, cwd: string, isScratch: boolean, volumeName: string, pendingVolume: import("./engine/api.js").VolumeDesc|null}} */
const state = {
  engine: null,
  session: null,
  mount: null,
  cwd: "/",
  isScratch: false,
  volumeName: "",
  pendingVolume: null,
};

const $ = (sel) => /** @type {HTMLElement} */ (document.querySelector(sel));
const el = (tag, attrs = {}, text = "") => {
  const n = document.createElement(tag);
  Object.assign(n, attrs);
  if (text) n.textContent = text;
  return n;
};
const fmtBytes = (n) => {
  if (n == null) return "";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  for (; n >= 1024 && i < u.length - 1; i++) n /= 1024;
  return `${n < 10 && i > 0 ? n.toFixed(1) : Math.round(n)} ${u[i]}`;
};
const joinPath = (dir, name) => (dir === "/" ? `/${name}` : `${dir}/${name}`);

// give the browser one paint before a long synchronous engine call
// (Argon2id unlock, big reads) so status text is visible during the stall
const paintThen = () => new Promise((r) => setTimeout(r, 30));

let toastTimer = 0;
let toastGen = 0;
function toast(msg, kind = "error", ms = 4200) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = kind === "info" ? "info" : "";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), ms);
  t.classList.remove("hidden");
  return ++toastGen; // token: lets a busy-toast owner hide only ITS toast
}
function hideToast(token) {
  if (token === toastGen) {
    clearTimeout(toastTimer);
    $("#toast").classList.add("hidden");
  }
}

function showView(name) {
  for (const v of ["landing", "boot", "volumes", "browser"])
    $(`#view-${v}`).classList.toggle("hidden", v !== name);
}

function errText(e) {
  if (e instanceof EngineError) return e.message;
  return String(e?.message ?? e);
}

// --- boot -------------------------------------------------------------------
async function ensureEngine() {
  state.engine ??= await createEngine();
  showView("boot");
  await state.engine.boot((stage, f) => {
    $("#bootStage").textContent = stage;
    $("#bootBar").style.width = `${Math.round(f * 100)}%`;
    $("#bootBar").parentElement?.setAttribute("aria-valuenow", String(Math.round(f * 100)));
  });
  return state.engine;
}

// --- open-a-file flow -------------------------------------------------------
async function openFile(/** @type {File} */ file) {
  // clear immediately so re-picking the SAME file after a failure refires
  // the change event (the standard retry gesture)
  /** @type {HTMLInputElement} */ ($("#fileInput")).value = "";
  if (file.size > FS_HARD_BYTES) {
    toast(`${file.name} is ${fmtBytes(file.size)} — beyond what the in-browser engine can hold (limit ${fmtBytes(FS_HARD_BYTES)}). Use desktop aloelite for this one.`);
    return;
  }
  if (file.size > FS_WARN_BYTES && !confirm(`${file.name} is ${fmtBytes(file.size)}. Large volumes can exhaust browser memory. Open anyway?`))
    return;
  try {
    const engine = await ensureEngine();
    $("#bootTitle").textContent = `Opening ${file.name}…`;
    await paintThen();
    const bytes = new Uint8Array(await file.arrayBuffer());
    state.session = await engine.openVolumeFile(file.name, bytes);
    state.isScratch = false;
    renderVolumePicker();
  } catch (e) {
    showView("landing");
    toast(`Could not open ${file.name}: ${errText(e)}`);
  }
}

function renderVolumePicker(autoAdvance = true) {
  const s = state.session;
  if (!s) return;
  showView("volumes");
  $("#volumesTitle").textContent = `Volumes in ${s.fileName}`;
  $("#pinCard").classList.add("hidden");
  const list = $("#volList");
  list.textContent = "";
  if (s.volumes.length === 0) {
    list.append(el("p", { className: "empty" }, "no volumes in this file"));
    return;
  }
  for (const v of s.volumes) {
    const row = el("div", { className: "volrow" });
    const left = el("div");
    left.append(el("div", {}, v.name || v.id));
    left.append(
      el("div", { className: "meta" }, v.encrypted ? `encrypted (${v.encMode})` : "not encrypted"),
    );
    const btn = el("button", {}, v.encrypted ? "Unlock" : "Open");
    btn.dataset.testid = "open-volume";
    btn.onclick = () => pickVolume(v);
    row.append(left, btn);
    list.append(row);
  }
  // Exactly one volume: skip the picker click — but ONLY on the first
  // render. Re-renders from a mount failure must not auto-advance, or a
  // volume that always fails (corrupt, wrong file) loops forever.
  if (autoAdvance && s.volumes.length === 1) pickVolume(s.volumes[0]);
}

function pickVolume(/** @type {import("./engine/api.js").VolumeDesc} */ v) {
  state.pendingVolume = v;
  if (!v.encrypted) {
    void mountVolume(v, null);
    return;
  }
  $("#pinVolName").textContent = v.name || v.id;
  $("#pinCard").classList.remove("hidden");
  const input = /** @type {HTMLInputElement} */ ($("#pinInput"));
  input.value = "";
  input.focus();
}

async function mountVolume(v, /** @type {string|null} */ pin) {
  const session = state.session;
  if (!session) return; // user backed out while an attempt was in flight
  let busy = 0;
  try {
    // a failed root listing can strand a live mount; never stack a second one
    if (state.mount) {
      try {
        await state.mount.unmount();
      } catch {
        /* already dead */
      }
      state.mount = null;
    }
    busy = toast(v.encrypted ? "Unlocking volume… (key stretching takes a moment)" : "Opening volume…", "info", 120000);
    await paintThen();
    state.mount = await session.mount(v.id, {
      pin: pin ?? undefined,
      readOnly: !state.isScratch,
    });
    // the secret has done its job — don't leave it sitting in the DOM
    /** @type {HTMLInputElement} */ ($("#pinInput")).value = "";
    /** @type {HTMLInputElement} */ ($("#scratchPin")).value = "";
    state.volumeName = v.name || v.id;
    hideToast(busy);
    const badge = $("#volBadge");
    badge.textContent = "";
    badge.append(el("span", {}, state.volumeName));
    badge.title = v.encrypted ? "encrypted" : "";
    if (v.encrypted) {
      badge.append(el("span", { className: "icon", ariaHidden: "true" }, " 🔒"));
      badge.append(el("span", { className: "sr-only" }, " (encrypted)"));
    }
    $("#roBadge").classList.toggle("hidden", !state.mount.readOnly);
    $("#writeTools").classList.toggle("hidden", state.mount.readOnly);
    await browse("/");
  } catch (e) {
    if (e instanceof EngineError && e.code === "bad_key") {
      toast("Wrong PIN — that key does not unlock this volume.");
      /** @type {HTMLInputElement} */ ($("#pinInput")).value = "";
      $("#pinInput").focus();
      return;
    }
    toast(`Could not open volume: ${errText(e)}`);
    renderVolumePicker(false);
  }
}

// --- scratch flow -----------------------------------------------------------
async function startScratch() {
  const pin = /** @type {HTMLInputElement} */ ($("#scratchPin")).value || null;
  try {
    const engine = await ensureEngine();
    $("#bootTitle").textContent = "Creating a scratch volume…";
    await paintThen();
    state.session = await engine.createScratch("scratch", pin);
    state.isScratch = true;
    const v = state.session.volumes[0];
    await mountVolume(v, pin);
  } catch (e) {
    showView("landing");
    toast(`Could not create scratch volume: ${errText(e)}`);
  }
}

// --- browsing ---------------------------------------------------------------
async function browse(/** @type {string} */ path) {
  const m = state.mount;
  if (!m) return;
  try {
    const entries = await m.list(path);
    state.cwd = path;
    showView("browser");
    hidePreview();
    renderCrumbs(path);
    const rows = $("#rows");
    rows.textContent = "";
    $("#emptyNote").classList.toggle("hidden", entries.length > 0);
    const sizeCells = [];
    for (const entry of entries) {
      const tr = el("tr");
      const name = el("td", { className: "name" });
      const nameBtn = el("button", { className: "rowlink" });
      nameBtn.append(el("span", { className: "icon", ariaHidden: "true" }, entry.type === "container" ? "📁" : "📄"));
      nameBtn.append(el("span", {}, entry.name));
      nameBtn.onclick = () =>
        entry.type === "container"
          ? browse(joinPath(path, entry.name))
          : preview(joinPath(path, entry.name));
      name.append(nameBtn);
      const size = el("td", { className: "size" });
      if (entry.type === "entry") sizeCells.push([size, joinPath(path, entry.name)]);
      const actions = el("td", { className: "actions" });
      if (entry.type === "entry") {
        const dl = el("button", { className: "secondary" }, "download");
        dl.onclick = () => download(joinPath(path, entry.name), entry.name);
        actions.append(dl);
      }
      if (!m.readOnly) {
        const rm = el("button", { className: "danger" }, "delete");
        rm.onclick = async () => {
          if (!confirm(`Delete ${entry.name}${entry.type === "container" ? " and everything in it" : ""}?`)) return;
          try {
            await m.remove(joinPath(path, entry.name), { recursive: entry.type === "container" });
            await browse(path);
          } catch (e) {
            toast(errText(e));
          }
        };
        actions.append(rm);
      }
      tr.append(name, size, actions);
      rows.append(tr);
    }
    // sizes arrive lazily so a big directory paints immediately; the scan is
    // fire-and-forget so navigation/mutations never wait on 500 stats
    void (async () => {
      for (const [cell, p] of sizeCells.slice(0, LAZY_SIZE_ROWS)) {
        try {
          cell.textContent = fmtBytes((await m.stat(p)).size);
        } catch (e) {
          if (isEngineError(e, "mount_invalid")) break; // mount gone: stop
          // one bad entry (deleted meanwhile, corrupt node) skips only itself
        }
      }
    })();
  } catch (e) {
    toast(`Could not list ${path}: ${errText(e)}`);
  }
}

function renderCrumbs(/** @type {string} */ path) {
  const c = $("#crumbs");
  c.textContent = "";
  const rootBtn = el("button", { className: "linklike" }, state.volumeName || "/");
  rootBtn.onclick = () => browse("/");
  c.append(rootBtn);
  let acc = "";
  for (const seg of path.split("/").filter(Boolean)) {
    acc += `/${seg}`;
    const target = acc;
    c.append(el("span", { className: "sep" }, "/"));
    const b = el("button", { className: "linklike" }, seg);
    b.onclick = () => browse(target);
    c.append(b);
  }
}

// --- preview / download -----------------------------------------------------
let previewUrl = "";
function hidePreview() {
  $("#previewCard").classList.add("hidden");
  if (previewUrl) URL.revokeObjectURL(previewUrl), (previewUrl = "");
}

async function preview(/** @type {string} */ path) {
  const m = state.mount;
  if (!m) return;
  try {
    const st = await m.stat(path);
    const size = st.size ?? 0;
    $("#previewName").textContent = st.name;
    const meta = $("#previewMeta");
    meta.textContent = "";
    meta.append(el("span", { className: "kv" }, fmtBytes(size)));
    meta.append(el("span", { className: "kv" }, new Date(st.modifiedAt || st.createdAt).toLocaleString()));
    for (const [k, v] of Object.entries(st.metadata || {}))
      meta.append(el("span", { className: "kv" }, `${k}: ${v}`));
    const body = $("#previewBody");
    body.textContent = "";
    hidePreviewUrlOnly(); // a text/binary preview must release the previous media blob too
    hideBody: {
      if (size > READ_MAX_BYTES) {
        body.append(el("p", { className: "empty" }, `too large to read in-browser (${fmtBytes(size)})`));
        break hideBody;
      }
      const isImg = IMAGE_EXT.test(st.name);
      const isAudio = AUDIO_EXT.test(st.name);
      const isVideo = VIDEO_EXT.test(st.name);
      const mediaCap = isImg ? PREVIEW_IMAGE_MAX : PREVIEW_MEDIA_MAX;
      if ((isImg || isAudio || isVideo) && size <= mediaCap) {
        const busy = toast("Reading file…", "info", 120000);
        await paintThen();
        const bytes = await m.readFile(path);
        hideToast(busy);
        previewUrl = URL.createObjectURL(new Blob([bytes]));
        if (isImg) body.append(el("img", { src: previewUrl, alt: st.name }));
        else if (isAudio) body.append(el("audio", { src: previewUrl, controls: true }));
        else body.append(el("video", { src: previewUrl, controls: true }));
      } else if (size <= PREVIEW_TEXT_MAX) {
        const bytes = await m.readFile(path);
        const text = tryDecode(bytes);
        if (text !== null) body.append(el("pre", {}, text));
        else binaryCard(body, path, st, "binary file");
      } else {
        binaryCard(body, path, st, "no preview at this size");
      }
    }
    $("#previewCard").classList.remove("hidden");
    $("#previewCard").scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (e) {
    toast(`Could not read ${path}: ${errText(e)}`);
  }
}

function hidePreviewUrlOnly() {
  if (previewUrl) URL.revokeObjectURL(previewUrl), (previewUrl = "");
}

function binaryCard(body, path, st, label) {
  const p = el("p", { className: "empty" }, `${label} — `);
  const b = el("button", {}, `download ${fmtBytes(st.size ?? 0)}`);
  b.onclick = () => download(path, st.name);
  p.append(b);
  body.append(p);
}

function tryDecode(/** @type {Uint8Array} */ bytes) {
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    return null;
  }
}

async function download(/** @type {string} */ path, /** @type {string} */ name) {
  const m = state.mount;
  if (!m) return;
  try {
    const st = await m.stat(path);
    if ((st.size ?? 0) > READ_MAX_BYTES) {
      toast(`${name} is ${fmtBytes(st.size ?? 0)} — beyond the in-browser read limit.`);
      return;
    }
    const busy = toast("Reading file…", "info", 120000);
    await paintThen();
    const bytes = await m.readFile(path);
    hideToast(busy);
    saveBlob(bytes, name);
  } catch (e) {
    toast(`Download failed: ${errText(e)}`);
  }
}

function saveBlob(/** @type {Uint8Array} */ bytes, /** @type {string} */ name) {
  const url = URL.createObjectURL(new Blob([bytes]));
  const a = el("a", { href: url, download: name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

// --- scratch write tools ----------------------------------------------------
async function uploadInto(/** @type {FileList} */ files) {
  const m = state.mount;
  if (!m) return;
  for (const f of files) {
    try {
      await m.putFile(joinPath(state.cwd, f.name), new Uint8Array(await f.arrayBuffer()));
    } catch (e) {
      toast(`Could not add ${f.name}: ${errText(e)}`);
    }
  }
  await browse(state.cwd);
}

// --- wiring -----------------------------------------------------------------
function wire() {
  const fileInput = /** @type {HTMLInputElement} */ ($("#fileInput"));
  $("#pickBtn").onclick = () => fileInput.click();
  fileInput.onchange = () => fileInput.files?.[0] && openFile(fileInput.files[0]);
  const drop = $("#drop");
  for (const ev of ["dragenter", "dragover"])
    drop.addEventListener(ev, (e) => (e.preventDefault(), drop.classList.add("armed")));
  for (const ev of ["dragleave", "drop"])
    drop.addEventListener(ev, (e) => (e.preventDefault(), drop.classList.remove("armed")));
  drop.addEventListener("drop", (e) => {
    const f = /** @type {DragEvent} */ (e).dataTransfer?.files?.[0];
    if (f) openFile(f);
  });

  $("#tryBtn").onclick = startScratch;

  $("#pinGo").onclick = () => {
    const pin = /** @type {HTMLInputElement} */ ($("#pinInput")).value;
    if (state.pendingVolume) void mountVolume(state.pendingVolume, pin || null);
  };
  $("#pinInput").addEventListener("keydown", (e) => e.key === "Enter" && $("#pinGo").click());
  $("#pinCancel").onclick = () => renderVolumePicker(false);
  $("#volBack").onclick = resetToLanding;
  $("#closeBtn").onclick = resetToLanding;

  $("#mkdirBtn").onclick = async () => {
    const name = prompt("Folder name:");
    if (!name || !state.mount) return;
    try {
      await state.mount.mkdir(joinPath(state.cwd, name));
      await browse(state.cwd);
    } catch (e) {
      toast(errText(e));
    }
  };
  const uploadInput = /** @type {HTMLInputElement} */ ($("#uploadInput"));
  $("#uploadBtn").onclick = () => uploadInput.click();
  uploadInput.onchange = () => uploadInput.files && uploadInto(uploadInput.files);
  $("#exportBtn").onclick = async () => {
    if (!state.session) return;
    try {
      const busy = toast("Packing the .fs snapshot…", "info", 120000);
      await paintThen();
      const bytes = await state.session.exportBytes();
      hideToast(busy);
      saveBlob(bytes, state.isScratch ? "scratch.fs" : state.session.fileName);
      toast("Downloaded — that file opens in desktop aloelite as-is.", "info");
    } catch (e) {
      toast(`Export failed: ${errText(e)}`);
    }
  };
}

async function resetToLanding() {
  hidePreview();
  try {
    await state.mount?.unmount();
    await state.session?.close();
  } catch {
    /* closing is best-effort */
  }
  state.session = null;
  state.mount = null;
  state.cwd = "/";
  state.isScratch = false;
  /** @type {HTMLInputElement} */ ($("#fileInput")).value = "";
  showView("landing");
}

wire();
