// Photo page glue: action panels (date / place), pending-suggestion list,
// comments (post, hide/unhide), moderator "remove from my group", admin
// "rescan wanted", missing-image placeholders, and prev/next by keyboard
// arrows and horizontal swipe. Like → like.js, date field → datefield.js,
// faces → tagger.js.
(function () {
  'use strict';

  const page = document.querySelector('[data-photo-page]');
  if (!page) return;
  const $ = (sel, root) => (root || page).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || page).querySelectorAll(sel));
  const CD = window.CD;
  const photoId = page.dataset.photoId;

  function errorText(err) {
    if (err.status === 429) return "You've sent a lot this hour. Please try again later.";
    if (err.status === 404) return "That isn't available any more. Reload the page.";
    if (err.status === 401) return 'Your session has expired. Reload the page and try again.';
    if (err.status === 403) return (err.body && /moderator|forbidden/.test(String(err.body.error)))
      ? "You're not allowed to do that." : 'Your session has expired. Reload the page and try again.';
    if (err.status === 400 && err.body && err.body.error) {
      const m = String(err.body.error);
      return m.charAt(0).toUpperCase() + m.slice(1);
    }
    return 'Something went wrong. Check your connection and try again.';
  }
  function showError(form, msg) {
    const el = form.querySelector('[data-form-error]');
    if (!el) { if (msg) CD.toast(msg, 'error'); return; }
    el.textContent = msg || '';
    el.hidden = !msg;
  }

  // ---- images that aren't on the server yet ------------------------------
  const frame = $('[data-photo-frame]');
  const img = $('[data-photo-img]');
  function photoMissing() {
    frame.classList.add('missing');
    $('[data-photo-missing]').hidden = false;
  }
  if (img) {
    if (img.complete && img.naturalWidth === 0) photoMissing();
    img.addEventListener('error', photoMissing);
  }
  $$('[data-back-img]').forEach((b) => {
    const fig = b.closest('figure');
    const missing = () => {
      fig.querySelector('[data-back-link]').hidden = true;
      fig.querySelector('[data-back-missing]').hidden = false;
    };
    if (b.complete && b.naturalWidth === 0 && b.currentSrc) missing();
    b.addEventListener('error', missing);
  });

  // ---- action panels -----------------------------------------------------
  const panels = $$('[data-panel]');
  function openPanel(name) {
    panels.forEach((p) => { p.hidden = p.dataset.panel !== name; });
    $$('[data-open-panel]').forEach((b) => {
      if (b.hasAttribute('aria-expanded')) b.setAttribute('aria-expanded', String(b.dataset.openPanel === name));
    });
    const p = panels.find((x) => x.dataset.panel === name);
    if (!p) return;
    if (CD.tagger && CD.tagger.active()) CD.tagger.stop();
    resetPanel(p);
    p.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    const input = p.querySelector('input');
    if (input) input.focus({ preventScroll: true });
  }
  function closePanels() {
    panels.forEach((p) => { p.hidden = true; });
    $$('[data-open-panel][aria-expanded]').forEach((b) => b.setAttribute('aria-expanded', 'false'));
  }
  function resetPanel(p) {
    const form = p.querySelector('form');
    form.hidden = false;
    p.querySelector('[data-thanks]').hidden = true;
    showError(form, '');
  }
  $$('[data-open-panel]').forEach((b) => b.addEventListener('click', (e) => {
    e.preventDefault();
    const name = b.dataset.openPanel;
    const p = panels.find((x) => x.dataset.panel === name);
    if (b.getAttribute('aria-expanded') === 'true' && p && !p.hidden) closePanels();
    else openPanel(name);
  }));
  $$('[data-close-panel]').forEach((b) => b.addEventListener('click', closePanels));
  $$('[data-again]').forEach((b) => b.addEventListener('click', () => {
    const p = b.closest('[data-panel]');
    resetPanel(p);
    p.querySelector('input').focus();
  }));
  if (location.hash === '#panel-date' || location.hash === '#panel-place') openPanel(location.hash.slice(7));

  // ---- pending suggestions + thank-you -----------------------------------
  page.addEventListener('cd:suggested', (e) => {
    const { text } = e.detail || {};
    const section = $('[data-pending]');
    const li = document.createElement('li');
    li.className = 'mine fresh';
    const who = document.createElement('strong');
    who.textContent = 'You';
    li.append(who, ` suggested ${text}`);
    $('[data-pending-list]').appendChild(li);
    section.hidden = false;
    const p = e.target.closest && e.target.closest('[data-panel]');
    if (p) {
      p.querySelector('form').hidden = true;
      const thanks = p.querySelector('[data-thanks]');
      thanks.querySelector('[data-thanks-text]').textContent =
        `Thank you! You suggested ${text}. Someone will check it soon.`;
      thanks.hidden = false;
      thanks.focus({ preventScroll: true });
    }
  });

  // ---- suggest a place -----------------------------------------------------
  const placeForm = $('[data-place-form]');
  if (placeForm && CD.autocomplete) {
    const input = $('[data-place-input]', placeForm);
    const submit = $('[data-place-submit]', placeForm);
    let picked = null;
    const ac = CD.autocomplete(input, {
      url: '/api/places/autocomplete?q=',
      label: (p) => p.name,
      allowNew: (q) => ({ name: q }),
      newLabel: (q) => `Add “${q}” as a new place`,
      onPick: (item) => { picked = item; submit.disabled = !item; showError(placeForm, ''); },
    });
    placeForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (!picked) { showError(placeForm, 'Pick a place from the list, or add a new one.'); return; }
      const body = picked.isNew ? { kind: 'place', new_place: { name: picked.name } } : { kind: 'place', place_id: picked.id };
      submit.disabled = true;
      submit.setAttribute('aria-busy', 'true');
      try {
        await CD.api(placeForm.dataset.postUrl, { method: 'POST', body });
        const name = picked.name;
        ac.clear();
        picked = null;
        placeForm.dispatchEvent(new CustomEvent('cd:suggested', { bubbles: true, detail: { kind: 'place', text: `${name} as the place` } }));
      } catch (err) {
        showError(placeForm, errorText(err));
        submit.disabled = false;
      } finally {
        submit.removeAttribute('aria-busy');
      }
    });
  }

  // ---- "Who is this?" chip → tagging ----------------------------------------
  $$('[data-start-tagging]').forEach((b) => b.addEventListener('click', () => {
    closePanels();
    if (CD.tagger) CD.tagger.start();
  }));
  const tagBtn = $('[data-action="tag"]');
  if (tagBtn) tagBtn.addEventListener('click', closePanels);

  // ---- admin: rescan wanted ------------------------------------------------
  const rescan = $('[data-rescan]');
  if (rescan) {
    rescan.addEventListener('click', async () => {
      const want = rescan.getAttribute('aria-pressed') !== 'true';
      rescan.setAttribute('aria-pressed', String(want));
      rescan.disabled = true;
      try {
        const r = await CD.api(rescan.dataset.url, { method: 'POST', body: { wanted: want } });
        rescan.setAttribute('aria-pressed', String(!!r.rescan_wanted));
        CD.toast(r.rescan_wanted ? 'Added to the rescan list.' : 'Removed from the rescan list.');
      } catch (err) {
        rescan.setAttribute('aria-pressed', String(!want));
        CD.toast(errorText(err), 'error');
      } finally {
        rescan.disabled = false;
      }
    });
  }

  // ---- moderators: remove from group ---------------------------------------
  $$('[data-remove-group]').forEach((btn) => btn.addEventListener('click', async () => {
    const name = btn.dataset.groupName;
    if (!window.confirm(`Remove this photo from ${name}? People who only see it through ${name} won't see it any more.`)) return;
    btn.disabled = true;
    try {
      const r = await CD.api(btn.dataset.url, { method: 'POST' });
      const li = btn.closest('[data-group-item]');
      li.textContent = '';
      const note = document.createElement('span');
      note.className = 'muted';
      note.textContent = `Removed from ${name}.${r.unfiled ? ' It is no longer in any group.' : ''}`;
      li.appendChild(note);
      CD.toast(`Removed from ${name}.`);
    } catch (err) {
      btn.disabled = false;
      CD.toast(errorText(err), 'error');
    }
  }));

  // ---- comments ------------------------------------------------------------
  const list = $('[data-comment-list]');
  const canModerate = page.dataset.canModerate === '1';

  function toggleButton(hidden) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'btn ghost small';
    b.dataset.commentToggle = '';
    b.dataset.hidden = hidden ? '1' : '';
    b.textContent = hidden ? 'Unhide' : 'Hide';
    return b;
  }

  function commentEl(id, author, body) {
    const li = document.createElement('li');
    li.className = 'comment fresh';
    li.dataset.commentId = String(id);
    const head = document.createElement('div');
    head.className = 'comment-head';
    const av = document.createElement('span');
    av.className = 'avatar';
    av.setAttribute('aria-hidden', 'true');
    av.textContent = author.split(/[\s._-]+/).filter(Boolean).slice(0, 2).map((s) => s[0]).join('').toUpperCase() || '?';
    const who = document.createElement('strong');
    who.textContent = author;
    const when = document.createElement('time');
    when.className = 'muted small-text';
    when.dateTime = new Date().toISOString();
    when.textContent = 'just now';
    head.append(av, who, when);
    const p = document.createElement('p');
    p.className = 'comment-body';
    p.textContent = body;
    const note = document.createElement('p');
    note.className = 'hidden-note';
    note.dataset.hiddenNote = '';
    note.hidden = true;
    note.textContent = 'Hidden. Only moderators can see this.';
    li.append(head, p, note);
    if (canModerate) li.appendChild(toggleButton(false));
    return li;
  }

  const commentForm = $('[data-comment-form]');
  if (commentForm) {
    commentForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const ta = commentForm.elements.body;
      const body = ta.value.trim();
      if (!body) { showError(commentForm, 'Write something first.'); ta.focus(); return; }
      const btn = commentForm.querySelector('[type="submit"]');
      btn.disabled = true;
      btn.setAttribute('aria-busy', 'true');
      showError(commentForm, '');
      try {
        const r = await CD.api(commentForm.dataset.url, { method: 'POST', body: { body } });
        list.appendChild(commentEl(r.id, page.dataset.me || 'You', body));
        $('[data-comments-empty]').hidden = true;
        ta.value = '';
        CD.toast('Comment posted.');
      } catch (err) {
        showError(commentForm, errorText(err));
      } finally {
        btn.disabled = false;
        btn.removeAttribute('aria-busy');
      }
    });
  }

  if (list) {
    list.addEventListener('click', async (e) => {
      const btn = e.target.closest('[data-comment-toggle]');
      if (!btn) return;
      const li = btn.closest('[data-comment-id]');
      const hide = !btn.dataset.hidden;
      btn.disabled = true;
      try {
        await CD.api(`/api/comments/${li.dataset.commentId}/${hide ? 'hide' : 'unhide'}`, { method: 'POST' });
        li.classList.toggle('is-hidden', hide);
        li.querySelector('[data-hidden-note]').hidden = !hide;
        btn.dataset.hidden = hide ? '1' : '';
        btn.textContent = hide ? 'Unhide' : 'Hide';
      } catch (err) {
        CD.toast(errorText(err), 'error');
      } finally {
        btn.disabled = false;
      }
    });
  }

  // ---- prev / next: arrows and swipe ---------------------------------------
  const prev = page.dataset.prev;
  const next = page.dataset.next;
  function go(url) { if (url) window.location.href = url; }

  document.addEventListener('keydown', (e) => {
    if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    const t = e.target;
    if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
    if (CD.tagger && CD.tagger.active()) return; // don't lose a half-tagged face
    go(e.key === 'ArrowLeft' ? prev : next);
  });

  let touch = null;
  page.addEventListener('touchstart', (e) => {
    touch = null;
    if (e.touches.length !== 1) return;
    const t = e.target;
    // Swipes count only on the photo itself, not while reading or typing below it.
    if (!t.closest('[data-photo-frame]')) return;
    // Never steal a gesture from the tagger, or from someone zoomed in.
    if (CD.tagger && CD.tagger.active() && t.closest('[data-photo-frame]')) return;
    if (window.visualViewport && window.visualViewport.scale > 1.05) return;
    touch = { x: e.touches[0].clientX, y: e.touches[0].clientY, t: Date.now() };
  }, { passive: true });
  page.addEventListener('touchmove', (e) => { if (touch && e.touches.length > 1) touch = null; }, { passive: true });
  page.addEventListener('touchend', (e) => {
    if (!touch) return;
    const c = e.changedTouches[0];
    const dx = c.clientX - touch.x;
    const dy = c.clientY - touch.y;
    const quick = Date.now() - touch.t < 700;
    touch = null;
    if (quick && Math.abs(dx) > 70 && Math.abs(dx) > 2 * Math.abs(dy)) go(dx < 0 ? next : prev);
  }, { passive: true });

  window.CD.photoPage = { id: photoId, openPanel, closePanels };
})();
