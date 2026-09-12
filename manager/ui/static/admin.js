// ./manager/static/admin.js
// License: Apache-2.0
//
// The Alpine component behind templates/admin.html. Extracted from an
// inline <script> in that template: it carried no Jinja, so it moves out
// verbatim and the template stays markup. Plain script, no build step --
// see the vendoring note in admin.html's <head>.

function app() {
  return {
    apiBase: '',   // empty = same origin; set to 'http://localhost:8080' for cross-origin dev
    fss: [],       // filesystems, each with .volumes
    log: [],
    loading: false,
    apiOk: false,

    showCreate: false,
    newName: '', newEnc: false, newPin: '', newPin2: '', newFsId: '', newShowTarget: false,
    createErr: '', creating: false,

    mountTarget: null,
    mountPin: '', mountPath: '', mountErr: '', mounting: false,

    unlockTarget: null, unlockPin: '', unlockErr: '', unlocking: false,

    delTarget: null,

    expVol: null, expPath: '/', expEntries: [], expErr: '', expBusy: false,
    expDropHover: false,
    // listing view state: filter text, sort column, sort direction (1 | -1)
    expQuery: '', expSort: 'name', expDir: 1,
    expUploads: [],
    toasts: [],
    pvName: '', pvUrl: '', pvKind: '', pvDoc: '', pvErr: '',
    preflightWarnings: [],
    _autoAttached: false,
    askTitle: '', askValue: '', askBtn: 'OK', askHint: '', _askResolve: null,
    cfTitle: '', cfBody: '', cfYes: 'OK', cfNo: 'Cancel', cfDanger: false, _cfResolve: null,

    // editor: path is the file's FULL path; dir is where a save uploads to
    edName: '', edDir: '/', edText: '', edLoaded: '', edBusy: false, edErr: '',
    // editor view: 'edit' | 'split' | 'preview' (markdown only), and the
    // Prism language id for the open file ('' = highlighting off)
    edView: 'edit', edLang: '',
    // paste bin: newest-first entries of the volume's /pastes folder
    pbText: '', pbEntries: [], pbBusy: false, pbErr: '',
    // sketch pad
    skDir: '/', skBusy: false, skErr: '', skBg: 'grid', skSize: 3,
    skColor: '#212529',
    skColors: ['#212529', '#0d6efd', '#dc3545', '#198754', '#fd7e14', '#6f42c1'],
    _pad: null, _padRatio: 1,

    _modal(id) { return bootstrap.Modal.getOrCreateInstance(document.getElementById(id)); },

    // Per-volume session token, held EXPLICITLY (localStorage + header)
    // instead of relying on browser cookie policy, which proved
    // unpredictable (ENCRYPTION.md: the client holds only T).
    _tok(vid) { try { return localStorage.getItem('aloe_t_' + vid); } catch { return null; } },
    _setTok(vid, t) { try { localStorage.setItem('aloe_t_' + vid, t); } catch {} },
    _clearTok(vid) { try { localStorage.removeItem('aloe_t_' + vid); } catch {} },

    // Is the volume usable by THIS client as-is? Plain: mounted is enough.
    // Encrypted: we must hold its session token (server 'attached' covers
    // cookie-jar clients; a stale local token surfaces as 401 -> PIN modal).
    // Styled replacement for window.prompt(): resolves the entered string,
    // or null on cancel/dismiss. Calls are SERIALIZED through a promise
    // chain -- a second ask() while the modal is mid-hide used to overwrite
    // the pending resolver, losing the first caller's flow entirely.
    ask(title, initial = '', btn = 'OK', hint = '') {
      const run = () => new Promise(res => {
        this.askTitle = title; this.askValue = initial; this.askBtn = btn;
        this.askHint = hint;
        this._askResolve = res;
        this._modal('askModal').show();
      });
      this._askChain = (this._askChain || Promise.resolve()).then(run, run);
      return this._askChain;
    },
    // Resolution happens in the hidden.bs.modal listener (init), AFTER the
    // modal has fully hidden -- resolving mid-transition let callers open
    // the next modal against bootstrap's hide animation, which swallowed it.
    askOk() {
      this._askOut = this.askValue.trim() || null;
      this._modal('askModal').hide();
    },
    askCancel() {
      this._askOut = null;
      this._modal('askModal').hide();
    },

    // Styled replacement for window.confirm(), resolving true/false. Shares
    // ask()'s promise chain deliberately: a confirm raised while an ask is
    // mid-hide must QUEUE, not stack -- stacking is what silently ate
    // operations before. Shadowing the global name is intentional, so a stray
    // bare confirm() left in an Alpine expression lands here, not on the OS
    // dialog. Inside these methods it must always be called as this.confirm().
    confirm(title, body = '', yes = 'OK', danger = false, no = 'Cancel') {
      const run = () => new Promise(res => {
        this.cfTitle = title; this.cfBody = body;
        this.cfYes = yes; this.cfNo = no; this.cfDanger = danger;
        this._cfResolve = res;
        this._modal('confirmModal').show();
      });
      this._askChain = (this._askChain || Promise.resolve()).then(run, run);
      return this._askChain;
    },
    cfOk() { this._cfOut = true; this._modal('confirmModal').hide(); },
    cfCancel() { this._cfOut = false; this._modal('confirmModal').hide(); },

    attachedFor(v) {
      if (!v.mounted) return false;
      if (v.frontend === 'fuse' || !v.encrypted) return true;
      return v.attached === true || !!this._tok(v.id);
    },

    async init() {
      for (const [id, sel] of [['askModal', '#askModal input'],
                               ['unlockModal', '#unlockModal input']]) {
        document.getElementById(id).addEventListener('shown.bs.modal', () => {
          const i = document.querySelector(sel);
          if (i) { i.focus(); if (i.select) i.select(); }
        });
      }
      document.getElementById('askModal').addEventListener('hidden.bs.modal', () => {
        const r = this._askResolve; this._askResolve = null;
        const v = this._askOut ?? null; this._askOut = null;
        if (r) r(v);  // Esc/backdrop dismissal never set _askOut -> null
      });
      // Same handoff for confirm: resolve only once fully hidden, so the
      // caller's next modal does not open against bootstrap's hide animation.
      document.getElementById('confirmModal').addEventListener('hidden.bs.modal', () => {
        const r = this._cfResolve; this._cfResolve = null;
        const v = this._cfOut ?? false; this._cfOut = null;
        if (r) r(v);  // Esc/backdrop dismissal reads as "no"
      });
      // Preview blobs were never revoked; closing by Esc or backdrop also left
      // the last one wired to the <img>/<iframe>. One listener covers every
      // close path.
      document.getElementById('pvModal').addEventListener('hidden.bs.modal', () => {
        if (this.pvUrl.startsWith('blob:')) URL.revokeObjectURL(this.pvUrl);
        // pvDoc holds a whole rendered document, and a media blob can be tens
        // of MB -- clearing both is what actually lets the memory go.
        this.pvUrl = ''; this.pvKind = ''; this.pvDoc = ''; this.pvErr = '';
      });
      // The sketch canvas can only be measured once the modal has finished
      // animating in; see skOpen.
      document.getElementById('skModal').addEventListener('shown.bs.modal',
        () => this._skInit());
      await this.refresh();
      try {
        const h = await this.api('GET', '/health');
        if (h.ok) this.preflightWarnings = h.data.warnings || [];
      } catch {}
      // reopen showCreate watcher so Alpine show/hide still works alongside BS modals
      this.$watch('showCreate', v => v ? this._modal('createModal').show() : this._modal('createModal').hide());
    },

    addLog(msg) {
      const t = new Date().toLocaleTimeString();
      this.log.unshift(`[${t}]  ${msg}`);
      if (this.log.length > 60) this.log.pop();
    },

    toast(msg, ok = true) {
      const t = { id: Date.now() + Math.random(), msg, ok };
      this.toasts.push(t);
      setTimeout(() => { this.toasts = this.toasts.filter(x => x.id !== t.id); }, 3500);
    },

    async api(method, path, body) {
      const opts = { method, headers: { 'Content-Type': 'application/json', 'X-Aloelite': '1' } };
      const tv = path.match(/^\/volumes\/([^\/?]+)/);
      if (tv) { const t = this._tok(tv[1]); if (t) opts.headers['X-Aloelite-Token'] = t; }
      if (body !== undefined) opts.body = JSON.stringify(body);
      const r = await fetch(this.apiBase + path, opts);
      const ct = r.headers.get('content-type') || '';
      const data = ct.includes('application/json') ? await r.json() : null;
      return { ok: r.ok, status: r.status, data };
    },

    _vol(id) {
      for (const f of this.fss) for (const v of f.volumes) if (v.id === id) return v;
      return null;
    },

    async refresh() {
      this.loading = true;
      try {
        const r = await this.api('GET', '/filesystems');
        this.apiOk = r.ok;
        if (r.ok) {
          const prev = {};
          for (const f of this.fss) for (const v of f.volumes) prev[v.id] = v;
          this.fss = (r.data || []).map(f => ({
            ...f,
            volumes: (f.volumes || []).map(v => ({
              ...v,
              _stat: prev[v.id]?._stat ?? null,
              _mounts: prev[v.id]?._mounts ?? null,
              _log:  prev[v.id]?._log  ?? null,
              _busy: false,
            })),
          }));
        }
      } catch { this.apiOk = false; }
      this.loading = false;
    },

    async doCreate() {
      if (!this.newName.trim()) { this.createErr = 'Name is required.'; return; }
      if (this.newEnc && !this.newPin) { this.createErr = 'PIN is required for encrypted volumes.'; return; }
      if (this.newEnc && this.newPin !== this.newPin2) { this.createErr = 'PINs do not match.'; return; }
      this.creating = true; this.createErr = '';
      try {
        const body = { name: this.newName.trim(), encrypted: this.newEnc };
        if (this.newEnc) body.pin = this.newPin;
        if (this.newFsId) body.fs_id = this.newFsId;
        const r = await this.api('POST', '/volumes', body);
        if (!r.ok) { this.createErr = r.data?.error || 'Failed to create volume.'; this.creating = false; return; }
        const vid = r.data.id;
        // one action: create -> unlock (direct) -> open the explorer
        const m = await this.api('POST', `/volumes/${vid}/mount`,
          this.newEnc ? { mode: 'direct', pin: this.newPin } : { mode: 'direct' });
        this.addLog(`Created "${this.newName.trim()}"`);
        this._modal('createModal').hide();
        this.newName = ''; this.newEnc = false; this.newPin = ''; this.newPin2 = '';
        this.newFsId = ''; this.newShowTarget = false;
        await this.refresh();
        if (m.ok) {
          const v = this._vol(vid);
          if (v) this.openExplorer(v);
        } else {
          this.addLog(`Unlock after create failed: ${m.data?.error}`);
        }
      } catch { this.createErr = 'Could not reach API.'; }
      this.creating = false;
    },

    // -- Open / Lock (the simple path) -------------------------------------
    async openVol(v) {
      if (v.mounted) {
        if (!this.attachedFor(v)) {
          // "open elsewhere": this browser has no session -- the way in is
          // the PIN, straight to the modal, no explorer flash
          this.unlockTarget = v; this.unlockPin = ''; this.unlockErr = '';
          this._modal('unlockModal').show();
          return;
        }
        this.openExplorer(v); return;
      }
      if (v.encrypted) {
        this.unlockTarget = v; this.unlockPin = ''; this.unlockErr = '';
        this._modal('unlockModal').show();
        return;
      }
      v._busy = true;
      const r = await this.api('POST', `/volumes/${v.id}/mount`, { mode: 'direct' });
      v._busy = false;
      if (r.ok) { await this.refresh(); const nv = this._vol(v.id); if (nv) this.openExplorer(nv); }
      else this.addLog(`Open "${v.name}" failed: ${r.data?.error}`);
    },

    async doUnlock() {
      const v = this.unlockTarget;
      if (!v) return;
      this.unlocking = true; this.unlockErr = '';
      const r = await this.api('POST', `/volumes/${v.id}/mount`,
        { mode: 'direct', pin: this.unlockPin });
      this.unlocking = false;
      if (r.ok) {
        if (r.data?.token) this._setTok(v.id, r.data.token);
        this._modal('unlockModal').hide();
        this.unlockPin = '';
        await this.refresh();
        const nv = this._vol(v.id);
        if (nv) this.openExplorer(nv);
      } else {
        this.unlockErr = r.data?.error || 'Unlock failed.';
      }
    },

    async lockVol(v) {
      v._busy = true;
      const r = await this.api('DELETE', `/volumes/${v.id}/mount`);
      v._busy = false;
      if (r.ok) { this._clearTok(v.id); this.addLog(`Locked "${v.name}"`); await this.refresh(); }
      else this.addLog(`Lock "${v.name}" failed: ${r.data?.error}`);
    },

    // -- filesystem actions -------------------------------------------------
    addVolume(f) { this.newFsId = f.id; this.showCreate = true; },

    async renameFs(f) {
      const name = await this.ask('Rename file', f.display_name, 'Rename');
      if (!name || name === f.display_name) return;
      const r = await this.api('PATCH', `/filesystems/${f.id}`, { display_name: name });
      if (!r.ok) this.addLog(`Rename failed: ${r.data?.error}`);
      await this.refresh();
    },

    downloadFs(f) {
      this.addLog(`Downloading "${f.display_name}"…`);
      const a = document.createElement('a');
      a.href = `${this.apiBase}/filesystems/${f.id}/export`;
      a.download = f.display_name;
      a.click();
    },

    async importFs(ev) {
      const file = ev.target.files[0];
      ev.target.value = '';
      if (!file) return;
      await this._importFile(file);
    },

    async _importFile(file) {
      const fd = new FormData();
      fd.append('file', file);
      try {
        const r = await fetch(`${this.apiBase}/filesystems/import`, { method: 'POST', body: fd, headers: { 'X-Aloelite': '1' } });
        const d = await r.json().catch(() => null);
        if (r.ok) this.addLog(`Imported "${d.display_name}" (${d.volumes.length} volume(s))`);
        else this.addLog(`Import failed: ${d?.error || r.status}`);
      } catch { this.addLog('Could not reach API.'); }
      await this.refresh();
    },

    mountVol(v) {
      this.mountTarget = v; this.mountPin = ''; this.mountPath = ''; this.mountErr = '';
      this._modal('mountModal').show();
    },

    async doMount() {
      if (!this.mountTarget) return;
      this.mounting = true; this.mountErr = '';
      await this._doMountRequest(this.mountTarget, this.mountPin || null, this.mountPath || null);
      this.mounting = false;
    },

    async _doMountRequest(v, pin, mountPath) {
      v._busy = true;
      try {
        const body = {};
        if (pin) body.pin = pin;
        if (mountPath) body.mount_name = mountPath;
        const r = await this.api('POST', `/volumes/${v.id}/mount`, body);
        if (r.ok) {
          this.addLog(`Mounted "${v.name}" → ${r.data.mountpoint}`);
          this._modal('mountModal').hide();
          this.mountPin = ''; this.mountPath = ''; this.mountErr = '';
          await this.refresh();
        } else {
          const msg = r.data?.error || 'Mount failed.';
          if (this.mountTarget?.id === v.id) this.mountErr = msg;
          else this.addLog(`Error mounting "${v.name}": ${msg}`);
        }
      } catch {
        const msg = 'Could not reach API.';
        if (this.mountTarget?.id === v.id) this.mountErr = msg;
        else this.addLog(msg);
      }
      v._busy = false;
    },

    confirmDelete(v) {
      this.delTarget = v;
      this._modal('delModal').show();
    },

    async doDelete() {
      if (!this.delTarget) return;
      const v = this.delTarget;
      this._modal('delModal').hide();
      try {
        const r = await this.api('DELETE', `/volumes/${v.id}`);
        if (r.ok) { this.addLog(`Deleted "${v.name}"`); await this.refresh(); }
        else { this.addLog(`Error deleting "${v.name}": ${r.data?.error}`); }
      } catch { this.addLog('Could not reach API.'); }
    },

    async doCheckpoint(v) {
      v._busy = true;
      try {
        const r = await this.api('POST', `/volumes/${v.id}/checkpoint`);
        if (r.ok) {
          const d = r.data;
          v._log = `Checkpoint: ${d.wal_frames_checkpointed} frames written, ${d.wal_frames_remaining} remaining.`;
          this.addLog(`Checkpoint "${v.name}": ${d.wal_frames_checkpointed} frames, ${d.wal_frames_remaining} remaining`);
        } else {
          v._log = `Checkpoint error: ${r.data?.error}`;
        }
      } catch { v._log = 'Could not reach API.'; }
      v._busy = false;
    },

    async doStat(v) {
      try {
        const r = await this.api('GET', `/volumes/${v.id}/stat`);
        if (r.ok) { v._stat = r.data; }
        else { this.addLog(`Stat error for "${v.name}": ${r.data?.error}`); }
      } catch { this.addLog('Could not reach API.'); }
    },

    async doMounts(v) {
      try {
        const r = await this.api('GET', `/volumes/${v.id}/mounts`);
        if (r.ok) { v._mounts = r.data; }
        else { this.addLog(`Mounts error for "${v.name}": ${r.data?.error}`); }
      } catch { this.addLog('Could not reach API.'); }
    },

    // -- file explorer ------------------------------------------------------
    openExplorer(v) {
      this.expVol = v; this.expPath = '/'; this.expEntries = []; this.expErr = '';
      this.expQuery = '';
      this._autoAttached = false;  // one auto-attach attempt per open
      this._modal('expModal').show();
      this.loadFiles();
    },

    // Cookie mode, encrypted volume: unlocked server-side, but THIS browser
    // has no session -- attaching means proving the PIN. Close the explorer
    // FULLY before raising the PIN modal (bootstrap hide is async; showing
    // during the transition stacks an empty explorer over the PIN field).
    // Plain volumes never 401, so no silent-attach path is needed.
    attachVol(v) {
      this.unlockTarget = v; this.unlockPin = ''; this.unlockErr = '';
      const el = document.getElementById('expModal');
      el.addEventListener(
        'hidden.bs.modal',
        () => this._modal('unlockModal').show(),
        { once: true },
      );
      this._modal('expModal').hide();
    },

    expJoin(name) {
      return (this.expPath === '/' ? '' : this.expPath) + '/' + name;
    },

    expCrumbs() {
      const segs = this.expPath.split('/').filter(Boolean);
      const out = [{ name: this.expVol?.name || '/', path: '/' }];
      let p = '';
      for (const s of segs) { p += '/' + s; out.push({ name: s, path: p }); }
      return out;
    },

    navTo(p) { this.expPath = p || '/'; this.expQuery = ''; this.loadFiles(); },

    // -- listing view: filter + sort applied over the loaded folder ----------
    expSortBy(k) {
      if (this.expSort === k) this.expDir = -this.expDir;
      else { this.expSort = k; this.expDir = 1; }
    },
    expCaret(k) { return this.expSort === k ? (this.expDir > 0 ? ' ▴' : ' ▾') : ''; },

    expRows() {
      const q = this.expQuery.trim().toLowerCase();
      const rows = q ? this.expEntries.filter(e => e.name.toLowerCase().includes(q))
                     : this.expEntries.slice();
      const k = this.expSort, d = this.expDir;
      rows.sort((a, b) => {
        // folders first is a filesystem convention, not a sort key: it holds
        // whichever column the user sorts by
        if (a.type !== b.type) return a.type === 'dir' ? -1 : 1;
        let r;
        if (k === 'size') r = (a.size || 0) - (b.size || 0);
        else if (k === 'mtime') r = (a.mtime || 0) - (b.mtime || 0);
        // numeric: 'report-2' must sort before 'report-10', not after it
        else r = a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' });
        return r * d;
      });
      return rows;
    },

    expSummary() {
      const shown = this.expRows(), total = this.expEntries.length;
      const dirs = shown.filter(e => e.type === 'dir').length;
      const files = shown.length - dirs;
      const bytes = shown.reduce((n, e) => n + (e.type === 'file' ? (e.size || 0) : 0), 0);
      const parts = [];
      if (dirs) parts.push(`${dirs} folder${dirs === 1 ? '' : 's'}`);
      if (files) parts.push(`${files} file${files === 1 ? '' : 's'}`);
      let s = parts.join(', ') || 'empty';
      if (files) s += ` · ${this.fmtBytes(bytes)}`;
      if (shown.length !== total) s += ` (of ${total})`;
      return s;
    },

    // what the hidden Size/Modified columns become on a phone
    expSub(e) {
      return e.type === 'file'
        ? `${this.fmtBytes(e.size)} · ${this.fmtTime(e.mtime)}`
        : this.fmtTime(e.mtime);
    },

    async loadFiles() {
      if (!this.expVol) return;
      this.expBusy = true; this.expErr = '';
      try {
        const r = await this.api('GET',
          `/volumes/${this.expVol.id}/files?path=${encodeURIComponent(this.expPath)}`);
        if (r.ok) this.expEntries = r.data.slice();  // expRows() filters + sorts
        else if (r.status === 401) {
          // Unlocked (encrypted) volume, but this browser holds no session:
          // route to the PIN modal once. The guard flag stops a failed
          // attach from looping through here forever.
          this.expEntries = [];
          this._clearTok(this.expVol.id);  // token is stale or absent
          if (!this._autoAttached) {
            this._autoAttached = true;
            this.expBusy = false;
            this.attachVol(this.expVol);
            return;
          }
          this.expErr = 'This browser has no session for this volume — unlock it.';
        }
        else if (r.status === 409) {
          // Session gone (e.g. manager restarted): route back into Open.
          this.expEntries = [];
          this._modal('expModal').hide();
          await this.refresh();
          const v = this._vol(this.expVol.id);
          if (v) this.openVol(v);
        }
        else { this.expEntries = []; this.expErr = r.data?.error || 'Failed to list files.'; }
      } catch { this.expErr = 'Could not reach API.'; }
      this.expBusy = false;
    },

    async expMkdir() {
      const name = await this.ask('New folder', '', 'Create');
      if (!name) return;
      const r = await this.api('POST',
        `/volumes/${this.expVol.id}/files/mkdir?path=${encodeURIComponent(this.expJoin(name))}`);
      if (!r.ok) this.expErr = r.data?.error || 'mkdir failed.';
      this.loadFiles();
    },

    async expDelete(e) {
      const ok = await this.confirm(
        'Delete?', `${e.name}${e.type === 'dir' ? ' and everything inside it' : ''} ` +
        'will be removed. This cannot be undone.', 'Delete', true);
      if (!ok) return;
      const r = await this.api('DELETE',
        `/volumes/${this.expVol.id}/files?path=${encodeURIComponent(this.expJoin(e.name))}`);
      if (!r.ok) { this.expErr = r.data?.error || 'delete failed.'; this.toast(this.expErr, false); }
      else this.toast(`Deleted ${e.name}`);
      this.loadFiles();
    },

    async expDownload(e) {
      const a = document.createElement('a');
      const url = `${this.apiBase}/volumes/${this.expVol.id}/files/download?path=${encodeURIComponent(this.expJoin(e.name))}`;
      if (this._tok(this.expVol.id)) {
        const r = await fetch(url, { headers: this._authHeaders(this.expVol.id) });
        if (!r.ok) { this.toast('Download failed', false); return; }
        a.href = URL.createObjectURL(await r.blob());
        a.download = e.name;
        a.click();
        setTimeout(() => URL.revokeObjectURL(a.href), 60000);
        return;
      }
      a.href = url;
      a.download = e.name;
      a.click();
    },

    async expRename(e) {
      const name = await this.ask(`Rename ${e.name}`, e.name, 'Rename');
      if (!name || name === e.name) return;
      await this._transfer('move', this.expJoin(e.name), this.expJoin(name), 'Renamed');
    },

    // Destination is an absolute path inside the volume, which the prefilled
    // value demonstrates — the hint says so for anyone who clears the field.
    _pathHint: 'Absolute path inside this volume, e.g. /reports/2026/name.txt. ' +
               'The destination folder must already exist.',

    async expMove(e) {
      const src = this.expJoin(e.name);
      const dst = await this.ask(`Move ${e.name}`, src, 'Move', this._pathHint);
      if (!dst || dst === src) return;
      await this._transfer('move', src, dst, 'Moved');
    },

    async expCopy(e) {
      const src = this.expJoin(e.name);
      const dst = await this.ask(`Copy ${e.name}`, src + '.copy', 'Copy', this._pathHint);
      if (!dst || dst === src) return;
      await this._transfer('copy', src, dst, 'Copied');
    },

    async _transfer(op, src, dst, verb) {
      const r = await this.api('POST', `/volumes/${this.expVol.id}/files/transfer`, { op, src, dst });
      if (r.ok) this.toast(`${verb} to ${dst}`);
      else { this.expErr = r.data?.error || `${op} failed.`; this.toast(this.expErr, false); }
      this.loadFiles();
    },

    // Formats the browser can actually display. Deliberately narrow on media:
    // an extension listed here promises a working player, and mkv/avi/mov/wmv
    // do not decode in most browsers -- offering them would just render a
    // broken control strip. Same for tiff/psd/heic on the image side.
    _RE_IMG: /\.(png|jpe?g|gif|webp|avif|svg|bmp|ico)$/i,
    _RE_AUDIO: /\.(mp3|m4a|aac|wav|flac|ogg|oga|opus|weba)$/i,
    _RE_VIDEO: /\.(mp4|m4v|webm|ogv)$/i,
    _RE_MD: /\.(md|markdown)$/i,

    canPreview(name) {
      return this._RE_IMG.test(name) || this._RE_AUDIO.test(name)
        || this._RE_VIDEO.test(name) || /\.pdf$/i.test(name)
        || this.canEdit(name);
    },

    // Anything the editor will open as text is previewable as text too: the
    // download endpoint serves those inline as text/plain (see
    // _inline_mimetype in manager/api.py), so the frame renders rather than
    // prompting a download. Keeping the two lists in step is the point --
    // they used to disagree, and .rs / Makefile / .toml had an Edit button
    // but no preview.
    canEdit(name) {
      // text-ish extensions, or none at all (Makefile, LICENSE, dotfiles)
      return /\.(txt|text|md|markdown|rst|log|out|err|json|jsonl|csv|tsv|ya?ml|py|pyi|js|mjs|cjs|ts|tsx|jsx|html?|css|scss|sass|less|sh|bash|zsh|fish|ps1|bat|toml|ini|cfg|conf|config|env|properties|xml|plist|rs|go|c|h|cc|cpp|hpp|cs|java|kt|kts|swift|rb|php|pl|pm|lua|r|jl|sql|graphql|proto|tf|hcl|diff|patch|srt|vtt|gitignore|gitattributes|dockerignore|editorconfig)$/i.test(name)
        || !/\./.test(name.replace(/^\./, ''));
    },

    _pvKindFor(name) {
      if (this._RE_MD.test(name)) return 'md';
      if (this._RE_IMG.test(name)) return 'img';
      if (this._RE_AUDIO.test(name)) return 'audio';
      if (this._RE_VIDEO.test(name)) return 'video';
      if (/\.pdf$/i.test(name)) return 'pdf';
      return 'frame';
    },

    async expPreview(e) {
      this.pvName = e.name;
      this.pvErr = '';
      this.pvDoc = '';
      const kind = this._pvKindFor(e.name);

      // Markdown is rendered here and handed to the iframe as srcdoc, so it
      // never travels as a volume URL at all.
      if (kind === 'md') {
        try {
          this.pvDoc = this._mdDoc(await this._fetchText(this.expPath, e.name));
        } catch (err) { this.toast(`Preview failed: ${err.message}`, false); return; }
        this.pvKind = 'md';
        this._modal('pvModal').show();
        return;
      }

      const url = this._fileUrl(this.expPath, e.name, true);
      // Media ALWAYS goes through a blob, even on an unencrypted volume: the
      // download endpoint sends no Accept-Ranges, so a <video> pointed at it
      // cannot seek. A blob: URL is wholly in memory, so the browser serves
      // its own ranges and the scrubber works -- at the cost of holding the
      // file in RAM, hence the size guard in the caller.
      const mustBlob = kind === 'audio' || kind === 'video' || !!this._tok(this.expVol.id);
      if (mustBlob) {
        if (!await this._pvSizeOk(e, kind)) return;
        try {
          const r = await fetch(url, { headers: this._authHeaders(this.expVol.id) });
          if (!r.ok) { this.toast('Preview failed', false); return; }
          this.pvUrl = URL.createObjectURL(await r.blob());
        } catch { this.toast('Preview failed', false); return; }
      } else {
        this.pvUrl = url;
      }
      this.pvKind = kind;
      this._modal('pvModal').show();
    },

    // Previewing pulls the whole file into memory; warn before doing that to
    // something large. Mirrors the editor's confirm at edOpen.
    async _pvSizeOk(e, kind) {
      const cap = (kind === 'audio' || kind === 'video') ? 64 * 1024 * 1024 : 16 * 1024 * 1024;
      if ((e.size || 0) <= cap) return true;
      return await this.confirm('Large file',
        `${e.name} is ${this.fmtBytes(e.size)}. Previewing it loads the whole ` +
        'file into the browser.', 'Preview anyway');
    },

    pvMediaError() {
      this.pvErr = `${this.pvName} could not be decoded by this browser. ` +
        'Download it to play locally.';
    },

    // Wrap marked's output in a document the sandbox can render.
    //
    // The iframe is sandbox="" (no allow-scripts), so nothing in here
    // executes. The CSP closes the remaining gap: sandboxing does NOT stop
    // subresource loads, so a markdown file carrying ![](http://tracker/x)
    // would otherwise phone home the moment someone previewed it. default-src
    // 'none' with img-src data: keeps inline data images working and blocks
    // every off-box fetch. This is why the manager ships no HTML sanitiser --
    // the sandbox plus this CSP is the stronger of the two options.
    _mdDoc(md) {
      let body;
      try {
        body = marked.parse(md, { gfm: true, breaks: false });
      } catch (err) {
        body = '<p>Could not render markdown.</p>';
      }
      return '<!doctype html><html><head><meta charset="utf-8">'
        + '<meta http-equiv="Content-Security-Policy" content="'
        + "default-src 'none'; style-src 'unsafe-inline'; img-src data:\">"
        + '<style>'
        + 'body{font:14px/1.6 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;'
        + 'color:#212529;margin:0;padding:1rem 1.25rem;text-align:left;}'
        + 'h1,h2,h3{margin:1.2em 0 .5em;line-height:1.25;}'
        + 'h1{font-size:1.6rem;}h2{font-size:1.3rem;}h3{font-size:1.1rem;}'
        + 'h1,h2{border-bottom:1px solid #dee2e6;padding-bottom:.25em;}'
        + 'code{background:#f1f3f5;padding:.15em .35em;border-radius:3px;'
        + 'font-size:.875em;}'
        + 'pre{background:#f8f9fa;border:1px solid #e9ecef;border-radius:6px;'
        + 'padding:.75rem;overflow:auto;}pre code{background:none;padding:0;}'
        + 'blockquote{margin:0 0 1em;padding:.1em 1em;border-left:4px solid #dee2e6;'
        + 'color:#6c757d;}'
        + 'table{border-collapse:collapse;margin:1em 0;}'
        + 'th,td{border:1px solid #dee2e6;padding:.35rem .6rem;}'
        + 'th{background:#f8f9fa;}img{max-width:100%;}'
        + 'a{color:#0d6efd;}hr{border:0;border-top:1px solid #dee2e6;}'
        + '</style></head><body>' + body + '</body></html>';
    },

    // -- text editor --------------------------------------------------------
    _authHeaders(vid) {
      const t = this._tok(vid);
      const h = { 'X-Aloelite': '1' };
      if (t) h['X-Aloelite-Token'] = t;
      return h;
    },

    _fileUrl(dir, name, inline) {
      const p = (dir === '/' ? '' : dir) + '/' + name;
      return `${this.apiBase}/volumes/${this.expVol.id}/files/download` +
        `?path=${encodeURIComponent(p)}${inline ? '&inline=1' : ''}`;
    },

    async _fetchText(dir, name) {
      const r = await fetch(this._fileUrl(dir, name, true),
        { headers: this._authHeaders(this.expVol.id) });
      if (!r.ok) throw new Error(`load failed (${r.status})`);
      return await r.text();
    },

    // Extension -> Prism language id, for the languages bundled in
    // static/prism.js. An extension absent from here simply gets no
    // highlighting; it is never an error.
    _LANGS: {
      js: 'javascript', mjs: 'javascript', cjs: 'javascript', jsx: 'javascript',
      ts: 'typescript', tsx: 'typescript', json: 'json', jsonl: 'json',
      py: 'python', pyi: 'python', rb: 'ruby', php: 'php', pl: 'perl', pm: 'perl',
      lua: 'lua', rs: 'rust', go: 'go', c: 'c', h: 'c', cc: 'c', cpp: 'c', hpp: 'c',
      java: 'java', sql: 'sql', sh: 'bash', bash: 'bash', zsh: 'bash', fish: 'bash',
      yaml: 'yaml', yml: 'yaml', toml: 'toml', ini: 'ini', cfg: 'ini', conf: 'ini',
      properties: 'properties', env: 'bash', css: 'css', scss: 'css', less: 'css',
      html: 'markup', htm: 'markup', xml: 'markup', svg: 'markup', plist: 'markup',
      md: 'markdown', markdown: 'markdown', diff: 'diff', patch: 'diff',
      dockerfile: 'docker', gitignore: 'git', gitattributes: 'git',
    },

    // Above this, re-highlighting the whole buffer on every keystroke stops
    // being free -- the overlay re-parses the entire file each time. Past the
    // cap the editor degrades to the plain textarea it has always been.
    _HL_CAP: 100 * 1024,

    _langFor(name) {
      const base = name.toLowerCase();
      const ext = /\./.test(base.replace(/^\./, '')) ? base.split('.').pop() : base;
      const lang = this._LANGS[ext];
      return (lang && window.Prism && Prism.languages[lang]) ? lang : '';
    },

    get edIsMd() { return this._RE_MD.test(this.edName); },

    // Highlight only when we know the language AND the file is small enough.
    get edHl() {
      return !!this.edLang && this.edText.length <= this._HL_CAP;
    },

    get edHtml() {
      if (!this.edHl) return '';
      let html;
      try {
        html = Prism.highlight(this.edText, Prism.languages[this.edLang], this.edLang);
      } catch (err) {
        // Highlighting is a nicety; the file still has to be editable. Drop to
        // the plain layer for the rest of this session rather than leaving the
        // overlay showing nothing.
        this.edLang = '';
        return '';
      }
      // A <pre> gives its final newline no height, so a buffer ending in one
      // leaves the highlight layer a line shorter than the textarea and the
      // two scroll out of step. Pad it back.
      if (this.edText.endsWith('\n')) html += '\n';
      return html;
    },

    get edMdDoc() { return this.edIsMd ? this._mdDoc(this.edText) : ''; },

    edMdButtons: [
      { title: 'Bold', label: '<b>B</b>', op: 'bold' },
      { title: 'Italic', label: '<i>I</i>', op: 'italic' },
      { title: 'Heading', label: 'H', op: 'head' },
      { title: 'Link', label: '&#128279;', op: 'link' },
      { title: 'Bullet list', label: '&bull;&nbsp;', op: 'ul' },
      { title: 'Numbered list', label: '1.', op: 'ol' },
      { title: 'Quote', label: '&rdquo;', op: 'quote' },
      { title: 'Inline code', label: '&lt;/&gt;', op: 'code' },
      { title: 'Code block', label: '&#9744;', op: 'fence' },
      { title: 'Checklist', label: '&#9745;', op: 'task' },
    ],

    edSetView(m) {
      this.edView = m;
      if (m !== 'preview') this.$nextTick(() => this.$refs.edTa?.focus());
    },

    // The highlight layer scrolls with the textarea. Both wrap identically
    // (see the shared rule in admin.html), so only the vertical offset moves.
    edSyncScroll() {
      const ta = this.$refs.edTa, pre = this.$refs.edPre;
      if (ta && pre) { pre.scrollTop = ta.scrollTop; pre.scrollLeft = ta.scrollLeft; }
    },

    async edOpen(e) {
      if (e.size > 2 * 1024 * 1024) {
        const ok = await this.confirm('Large file',
          `${e.name} is ${this.fmtBytes(e.size)}. Opening it in the editor loads ` +
          'the whole file into the browser.', 'Open anyway');
        if (!ok) return;
      }
      this.edOpenPath(this.expPath, e.name);
    },

    async edOpenPath(dir, name) {
      this.edErr = ''; this.edBusy = true;
      this.edName = name; this.edDir = dir;
      this.edLang = this._langFor(name);
      this.edView = 'edit';
      try {
        this.edText = this.edLoaded = await this._fetchText(dir, name);
        this._modal('edModal').show();
        this.$nextTick(() => this.edSyncScroll());
      } catch (err) { this.toast(`Could not open ${name}: ${err.message}`, false); }
      this.edBusy = false;
    },

    // -- markdown editing: selection surgery on the textarea ----------------
    // Deliberately not an editor library. Everything below is setRangeText
    // plus cursor bookkeeping, which is the whole cost of the Keep-tier
    // feature set; a real rich editor would be two orders of magnitude more
    // code and a second engine to vendor.
    _edSel() {
      const ta = this.$refs.edTa;
      return { ta, s: ta.selectionStart, e: ta.selectionEnd,
               sel: this.edText.slice(ta.selectionStart, ta.selectionEnd) };
    },

    _edApply(text, selStart, selEnd) {
      const ta = this.$refs.edTa;
      this.edText = text;
      this.$nextTick(() => {
        ta.focus();
        ta.selectionStart = selStart;
        ta.selectionEnd = selEnd === undefined ? selStart : selEnd;
        this.edSyncScroll();
      });
    },

    // Wrap the selection, or drop the markers at the caret ready to type in.
    _edWrap(before, after) {
      const { s, e, sel } = this._edSel();
      const text = this.edText.slice(0, s) + before + sel + after + this.edText.slice(e);
      if (sel) this._edApply(text, s + before.length, s + before.length + sel.length);
      else this._edApply(text, s + before.length);
    },

    // Prefix every line the selection touches. `fn` receives the 0-based
    // index so ordered lists can count.
    _edLines(fn) {
      const { s, e } = this._edSel();
      const start = this.edText.lastIndexOf('\n', s - 1) + 1;
      let end = this.edText.indexOf('\n', e);
      if (end === -1) end = this.edText.length;
      const block = this.edText.slice(start, end).split('\n').map(fn).join('\n');
      this._edApply(this.edText.slice(0, start) + block + this.edText.slice(end),
        start, start + block.length);
    },

    edMd(op) {
      switch (op) {
        case 'bold': return this._edWrap('**', '**');
        case 'italic': return this._edWrap('*', '*');
        case 'code': return this._edWrap('`', '`');
        case 'fence': return this._edWrap('```\n', '\n```');
        case 'link': {
          const { sel } = this._edSel();
          return sel ? this._edWrap('[', '](url)') : this._edWrap('[', '](url)');
        }
        // Toggling: a second press on an already-prefixed block strips it,
        // so the buttons behave like the toggles they look like.
        case 'head': return this._edLines(l =>
          l.startsWith('#') ? l.replace(/^#+\s*/, '') : '## ' + l);
        case 'ul': return this._edLines(l =>
          l.startsWith('- ') ? l.slice(2) : '- ' + l);
        case 'ol': return this._edLines((l, i) =>
          /^\d+\.\s/.test(l) ? l.replace(/^\d+\.\s*/, '') : `${i + 1}. ` + l);
        case 'quote': return this._edLines(l =>
          l.startsWith('> ') ? l.slice(2) : '> ' + l);
        case 'task': return this._edLines(l =>
          /^- \[[ x]\]\s/.test(l) ? l.replace(/^- \[[ x]\]\s*/, '') : '- [ ] ' + l);
      }
    },

    // Tab indents rather than leaving the field. Escape still exits the
    // textarea, so this does not trap keyboard users in the editor.
    edTab(ev) {
      const { s, e, sel } = this._edSel();
      if (sel.includes('\n')) {
        return this._edLines(l => ev.shiftKey ? l.replace(/^ {1,2}/, '') : '  ' + l);
      }
      if (ev.shiftKey) {
        const start = this.edText.lastIndexOf('\n', s - 1) + 1;
        if (/^ {1,2}/.test(this.edText.slice(start))) {
          const cut = this.edText.slice(start).match(/^ {1,2}/)[0].length;
          return this._edApply(
            this.edText.slice(0, start) + this.edText.slice(start + cut),
            Math.max(start, s - cut));
        }
        return;
      }
      this._edApply(this.edText.slice(0, s) + '  ' + this.edText.slice(e), s + 2);
    },

    // Enter continues a list. An empty item ends the list instead, which is
    // the behaviour every notes app has trained people to expect.
    edEnter(ev) {
      if (!this.edIsMd || ev.shiftKey) return;
      const { s, e } = this._edSel();
      if (s !== e) return;
      const start = this.edText.lastIndexOf('\n', s - 1) + 1;
      const line = this.edText.slice(start, s);
      const m = line.match(/^(\s*)(-\s\[[ x]\]\s|[-*+]\s|(\d+)\.\s)/);
      if (!m) return;
      ev.preventDefault();
      // "- " and nothing after it: the user is done with the list.
      if (line.slice(m[0].length).trim() === '') {
        return this._edApply(
          this.edText.slice(0, start) + this.edText.slice(s), start);
      }
      const next = m[3]
        ? `${m[1]}${parseInt(m[3], 10) + 1}. `
        : m[1] + m[2].replace('[x]', '[ ]');
      this._edApply(this.edText.slice(0, s) + '\n' + next + this.edText.slice(s),
        s + 1 + next.length);
    },

    async edNew() {
      const name = await this.ask('New file', '', 'Create');
      if (!name) return;
      this.edErr = ''; this.edName = name; this.edDir = this.expPath;
      this.edText = ''; this.edLoaded = null;  // null: Save enabled even when empty
      this.edLang = this._langFor(name);
      this.edView = 'edit';
      this._modal('edModal').show();
    },

    async edSave() {
      if (this.edBusy || this.edText === this.edLoaded) return;
      this.edBusy = true; this.edErr = '';
      try {
        const fd = new FormData();
        fd.append('file', new File([this.edText], this.edName, { type: 'text/plain' }));
        const r = await fetch(
          `${this.apiBase}/volumes/${this.expVol.id}/files/upload?path=${encodeURIComponent(this.edDir)}`,
          { method: 'POST', body: fd, headers: this._authHeaders(this.expVol.id) });
        if (!r.ok) {
          const d = await r.json().catch(() => null);
          this.edErr = d?.error || `save failed (${r.status})`;
        } else {
          this.edLoaded = this.edText;
          this.toast(`Saved ${this.edName}`);
          this.loadFiles();
        }
      } catch { this.edErr = 'Could not reach API.'; }
      this.edBusy = false;
    },

    async edClose() {
      if (this.edText !== this.edLoaded) {
        const ok = await this.confirm('Discard changes?',
          `${this.edName} has unsaved edits.`, 'Discard', true, 'Keep editing');
        if (!ok) return;
      }
      this._modal('edModal').hide();
    },

    // -- sketch pad ---------------------------------------------------------
    // signature_pad does the part that is genuinely fiddly: pointer capture,
    // stylus pressure -> stroke width, and bezier smoothing between samples.
    // Everything here is the shell around it. Output is SVG because
    // canPreview() already renders .svg through the image path, so a saved
    // sketch is viewable immediately and stays scalable; SVG in an <img>
    // cannot execute script, so it needs no special handling.
    skOpen() {
      this.skErr = '';
      this.skDir = this.expPath;
      // Sizing happens on shown.bs.modal (wired in init), NOT here and not in
      // $nextTick: the modal fades in, so for the length of that transition
      // the canvas still measures 0 wide and the backing store would be
      // allocated at 0x0 -- strokes register but nothing rasterises and the
      // saved SVG comes out empty.
      this._modal('skModal').show();
    },

    _skInit() {
      const canvas = this.$refs.skCanvas;
      if (!canvas) return;
      this._skResize(canvas);
      if (!this._pad) {
        this._pad = new SignaturePad(canvas, {
          backgroundColor: 'rgba(0,0,0,0)',   // transparent: the grid shows through
          penColor: this.skColor,
        });
        // Re-measure on rotate/resize. Sizing a canvas clears it, so the
        // strokes are lifted out and put back around the change.
        window.addEventListener('resize', () => {
          if (!document.getElementById('skModal').classList.contains('show')) return;
          const data = this._pad.toData();
          this._skResize(canvas);
          this._pad.clear();
          if (data.length) this._pad.fromData(data);
        });
      }
      this.skSetSize();
      this._pad.clear();
    },

    // Back the canvas with devicePixelRatio pixels so strokes are not soft on
    // a HiDPI screen, then scale the context so drawing stays in CSS units.
    _skResize(canvas) {
      const ratio = Math.max(window.devicePixelRatio || 1, 1);
      this._padRatio = ratio;
      canvas.width = canvas.offsetWidth * ratio;
      canvas.height = canvas.offsetHeight * ratio;
      canvas.getContext('2d').scale(ratio, ratio);
    },

    skSetColor(c) {
      this.skColor = c;
      if (this._pad) this._pad.penColor = c;
    },

    skSetSize() {
      if (!this._pad) return;
      // A pen reports pressure, so keep a min/max spread for it; the ratio
      // holds across sizes so thin stays expressive and thick stays smooth.
      this._pad.minWidth = this.skSize * 0.5;
      this._pad.maxWidth = this.skSize * 1.2;
    },

    skUndo() {
      if (!this._pad) return;
      const data = this._pad.toData();
      if (!data.length) return;
      data.pop();
      this._pad.clear();
      if (data.length) this._pad.fromData(data);
    },

    skClear() { this._pad?.clear(); },

    async skSave() {
      if (!this._pad || this.skBusy) return;
      if (this._pad.isEmpty()) { this.skErr = 'Nothing drawn yet.'; return; }
      const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
      const name = await this.ask('Save sketch', `sketch-${stamp}.svg`, 'Save');
      if (!name) return;
      this.skBusy = true; this.skErr = '';
      try {
        // toDataURL hands back a base64 data: URL; fetch is the shortest
        // correct way back to bytes (no manual atob/charCode loop).
        const url = this._pad.toDataURL('image/svg+xml');
        const blob = await (await fetch(url)).blob();
        const fd = new FormData();
        fd.append('file', new File([blob], name, { type: 'image/svg+xml' }));
        const r = await fetch(
          `${this.apiBase}/volumes/${this.expVol.id}/files/upload?path=${encodeURIComponent(this.skDir)}`,
          { method: 'POST', body: fd, headers: this._authHeaders(this.expVol.id) });
        if (!r.ok) {
          const d = await r.json().catch(() => null);
          this.skErr = d?.error || `save failed (${r.status})`;
        } else {
          this.toast(`Saved ${name}`);
          this._pad.clear();
          this._modal('skModal').hide();
          this.loadFiles();
        }
      } catch { this.skErr = 'Could not reach API.'; }
      this.skBusy = false;
    },

    async skClose() {
      if (this._pad && !this._pad.isEmpty()) {
        const ok = await this.confirm('Discard sketch?',
          'This sketch has not been saved.', 'Discard', true, 'Keep drawing');
        if (!ok) return;
        this._pad.clear();
      }
      this._modal('skModal').hide();
    },

    // -- paste bin (phone <-> desktop text drop; lives in /pastes) ----------
    async openPastes() {
      this.pbErr = ''; this.pbText = '';
      this._modal('pbModal').show();
      await this.pbRefresh();
    },

    async pbRefresh() {
      this.pbBusy = true; this.pbEntries = [];
      try {
        const r = await this.api('GET',
          `/volumes/${this.expVol.id}/files?path=${encodeURIComponent('/pastes')}`);
        if (r.ok) {
          const files = r.data.filter(e => e.type === 'file')
            .sort((a, b) => b.mtime - a.mtime).slice(0, 50);
          // inline previews for the newest few small ones
          for (const e of files.slice(0, 10)) {
            if (e.size <= 16384) {
              try {
                const t = await this._fetchText('/pastes', e.name);
                e.preview = t.slice(0, 120).replace(/\s+/g, ' ');
              } catch { /* name-only row */ }
            }
          }
          this.pbEntries = files;
        }
        // 404/409: no /pastes folder yet — an empty list is the right answer
      } catch { this.pbErr = 'Could not reach API.'; }
      this.pbBusy = false;
    },

    async pbSave() {
      if (!this.pbText.trim()) return;
      this.pbBusy = true; this.pbErr = '';
      try {
        // best-effort mkdir; 409/500 "exists" is fine
        await this.api('POST',
          `/volumes/${this.expVol.id}/files/mkdir?path=${encodeURIComponent('/pastes')}`);
        const ts = new Date().toISOString().replace(/[:T]/g, '-').slice(0, 19);
        const fd = new FormData();
        fd.append('file', new File([this.pbText], `paste-${ts}.txt`, { type: 'text/plain' }));
        const r = await fetch(
          `${this.apiBase}/volumes/${this.expVol.id}/files/upload?path=${encodeURIComponent('/pastes')}`,
          { method: 'POST', body: fd, headers: this._authHeaders(this.expVol.id) });
        if (!r.ok) {
          const d = await r.json().catch(() => null);
          this.pbErr = d?.error || `save failed (${r.status})`;
        } else {
          this.pbText = '';
          this.toast('Paste saved');
          await this.pbRefresh();
        }
      } catch { this.pbErr = 'Could not reach API.'; }
      this.pbBusy = false;
    },

    async pbCopy(e) {
      try {
        const t = await this._fetchText('/pastes', e.name);
        await this.copyText(t);
        this.toast('Copied to clipboard');
      } catch { this.toast('Copy failed', false); }
    },

    async pbDelete(e) {
      const r = await this.api('DELETE',
        `/volumes/${this.expVol.id}/files?path=${encodeURIComponent('/pastes/' + e.name)}`);
      if (!r.ok) this.toast(r.data?.error || 'delete failed', false);
      await this.pbRefresh();
    },

    async copyText(t) {
      // navigator.clipboard needs a secure context; a VPN'd http:// origin
      // isn't one, so fall back to the selection-based path
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(t);
        return;
      }
      const ta = document.createElement('textarea');
      ta.value = t;
      ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.focus(); ta.select();
      try { document.execCommand('copy'); } finally { ta.remove(); }
    },

    async expUpload(ev) {
      const files = [...ev.target.files];
      ev.target.value = '';
      if (!files.length) return;
      this.expBusy = true; this.expErr = '';
      for (const f of files) await this._uploadOne(f, this.expPath);
      this.expBusy = false;
      this.loadFiles();
    },

    async _uploadOne(file, dirPath) {
      // Courtesy guard: an aloelite file dropped into a volume is usually an
      // import aimed at the wrong zone — but storing it as content is legal.
      if (/\.(sqlite|fs|aloe)$/i.test(file.name)) {
        const head = new Uint8Array(await file.slice(0, 16).arrayBuffer());
        const magic = 'SQLite format 3\u0000';
        if (String.fromCharCode(...head) === magic) {
          // both outcomes are legitimate, so name them on the buttons rather
          // than making the user decode OK/Cancel
          const imp = await this.confirm('That looks like an aloelite file',
            `"${file.name}" is an aloelite filesystem. Import it as its own ` +
            'filesystem, or store it as an ordinary file inside this volume?',
            'Import it', false, 'Store as a file');
          if (imp) { await this._importFile(file); return; }
        }
      }
      await this._uploadXhr(file, dirPath);
    },

    _uploadXhr(file, dirPath) {
      return new Promise(res => {
        this.expUploads.push({ id: Date.now() + Math.random(), name: file.name, pct: 0 });
        const item = this.expUploads[this.expUploads.length - 1]; // proxied ref
        const done = () => { this.expUploads = this.expUploads.filter(u => u.id !== item.id); };
        const xhr = new XMLHttpRequest();
        xhr.open('POST',
          `${this.apiBase}/volumes/${this.expVol.id}/files/upload?path=${encodeURIComponent(dirPath)}`);
        xhr.setRequestHeader('X-Aloelite', '1');
        const xt = this._tok(this.expVol.id);
        if (xt) xhr.setRequestHeader('X-Aloelite-Token', xt);
        xhr.upload.onprogress = e => {
          if (e.lengthComputable) item.pct = Math.round(e.loaded / e.total * 100);
        };
        xhr.onload = () => {
          done();
          if (xhr.status >= 200 && xhr.status < 300) {
            this.toast(`Uploaded ${file.name}`); res(true);
          } else {
            let d = null; try { d = JSON.parse(xhr.responseText); } catch {}
            this.expErr = d?.error || `upload of ${file.name} failed`;
            this.toast(this.expErr, false); res(false);
          }
        };
        xhr.onerror = () => {
          done(); this.expErr = 'Could not reach API.';
          this.toast(this.expErr, false); res(false);
        };
        const fd = new FormData();
        fd.append('file', file);
        xhr.send(fd);
      });
    },

    async expDrop(ev) {
      this.expBusy = true; this.expErr = '';
      const items = [...(ev.dataTransfer.items || [])];
      const entries = items.map(i => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
      if (entries.length) {
        for (const e of entries) await this._dropEntry(e, this.expPath);
      } else {
        for (const f of [...ev.dataTransfer.files]) await this._uploadOne(f, this.expPath);
      }
      this.expBusy = false;
      this.loadFiles();
    },

    async _dropEntry(entry, dirPath) {
      if (entry.isFile) {
        const file = await new Promise((res, rej) => entry.file(res, rej));
        await this._uploadOne(file, dirPath);
        return;
      }
      const sub = (dirPath === '/' ? '' : dirPath) + '/' + entry.name;
      await this.api('POST',
        `/volumes/${this.expVol.id}/files/mkdir?path=${encodeURIComponent(sub)}`);
      const reader = entry.createReader();
      // readEntries returns batches; loop until empty
      for (;;) {
        const batch = await new Promise((res, rej) => reader.readEntries(res, rej));
        if (!batch.length) break;
        for (const child of batch) await this._dropEntry(child, sub);
      }
    },

    fmtBytes(n) {
      if (n < 1024) return `${n} B`;
      if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
      if (n < 1073741824) return `${(n / 1048576).toFixed(1)} MB`;
      if (n < 1099511627776) return `${(n / 1073741824).toFixed(2)} GB`;
      return `${(n / 1099511627776).toFixed(2)} TB`;
    },

    // Compact by default — the full toLocaleString ate three lines per row on
    // a phone, and the year is noise for a file touched this year. The exact
    // stamp stays available as the cell's title / in stat.
    fmtTime(ts) {
      if (!ts) return '';
      const d = new Date(ts * 1000), now = new Date();
      if (d.toDateString() === now.toDateString()) {
        return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      }
      return d.toLocaleDateString([], d.getFullYear() === now.getFullYear()
        ? { month: 'short', day: 'numeric' }
        : { year: 'numeric', month: 'short', day: 'numeric' });
    },

    fmtTimeFull(ts) { return ts ? new Date(ts * 1000).toLocaleString() : ''; },
  };
}
