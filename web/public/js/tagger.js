// Face tagging on the photo page.
//
// "Tag a face" ([data-action="tag"]) turns tagging on: the face boxes
// (server-rendered in [data-face-layer], positioned as percentages of
// photo.width/height) become visible and tappable.
//
//   * Tap an unnamed box → name autocomplete → person suggestion
//     POST /api/photos/:id/suggestions {kind:'person', face_id, person_id | new_person}
//   * Tap a named box → "This isn't them" → POST /api/faces/:id/dispute {note},
//     and the same name field to say who it really is.
//   * Missed a face? Drag a box (mouse / pen), or tap two opposite corners
//     (touch, or click-click) → POST /api/photos/:id/faces {bbox, person_id | new_person}.
//     bbox is in photo.width/height pixel coordinates.
//
// Contributors never create people: a new name travels inside the
// suggestion as new_person {given_name, surname}; an admin's accept creates
// the person.
//
// Emits `cd:suggested` on the page element ({ kind: 'person', text }) so
// photo.js can add it to the pending list. window.CD.tagger exposes
// { start, stop, active } for photo.js (swipe guard, "Who is this?" chip).
(function () {
  'use strict';

  const page = document.querySelector('[data-photo-page]');
  const frame = page && page.querySelector('[data-photo-frame]');
  const layer = frame && frame.querySelector('[data-face-layer]');
  if (!layer) return;

  const $ = (sel, root) => (root || page).querySelector(sel);
  const photoId = page.dataset.photoId;
  const W = Number(page.dataset.width);
  const H = Number(page.dataset.height);
  const tagBtn = $('[data-action="tag"]');
  const bar = $('[data-tag-bar]');
  const barText = bar.querySelector('p');
  const barDefault = barText.textContent;
  const panel = $('[data-tag-panel]');
  const title = $('[data-tag-title]', panel);
  const crop = $('[data-tag-crop]', panel);
  const named = $('[data-tag-named]', panel);
  const personLink = $('[data-tag-person]', panel);
  const disputeOpen = $('[data-dispute-open]', panel);
  const disputeForm = $('[data-dispute-form]', panel);
  const nameForm = $('[data-name-form]', panel);
  const nameLabel = $('[data-name-label]', panel);
  const nameInput = $('[data-name-input]', panel);
  const nameSubmit = $('[data-name-submit]', panel);
  const thanks = $('[data-tag-thanks]', panel);

  let active = false;
  let target = null;   // { kind: 'face', box } | { kind: 'new', box, bbox }
  let picked = null;   // autocomplete item (isNew for "someone new")
  let corner = null;   // first corner of a two-tap box: { fx, fy, el }
  let down = null;     // pointer down state for drag / tap
  let ac = null;

  function errorText(err) {
    if (err.status === 429) return "You've sent a lot this hour. Please try again later.";
    if (err.status === 404) return "That isn't available any more. Reload the page.";
    if (err.status === 401 || err.status === 403) return 'Your session has expired. Reload the page and try again.';
    if (err.status === 400 && err.body && err.body.error) {
      const m = String(err.body.error);
      return m.charAt(0).toUpperCase() + m.slice(1);
    }
    return 'Something went wrong. Check your connection and try again.';
  }
  function showError(form, msg) {
    const el = form.querySelector('[data-form-error]');
    el.textContent = msg || '';
    el.hidden = !msg;
  }
  function yearSpan(p) {
    if (p.birth_year == null && p.death_year == null) return null;
    return `${p.birth_year != null ? p.birth_year : '?'}–${p.death_year != null ? p.death_year : ''}`;
  }
  function splitName(full) {
    const parts = String(full).trim().split(/\s+/).filter(Boolean);
    if (parts.length <= 1) return { given_name: parts[0] || '' };
    return { given_name: parts.slice(0, -1).join(' '), surname: parts[parts.length - 1] };
  }
  const pct = (v) => `${(Math.max(0, Math.min(1, v)) * 100).toFixed(3)}%`;

  // ---- tagging mode ----------------------------------------------------
  function start() {
    if (active) return;
    active = true;
    frame.classList.add('tagging');
    bar.hidden = false;
    if (tagBtn) tagBtn.setAttribute('aria-pressed', 'true');
    const r = frame.getBoundingClientRect();
    if (r.top < 0 || r.bottom > window.innerHeight) frame.scrollIntoView({ block: 'start', behavior: 'smooth' });
    const first = layer.querySelector('.face-unknown, .face-unnamed');
    if (first) first.focus({ preventScroll: true });
  }
  function stop() {
    active = false;
    frame.classList.remove('tagging');
    bar.hidden = true;
    if (tagBtn) tagBtn.setAttribute('aria-pressed', 'false');
    clearCorner();
    closePanel();
  }

  // ---- panel -----------------------------------------------------------
  function ensureAutocomplete() {
    if (ac) return;
    ac = window.CD.autocomplete(nameInput, {
      url: '/api/people/autocomplete?q=',
      label: (p) => p.display_name,
      detail: yearSpan,
      allowNew: (q) => ({ display_name: q }),
      newLabel: (q) => `“${q}” is someone new`,
      onPick: (item) => {
        picked = item;
        nameSubmit.disabled = !item;
        showError(nameForm, '');
      },
    });
  }

  function setCurrent(box) {
    layer.querySelectorAll('.face-box.current').forEach((b) => b.classList.remove('current'));
    if (box) box.classList.add('current');
  }

  function openPanel(t) {
    discardDrawn();
    target = t;
    picked = null;
    ensureAutocomplete();
    ac.clear();
    nameSubmit.disabled = true;
    showError(nameForm, '');
    showError(disputeForm, '');
    disputeForm.reset();
    disputeForm.hidden = true;
    disputeOpen.hidden = false;
    thanks.hidden = true;
    nameForm.hidden = false;
    setCurrent(t.box);

    const box = t.box;
    const isNamed = t.kind === 'face' && !!box.dataset.personId;
    if (t.kind === 'face') {
      crop.src = box.dataset.crop;
      crop.hidden = false;
    } else {
      crop.hidden = true;
      crop.removeAttribute('src');
    }
    named.hidden = !isNamed;
    if (isNamed) {
      title.textContent = box.dataset.personName;
      personLink.textContent = box.dataset.personName;
      personLink.href = `/people/${box.dataset.personId}`;
      nameLabel.textContent = 'Know who it really is?';
    } else {
      title.textContent = t.kind === 'new' ? 'Who did you find?' : 'Who is this?';
      nameLabel.textContent = 'Their name';
    }
    panel.hidden = false;
    panel.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    if (isNamed) disputeOpen.focus({ preventScroll: true });
    else nameInput.focus({ preventScroll: true });
  }

  function closePanel() {
    discardDrawn();
    target = null;
    panel.hidden = true;
    setCurrent(null);
    if (ac) ac.close();
  }

  function discardDrawn() {
    if (target && target.kind === 'new' && !target.saved && target.box.isConnected) target.box.remove();
  }

  function markThanks(box, text) {
    box.classList.add('suggested');
    const label = box.querySelector('.face-label');
    if (label && !box.dataset.personId) label.textContent = 'Thanks!';
    nameForm.hidden = true;
    thanks.textContent = text;
    thanks.hidden = false;
    thanks.focus({ preventScroll: true });
  }

  nameForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!target) return;
    const typed = nameInput.value.trim();
    if (!picked) {
      showError(nameForm, typed ? 'Pick a name from the list, or choose “someone new”.' : 'Type a name first.');
      return;
    }
    const name = picked.display_name;
    const who = picked.isNew ? { new_person: splitName(name) } : { person_id: picked.id };
    const t = target;
    nameSubmit.disabled = true;
    nameSubmit.setAttribute('aria-busy', 'true');
    showError(nameForm, '');
    try {
      if (t.kind === 'face') {
        await window.CD.api(`/api/photos/${photoId}/suggestions`, {
          method: 'POST', body: Object.assign({ kind: 'person', face_id: Number(t.box.dataset.faceId) }, who),
        });
      } else {
        const r = await window.CD.api(`/api/photos/${photoId}/faces`, {
          method: 'POST', body: Object.assign({ bbox: t.bbox }, who),
        });
        t.saved = true;
        const box = t.box;
        box.classList.remove('drawn');
        box.classList.add('face-unnamed');
        box.dataset.faceId = String(r.face_id);
        box.dataset.state = 'unnamed';
        box.dataset.crop = `/media/faces/${r.face_id}`;
        box.setAttribute('aria-label', `Face you added: ${name}`);
        box.tabIndex = 0;
      }
      markThanks(t.box, `Thank you! We'll check that this is ${name}.`);
      page.dispatchEvent(new CustomEvent('cd:suggested', {
        bubbles: true, detail: { kind: 'person', text: `${name} for a face` },
      }));
    } catch (err) {
      showError(nameForm, errorText(err));
      nameSubmit.disabled = false;
    } finally {
      nameSubmit.removeAttribute('aria-busy');
    }
  });

  disputeOpen.addEventListener('click', () => {
    disputeOpen.hidden = true;
    disputeForm.hidden = false;
    disputeForm.querySelector('textarea').focus();
  });

  disputeForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!target || target.kind !== 'face') return;
    const btn = disputeForm.querySelector('[type="submit"]');
    btn.disabled = true;
    showError(disputeForm, '');
    const box = target.box;
    try {
      await window.CD.api(`/api/faces/${box.dataset.faceId}/dispute`, {
        method: 'POST', body: { note: disputeForm.elements.note.value.trim() },
      });
      box.classList.add('face-disputed');
      const label = box.querySelector('.face-label');
      if (label && !/\?$/.test(label.textContent)) label.textContent += '?';
      disputeForm.hidden = true;
      named.querySelector('p').textContent = "Thanks. We'll take another look at this one.";
      nameInput.focus();
    } catch (err) {
      showError(disputeForm, errorText(err));
    } finally {
      btn.disabled = false;
    }
  });

  panel.querySelector('[data-tag-close]').addEventListener('click', closePanel);
  panel.querySelector('[data-tag-cancel]').addEventListener('click', closePanel);
  bar.querySelector('[data-tag-done]').addEventListener('click', stop);
  if (tagBtn) tagBtn.addEventListener('click', () => (active ? stop() : start()));
  page.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && active && !panel.hidden && !(e.target.closest && e.target.closest('.ac'))) closePanel();
  });

  // ---- tapping boxes / drawing new ones ----------------------------------
  layer.addEventListener('click', (e) => {
    const box = e.target.closest('.face-box');
    if (!box || !active || box.classList.contains('drawn')) return;
    clearCorner();
    openPanel({ kind: 'face', box });
  });

  function fractions(clientX, clientY) {
    const r = layer.getBoundingClientRect();
    return { fx: (clientX - r.left) / r.width, fy: (clientY - r.top) / r.height, r };
  }

  function clearCorner() {
    if (corner) corner.el.remove();
    corner = null;
    barText.textContent = barDefault;
  }

  function newBoxEl() {
    const el = document.createElement('button');
    el.type = 'button';
    el.className = 'face-box drawn';
    el.tabIndex = -1;
    el.innerHTML = '<span class="face-label">New</span>';
    layer.appendChild(el);
    return el;
  }

  function place(el, a, b) {
    el.style.left = pct(Math.min(a.fx, b.fx));
    el.style.top = pct(Math.min(a.fy, b.fy));
    el.style.width = pct(Math.abs(a.fx - b.fx));
    el.style.height = pct(Math.abs(a.fy - b.fy));
  }

  function finishBox(a, b, el) {
    const r = layer.getBoundingClientRect();
    const clamp = (v) => Math.max(0, Math.min(1, v));
    const x0 = clamp(Math.min(a.fx, b.fx)), x1 = clamp(Math.max(a.fx, b.fx));
    const y0 = clamp(Math.min(a.fy, b.fy)), y1 = clamp(Math.max(a.fy, b.fy));
    if ((x1 - x0) * r.width < 14 || (y1 - y0) * r.height < 14) {
      if (el) el.remove();
      window.CD.toast('That box is too small. Draw it around the whole face.', 'error');
      return;
    }
    const box = el || newBoxEl();
    place(box, { fx: x0, fy: y0 }, { fx: x1, fy: y1 });
    const bbox = {
      x: Math.round(x0 * W), y: Math.round(y0 * H),
      w: Math.max(1, Math.round((x1 - x0) * W)), h: Math.max(1, Math.round((y1 - y0) * H)),
    };
    openPanel({ kind: 'new', box, bbox });
  }

  layer.addEventListener('pointerdown', (e) => {
    if (!active || !e.isPrimary || e.target.closest('.face-box')) return;
    down = { x: e.clientX, y: e.clientY, touch: e.pointerType === 'touch', el: null };
    if (!down.touch) {
      e.preventDefault(); // no text/image drag selection
      layer.setPointerCapture(e.pointerId);
    }
  });

  layer.addEventListener('pointermove', (e) => {
    if (!down || down.touch) return;
    if (!down.el && Math.hypot(e.clientX - down.x, e.clientY - down.y) < 8) return;
    clearCorner();
    if (!down.el) down.el = newBoxEl();
    place(down.el, fractions(down.x, down.y), fractions(e.clientX, e.clientY));
  });

  layer.addEventListener('pointerup', (e) => {
    if (!down) return;
    const d = down;
    down = null;
    const moved = Math.hypot(e.clientX - d.x, e.clientY - d.y) >= 8;
    if (d.el) { finishBox(fractions(d.x, d.y), fractions(e.clientX, e.clientY), d.el); return; }
    if (moved) return; // a touch scroll/pan, not a tap
    const p = fractions(e.clientX, e.clientY);
    if (!corner) {
      closePanel();
      const el = document.createElement('span');
      el.className = 'corner-mark';
      el.style.left = pct(p.fx);
      el.style.top = pct(p.fy);
      layer.appendChild(el);
      corner = { fx: p.fx, fy: p.fy, el };
      barText.textContent = 'Now tap the opposite corner of the face.';
    } else {
      const first = corner;
      clearCorner();
      finishBox(first, p, null);
    }
  });

  layer.addEventListener('pointercancel', () => {
    if (down && down.el) down.el.remove();
    down = null;
  });

  window.CD.tagger = {
    start,
    stop,
    active: () => active,
    // True while a gesture on the photo belongs to the tagger.
    busy: () => active && (!!down || !!corner),
  };
})();
