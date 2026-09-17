// /upload — pick (camera, files, folder, drag-and-drop) → sha256 pre-check →
// one-at-a-time multipart upload with progress → finish.
//
// API (routes/api-contributions.js):
//   POST /api/contributions {note, group_ids}          → {id}
//   HEAD /api/contributions/:id/files?sha256=          → 204 already on the server
//   POST /api/contributions/:id/files  (field `file`)  → 201 {duplicate} | 200 {skipped}
//   POST /api/contributions/:id/finish
//   GET  /api/contributions/mine                        (resume check)
//
// Resuming: the pre-check skips anything the server already holds, so
// picking the same photos again after a dropped connection only sends
// the rest. The open contribution id is remembered in localStorage and
// reused (same groups + note, still unfinished) so a resumed batch stays
// one contribution for the admin.
(function () {
  'use strict';

  const form = document.querySelector('[data-upload]');
  if (!form) return;

  const MAX_BYTES = 100 * 1024 * 1024;
  const STORE_KEY = 'cd.upload.open';
  const EXT_MIME = {
    jpg: 'image/jpeg', jpeg: 'image/jpeg', jpe: 'image/jpeg', png: 'image/png',
    tif: 'image/tiff', tiff: 'image/tiff', heic: 'image/heic', heif: 'image/heic', webp: 'image/webp',
    mp4: 'video/mp4', m4v: 'video/mp4', mov: 'video/quicktime', avi: 'video/x-msvideo', webm: 'video/webm',
  };
  const OK_MIMES = new Set(Object.values(EXT_MIME));
  const PREVIEWABLE = new Set(['image/jpeg', 'image/png', 'image/webp']);

  const svg = (d) => `<svg class="icon" viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${d}</svg>`;
  const ICON_PHOTO = svg('<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="M21 15l-5-5L5 21"/>');
  const ICON_VIDEO = svg('<rect x="3" y="6" width="13" height="12" rx="2"/><path d="M16 10l5-3v10l-5-3z"/>');
  const ICON_CLOSE = svg('<path d="M6 6l12 12M18 6L6 18"/>');

  const $ = (sel) => form.querySelector(sel);
  const listEl = $('[data-list]');
  const statusEl = $('[data-status]');
  const barEl = $('[data-bar]');
  const countEl = $('[data-count]');
  const sendBtn = $('[data-send]');
  const finishBtn = $('[data-finish]');
  const clearBtn = $('[data-clear]');
  const dropEl = $('[data-drop]');
  const noteEl = form.querySelector('#up-note');
  const groupsEl = form.querySelector('.up-groups');
  const doneEl = document.querySelector('[data-done]');
  const loadingEl = document.querySelector('[data-loading]');

  let items = [];
  let seq = 0;
  let cid = null;            // contribution id for the current batch
  let running = false;
  let started = false;       // Send pressed for this batch (inputs locked)
  let resumeTimer = null;
  let lastRunning = false;
  let resumed = false;       // cid reused from an unfinished earlier visit

  // ---- helpers ------------------------------------------------------------
  function mb(n) {
    if (n < 1024 * 1024) return `${Math.max(1, Math.round(n / 1024))} KB`;
    return `${(n / 1024 / 1024).toFixed(1)} MB`;
  }
  function plural(n, one, many) { return `${n} ${n === 1 ? one : (many || `${one}s`)}`; }
  function mimeOf(file) {
    const t = (file.type || '').toLowerCase();
    if (OK_MIMES.has(t)) return t;
    const m = /\.([a-z0-9]+)$/i.exec(file.name || '');
    return (m && EXT_MIME[m[1].toLowerCase()]) || (t === 'image/heif' ? 'image/heic' : t);
  }
  function setStatus(html, kind) {
    statusEl.innerHTML = html ? `<p class="notice ${kind || 'info'}">${html}</p>` : '';
  }
  function storeGet() { try { return JSON.parse(localStorage.getItem(STORE_KEY) || 'null'); } catch { return null; } }
  function storeSet(v) { try { if (v) localStorage.setItem(STORE_KEY, JSON.stringify(v)); else localStorage.removeItem(STORE_KEY); } catch { /* private mode */ } }
  function chosenGroups() {
    return Array.from(form.querySelectorAll('input[name="group_ids"]:checked')).map((i) => Number(i.value)).sort((a, b) => a - b);
  }

  // ---- list rendering ---------------------------------------------------------
  function addFiles(fileList, { fromFolder = false } = {}) {
    if (doneEl && !doneEl.hidden) resetBatch();
    let ignored = 0;
    const known = new Set(items.map((it) => `${it.file.name}|${it.file.size}|${it.file.lastModified}`));
    for (const file of Array.from(fileList)) {
      if (!file || /^\./.test(file.name) || /^thumbs\.db$/i.test(file.name)) continue;
      const mime = mimeOf(file);
      if (!OK_MIMES.has(mime)) { ignored += 1; continue; }
      const key = `${file.name}|${file.size}|${file.lastModified}`;
      if (known.has(key)) continue;
      known.add(key);
      const it = { id: ++seq, file, mime, state: 'queued', pct: 0, attempts: 0 };
      if (file.size > MAX_BYTES) { it.state = 'invalid'; it.message = 'Too big to send (the limit is 100 MB)'; }
      items.push(it);
      listEl.appendChild(renderItem(it));
    }
    if (ignored) {
      setStatus(`${plural(ignored, 'file')} ${fromFolder ? 'in that folder ' : ''}${ignored === 1 ? "wasn't a photo or video, so it was" : "weren't photos or videos, so they were"} left out.`, 'info');
    }
    refresh();
  }

  function renderItem(it) {
    const li = document.createElement('li');
    li.className = 'up-item';
    li.dataset.id = String(it.id);
    const prev = document.createElement('span');
    prev.className = 'up-prev';
    if (PREVIEWABLE.has(it.mime) || (it.mime.startsWith('image/') && it.file.type)) {
      it.url = URL.createObjectURL(it.file);
      const img = document.createElement('img');
      img.alt = '';
      img.loading = 'lazy';
      img.decoding = 'async';
      img.src = it.url;
      img.addEventListener('error', () => { prev.innerHTML = ICON_PHOTO; });
      prev.appendChild(img);
    } else {
      prev.innerHTML = it.mime.startsWith('video/') ? ICON_VIDEO : ICON_PHOTO;
    }
    const info = document.createElement('div');
    info.className = 'up-info';
    const name = document.createElement('span');
    name.className = 'up-name';
    name.textContent = it.file.webkitRelativePath || it.file.name || 'Photo';
    const size = document.createElement('span');
    size.className = 'up-size';
    size.textContent = mb(it.file.size);
    const bar = document.createElement('span');
    bar.className = 'up-progress';
    bar.setAttribute('aria-hidden', 'true');
    bar.innerHTML = '<span></span>';
    const state = document.createElement('div');
    state.className = 'up-state';
    info.append(name, size, bar, state);
    li.append(prev, info);
    it.el = li;
    paint(it);
    return li;
  }

  const LABELS = {
    queued: () => 'Ready to send',
    hashing: () => 'Checking…',
    checking: () => 'Checking…',
    uploading: (it) => (it.pct >= 100 ? 'Saving…' : `Uploading ${it.pct}%`),
    done: () => 'Sent',
    skipped: () => 'Already on the server — skipped',
    dup: () => 'Sent — this looks like a photo we may already have. The admin will compare.',
    failed: (it) => `Couldn't send${it.message ? ` — ${it.message}` : ''}`,
    invalid: (it) => it.message || "Can't send this file",
  };

  function paint(it) {
    if (!it.el) return;
    it.el.dataset.state = it.state;
    const pct = ['done', 'skipped', 'dup'].includes(it.state) ? 100 : (it.state === 'uploading' ? it.pct : 0);
    it.el.querySelector('.up-progress > span').style.width = `${pct}%`;
    const st = it.el.querySelector('.up-state');
    st.textContent = '';
    const label = document.createElement('span');
    label.textContent = LABELS[it.state](it);
    st.appendChild(label);
    if (it.state === 'failed') {
      const retry = document.createElement('button');
      retry.type = 'button';
      retry.className = 'btn secondary small';
      retry.textContent = 'Retry';
      retry.addEventListener('click', () => { it.state = 'queued'; it.message = null; paint(it); refresh(); if (started) run(); });
      st.appendChild(retry);
    }
    if (['queued', 'invalid', 'failed'].includes(it.state) && !(running && it.state === 'queued')) {
      const rm = document.createElement('button');
      rm.type = 'button';
      rm.className = 'icon-btn up-remove';
      rm.setAttribute('aria-label', `Remove ${it.file.name} from the list`);
      rm.innerHTML = ICON_CLOSE;
      rm.addEventListener('click', () => removeItem(it));
      st.appendChild(rm);
    }
  }

  function removeItem(it) {
    if (it.url) URL.revokeObjectURL(it.url);
    if (it.el) it.el.remove();
    items = items.filter((x) => x !== it);
    refresh();
  }

  function counts() {
    const c = { queued: 0, active: 0, done: 0, skipped: 0, dup: 0, failed: 0, invalid: 0 };
    for (const it of items) {
      if (it.state === 'hashing' || it.state === 'checking' || it.state === 'uploading') c.active += 1;
      else c[it.state] += 1;
    }
    return c;
  }

  function refresh() {
    const c = counts();
    barEl.hidden = items.length === 0;
    const toSend = c.queued + c.active;
    if (running) {
      const finishedN = c.done + c.skipped + c.dup + c.failed;
      countEl.textContent = `Sending ${Math.min(finishedN + 1, finishedN + toSend)} of ${finishedN + toSend}…`;
    } else if (c.queued) {
      countEl.textContent = `${plural(c.queued, 'photo')} ready to send`;
    } else if (c.failed) {
      countEl.textContent = `${plural(c.failed, 'photo')} couldn't be sent`;
    } else {
      countEl.textContent = items.length ? 'Nothing left to send' : '';
    }
    sendBtn.hidden = running || c.queued === 0;
    sendBtn.textContent = c.queued === 1 ? 'Send 1 photo' : `Send ${c.queued} photos`;
    clearBtn.hidden = running || started;
    finishBtn.hidden = running || !(started && c.failed && !c.queued);
    // Groups and note belong to the contribution — locked once it exists.
    if (groupsEl) groupsEl.disabled = started;
    noteEl.disabled = started;
    // Remove buttons on queued rows depend on `running`; repaint on change only.
    if (running !== lastRunning) {
      lastRunning = running;
      for (const it of items) if (it.state === 'queued') paint(it);
    }
  }


  // ---- contribution --------------------------------------------------------------
  async function ensureContribution() {
    if (cid) return cid;
    const group_ids = chosenGroups();
    const note = noteEl.value.trim();
    const saved = storeGet();
    if (saved && saved.cid && saved.note === note && JSON.stringify(saved.group_ids) === JSON.stringify(group_ids)) {
      try {
        const mine = await window.CD.api('/api/contributions/mine');
        const c = (mine.items || []).find((x) => x.id === saved.cid);
        if (c && !c.finished_at && (c.status === 'pending' || c.status === 'partial')) {
          cid = c.id;
          resumed = c.file_count > 0;
          return cid;
        }
      } catch { /* fall through and create a new one */ }
    }
    const created = await window.CD.api('/api/contributions', { method: 'POST', body: { note, group_ids } });
    cid = created.id;
    storeSet({ cid, note, group_ids });
    return cid;
  }

  async function sha256Hex(file) {
    if (!(window.crypto && crypto.subtle && file.arrayBuffer)) return null;
    try {
      const hash = await crypto.subtle.digest('SHA-256', await file.arrayBuffer());
      return Array.from(new Uint8Array(hash)).map((b) => b.toString(16).padStart(2, '0')).join('');
    } catch { return null; }
  }

  function httpError(status, message, extra) {
    const e = new Error(message);
    e.status = status;
    Object.assign(e, extra || {});
    return e;
  }

  function sendFile(it, token) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', `/api/contributions/${cid}/files`);
      xhr.setRequestHeader('X-CSRF-Token', token);
      xhr.setRequestHeader('Accept', 'application/json');
      xhr.upload.onprogress = (e) => {
        if (!e.lengthComputable) return;
        it.pct = Math.min(100, Math.round((e.loaded / e.total) * 100));
        paint(it);
      };
      xhr.onload = () => {
        let body = null;
        try { body = JSON.parse(xhr.responseText || 'null'); } catch { body = null; }
        if (xhr.status >= 200 && xhr.status < 300) return resolve(body || {});
        const retryAfter = Number(xhr.getResponseHeader('Retry-After')) || 0;
        reject(httpError(xhr.status, (body && body.error) || '', { retryAfter, json: Boolean(body) }));
      };
      xhr.onerror = () => reject(httpError(0, 'network'));
      xhr.ontimeout = () => reject(httpError(0, 'network'));
      const fd = new FormData();
      const blob = it.file.type === it.mime ? it.file : new File([it.file], it.file.name, { type: it.mime, lastModified: it.file.lastModified });
      fd.append('file', blob, it.file.name);
      xhr.send(fd);
    });
  }

  // ---- the runner -------------------------------------------------------------------
  async function processItem(it, token) {
    it.state = 'hashing';
    it.pct = 0;
    paint(it);
    const sha = await sha256Hex(it.file);
    if (sha) {
      it.state = 'checking';
      paint(it);
      try {
        const head = await fetch(`/api/contributions/${cid}/files?sha256=${sha}`, { method: 'HEAD', credentials: 'same-origin' });
        if (head.status === 204) { it.state = 'skipped'; return; }
      } catch { /* pre-check is an optimisation; upload anyway */ }
    }
    it.state = 'uploading';
    paint(it);
    const body = await sendFile(it, token);
    if (body.skipped) it.state = 'skipped';
    else if (body.duplicate) it.state = 'dup';
    else it.state = 'done';
    it.uploaded = !body.skipped;
  }

  async function run() {
    if (running) return;
    clearTimeout(resumeTimer);
    running = true;
    started = true;
    setStatus('');
    refresh();
    let stop = false;
    try {
      const token = await window.CD.csrf();
      await ensureContribution();
      let it;
      while (!stop && (it = items.find((x) => x.state === 'queued'))) {
        try {
          await processItem(it, token);
        } catch (err) {
          const s = err.status;
          if (s === 429) {
            it.state = 'queued';
            const secs = Math.max(60, err.retryAfter || 600);
            const at = new Date(Date.now() + secs * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
            setStatus(`You've sent a lot of photos this hour, so we're taking a short break. We'll carry on by ${at} if you keep this page open — everything already sent is safe. <button type="button" class="btn secondary small" data-resume>Try again now</button>`, 'info');
            resumeTimer = setTimeout(run, secs * 1000);
            stop = true;
          } else if (s === 409 && it.attempts < 1) {
            // The batch was decided while we were sending: start a fresh one.
            it.attempts += 1;
            it.state = 'queued';
            cid = null;
            storeSet(null);
            await ensureContribution();
          } else if (s === 401 || (s === 403 && !err.json)) {
            it.state = 'queued';
            setStatus('Your sign-in has expired. <a href="/login">Sign in again</a>, then pick the photos again — anything already sent will be skipped.', 'error');
            stop = true;
          } else {
            it.state = 'failed';
            it.message = s === 0 ? 'check your connection' : (s === 413 ? 'the file is too big' : (err.message || `error ${s}`));
          }
        }
        paint(it);
        refresh();
      }
    } catch (err) {
      const notMember = err.status === 403 && err.body && /not a member/.test(err.body.error || '');
      const msg = notMember
        ? "You can't send photos to one of those groups. Untick it and try again."
        : err.status === 403 || err.status === 401
          ? 'Your sign-in has expired. <a href="/login">Sign in again</a>, then pick the photos again — anything already sent will be skipped.'
        : (err.status === 0 || !err.status ? "Couldn't reach the server. Check your connection and try again." : `Couldn't start the upload (${err.message}).`);
      setStatus(msg, 'error');
      if (!cid) started = false;
      stop = true;
    } finally {
      running = false;
      refresh();
    }
    if (!stop) afterRun();
  }

  async function finish() {
    const c = counts();
    const sent = items.filter((x) => x.uploaded).length;
    // A resumed contribution already holds files from before the reload, so
    // it is finished even when this run only skipped things.
    if ((sent || resumed) && cid) {
      try {
        await window.CD.api(`/api/contributions/${cid}/finish`, { method: 'POST', body: {} });
      } catch {
        setStatus("Your photos were sent, but we couldn't tell the admin yet. Press Finish to try again.", 'error');
        finishBtn.hidden = false;
        return;
      }
      storeSet(null);
      cid = null;
      resumed = false;
    }
    showDone(sent, c.skipped, c.failed);
  }

  function afterRun() {
    const c = counts();
    if (c.failed) {
      setStatus(`${plural(c.failed, 'photo')} couldn't be sent. Press Retry next to ${c.failed === 1 ? 'it' : 'them'}, or Finish to send the rest without ${c.failed === 1 ? 'it' : 'them'}.`, 'error');
      return;
    }
    finish();
  }

  function showDone(sent, skipped, failed) {
    const title = doneEl.querySelector('[data-done-title]');
    const text = doneEl.querySelector('[data-done-text]');
    if (sent) {
      title.textContent = 'All sent — thank you!';
      const bits = [`${plural(sent, 'photo')} sent.`];
      if (skipped) bits.push(`${skipped} ${skipped === 1 ? 'was' : 'were'} already on the server.`);
      if (failed) bits.push(`${plural(failed, 'photo')} couldn't be sent.`);
      bits.push("They'll stay private until they're approved; you can check on them in My uploads.");
      text.textContent = bits.join(' ');
    } else {
      title.textContent = 'Nothing new to send';
      text.textContent = skipped
        ? `${skipped === 1 ? 'That photo is' : `All ${skipped} photos are`} already on the server, so there was nothing to send.`
        : 'No photos were sent.';
    }
    barEl.hidden = true;
    setStatus('');
    doneEl.hidden = false;
    doneEl.focus();
  }

  function resetBatch() {
    // A contribution that received nothing (everything skipped) is reused
    // for the next batch; one that got files was finished — start fresh.
    if (items.some((x) => x.uploaded)) cid = null;
    for (const it of items) if (it.url) URL.revokeObjectURL(it.url);
    items = [];
    listEl.innerHTML = '';
    started = false;
    doneEl.hidden = true;
    setStatus('');
    refresh();
  }

  // ---- wiring ----------------------------------------------------------------------------
  form.addEventListener('submit', (e) => { e.preventDefault(); run(); });
  finishBtn.addEventListener('click', () => { for (const it of items) if (it.state === 'failed') removeItem(it); finish(); });
  clearBtn.addEventListener('click', () => resetBatch());
  statusEl.addEventListener('click', (e) => { if (e.target.closest('[data-resume]')) run(); });
  doneEl.querySelector('[data-more]').addEventListener('click', () => {
    resetBatch();
    form.querySelector('#up-files').focus();
  });
  form.querySelectorAll('input[data-pick]').forEach((input) => {
    input.addEventListener('change', () => {
      addFiles(input.files, { fromFolder: input.hasAttribute('webkitdirectory') });
      input.value = '';
    });
  });

  // Drag and drop anywhere on the page (desktop).
  if (dropEl && 'draggable' in document.createElement('span')) {
    dropEl.hidden = false;
    let depth = 0;
    const hasFiles = (e) => e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files');
    document.addEventListener('dragenter', (e) => { if (hasFiles(e)) { depth += 1; dropEl.classList.add('over'); } });
    document.addEventListener('dragleave', () => { depth = Math.max(0, depth - 1); if (!depth) dropEl.classList.remove('over'); });
    document.addEventListener('dragover', (e) => { if (hasFiles(e)) e.preventDefault(); });
    document.addEventListener('drop', async (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth = 0;
      dropEl.classList.remove('over');
      const dt = e.dataTransfer;
      const entries = [];
      if (dt.items && dt.items.length && typeof dt.items[0].webkitGetAsEntry === 'function') {
        for (const item of Array.from(dt.items)) {
          const entry = item.kind === 'file' ? item.webkitGetAsEntry() : null;
          if (entry) entries.push(entry);
        }
      }
      if (!entries.length) { addFiles(dt.files); return; }
      const files = [];
      for (const entry of entries) await walk(entry, files);
      addFiles(files, { fromFolder: entries.some((en) => en.isDirectory) });
    });
  }

  function walk(entry, out) {
    if (entry.isFile) {
      return new Promise((resolve) => entry.file((f) => { out.push(f); resolve(); }, () => resolve()));
    }
    if (!entry.isDirectory) return Promise.resolve();
    const reader = entry.createReader();
    return new Promise((resolve) => {
      const batch = () => reader.readEntries(async (list) => {
        if (!list.length) return resolve();
        for (const child of list) await walk(child, out);
        batch();
      }, () => resolve());
      batch();
    });
  }

  window.addEventListener('beforeunload', (e) => {
    if (!running) return undefined;
    e.preventDefault();
    e.returnValue = '';
    return '';
  });

  if (loadingEl) loadingEl.remove();
  form.hidden = false;
  refresh();
})();
