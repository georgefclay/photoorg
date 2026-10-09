// People pages: live filtering + "More people" on /people, the
// "Suggest a relationship" form on /people/:id, and quiet placeholders for
// missing album covers. Everything here enhances plain HTML that already
// works without JavaScript.
(function () {
  'use strict';

  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const num = (n) => Number(n).toLocaleString('en-US');
  const plural = (n, one, many) => `${num(n)} ${Number(n) === 1 ? one : (many || `${one}s`)}`;

  function yearSpan(a, b) {
    if (a == null && b == null) return null;
    if (a != null && b != null) return a === b ? String(a) : `${a}–${b}`;
    return a != null ? `${a}–` : `–${b}`;
  }
  function lifeSpan(p) {
    if (p.birth_year != null && p.death_year != null) return `${p.birth_year}–${p.death_year}`;
    if (p.birth_year != null) return `born ${p.birth_year}`;
    if (p.death_year != null) return `died ${p.death_year}`;
    return null;
  }

  // Must match views/people-row.ejs.
  function rowHtml(p) {
    const words = String(p.display_name || '?').split(/\s+/).filter(Boolean);
    const initials = ((words[0] || '?')[0] + (words.length > 1 ? words[words.length - 1][0] : '')).toUpperCase();
    const life = lifeSpan(p);
    const years = yearSpan(p.year_min, p.year_max);
    const photos = p.photo_count
      ? plural(p.photo_count, 'photo')
        + (p.face_count > p.photo_count ? ` (${plural(p.face_count, 'face')})` : '')
        + (years ? `, ${years}` : '')
      : 'No photos you can see yet';
    return `<li><a class="list-link person-row${p.photo_count ? '' : ' no-photos'}" href="/people/${p.id}">`
      + `<span class="person-avatar" aria-hidden="true">${esc(initials)}</span>`
      + `<span class="person-text"><span class="person-name">${esc(p.display_name)}</span>`
      + `<span class="person-sub">${life ? `${esc(life)} · ` : ''}${esc(photos)}</span></span></a></li>`;
  }

  // ---- /people ----------------------------------------------------------------
  const find = document.querySelector('[data-people-find]');
  const results = document.querySelector('[data-people-results]');
  const more = document.querySelector('[data-people-more]');
  const moreWrap = document.querySelector('[data-people-more-wrap]');

  if (find && results && more && moreWrap) {
    const input = find.querySelector('[data-people-q]');
    const api = find.dataset.api;
    let next = more.dataset.next || '';
    let q = input.value.trim();
    let ctrl = null;
    let timer = null;

    function setNext(cursor) {
      next = cursor || '';
      moreWrap.hidden = !next;
      const u = new URLSearchParams();
      if (q) u.set('q', q);
      if (next) u.set('cursor', next);
      more.href = `/people?${u}`;
    }

    function emptyHtml() {
      const title = q ? `No one called “${esc(q)}”` : 'No people yet';
      const text = q ? 'Try a first name, a surname, a maiden name or a nickname.'
        : 'People appear here once faces in the photos have been named.';
      return `<div class="empty"><h2>${title}</h2><p>${text}</p></div>`;
    }

    async function fetchPage(cursor, signal) {
      const u = `${api}&q=${encodeURIComponent(q)}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`;
      return window.CD.api(u, { signal });
    }

    async function refresh() {
      if (ctrl) ctrl.abort();
      ctrl = new AbortController();
      results.setAttribute('aria-busy', 'true');
      try {
        const data = await fetchPage(null, ctrl.signal);
        results.innerHTML = data.items.length
          ? `<ul class="list people-list" data-people-list aria-label="People">${data.items.map(rowHtml).join('')}</ul>`
          : emptyHtml();
        setNext(data.next);
        const url = q ? `/people?q=${encodeURIComponent(q)}` : '/people';
        if (window.history && history.replaceState) history.replaceState(null, '', url);
      } catch (err) {
        if (err.name !== 'AbortError') window.CD.toast("Couldn't search people right now.", 'error');
      } finally {
        results.removeAttribute('aria-busy');
      }
    }

    input.addEventListener('input', () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        const v = input.value.trim();
        if (v === q) return;
        q = v;
        refresh();
      }, 250);
    });
    find.addEventListener('submit', (e) => {
      e.preventDefault();
      clearTimeout(timer);
      q = input.value.trim();
      refresh();
    });

    let loading = false;
    more.addEventListener('click', async (e) => {
      e.preventDefault();
      if (loading || !next) return;
      loading = true;
      more.setAttribute('aria-busy', 'true');
      more.textContent = 'Loading…';
      try {
        const data = await fetchPage(next);
        const list = results.querySelector('[data-people-list]');
        if (list) list.insertAdjacentHTML('beforeend', data.items.map(rowHtml).join(''));
        setNext(data.next);
      } catch (err) {
        window.CD.toast("Couldn't load more people. Tap to try again.", 'error');
      } finally {
        loading = false;
        more.removeAttribute('aria-busy');
        more.textContent = 'More people';
      }
    });
  }

  // ---- /people/:id — Suggest a relationship ---------------------------------
  const form = document.querySelector('[data-rel-form]');
  if (form && window.CD && window.CD.autocomplete) {
    const personId = Number(form.dataset.personId);
    const other = form.querySelector('[data-rel-other]');
    const otherId = form.querySelector('[data-rel-other-id]');
    const thanks = document.querySelector('[data-rel-thanks]');
    const pendingWrap = document.querySelector('[data-pending-wrap]');
    const pendingList = document.querySelector('[data-pending-list]');
    const WORDS = { parent: 'parent', child: 'child', spouse: 'spouse', sibling: 'brother or sister' };
    let picked = null;

    window.CD.autocomplete(other, {
      source: async (q, signal) => {
        const data = await window.CD.api(`/api/people/autocomplete?q=${encodeURIComponent(q)}`, { signal });
        return (data.items || []).filter((p) => p.id !== personId);
      },
      detail: (p) => yearSpan(p.birth_year, p.death_year),
      onPick: (item) => {
        picked = item;
        otherId.value = item ? String(item.id) : '';
      },
    });

    function say(html, kind) {
      if (thanks) thanks.innerHTML = `<p class="notice ${kind}">${html}</p>`;
    }

    form.addEventListener('submit', async (e) => {
      // Without a picked person, let the server match the typed name.
      if (!picked) return;
      const relation = (form.querySelector('input[name="relation"]:checked') || {}).value;
      if (!relation) return; // native `required` shows the message
      e.preventDefault();
      const who = picked.id;
      const body = relation === 'parent'
        ? { person_a_id: who, person_b_id: personId, type: 'parent' }
        : relation === 'child'
          ? { person_a_id: personId, person_b_id: who, type: 'parent' }
          : { person_a_id: personId, person_b_id: who, type: relation };
      const btn = form.querySelector('button[type="submit"]');
      btn.setAttribute('aria-busy', 'true');
      btn.disabled = true;
      try {
        const res = await window.CD.api('/api/relationships', { method: 'POST', body });
        if (res && res.duplicate) {
          say('Someone has already suggested that. It is waiting for review.', 'info');
        } else {
          say(`Thank you! Your suggestion about ${esc(picked.display_name)} is waiting for review.`, 'ok');
          if (pendingList && pendingWrap) {
            pendingList.insertAdjacentHTML('beforeend',
              `<li><a href="/people/${who}">${esc(picked.display_name)}</a> — their ${esc(WORDS[relation])}</li>`);
            pendingWrap.hidden = false;
          }
        }
        form.reset();
        otherId.value = '';
        picked = null;
      } catch (err) {
        if (err.status === 409) say('We already have that relationship recorded. Thank you!', 'info');
        else say("Sorry, that didn't work. Please try again.", 'error');
      } finally {
        btn.removeAttribute('aria-busy');
        btn.disabled = false;
      }
    });
  }

  // ---- admin person editor (Phase 15) --------------------------------------
  // Names are human-visible text, so an admin corrects them here rather
  // than by hand in SQL. The save PATCHes only the fields on the form, and
  // the server stamps `edited_on_web_at` so the laptop's next push keeps
  // this wording instead of handing the old one back.
  const pe = document.querySelector('[data-person-edit]');
  if (pe) {
    const personId = pe.dataset.personId;
    const form = pe.querySelector('[data-person-form]');
    const openBtn = pe.querySelector('[data-act="open-edit"]');
    const cancelBtn = pe.querySelector('[data-act="cancel-edit"]');
    const errorEl = form.querySelector('[data-form-error]');
    const statusEl = form.querySelector('.status');

    function setError(msg) {
      errorEl.textContent = msg || '';
      errorEl.hidden = !msg;
    }

    openBtn.addEventListener('click', () => {
      form.hidden = false;
      openBtn.hidden = true;
      form.querySelector('input').focus();
    });
    cancelBtn.addEventListener('click', () => {
      form.reset();
      setError('');
      statusEl.textContent = '';
      form.hidden = true;
      openBtn.hidden = false;
    });

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      setError('');
      const body = {};
      for (const el of form.elements) {
        if (!el.name) continue;
        body[el.name] = el.value;
      }
      const btn = form.querySelector('[type="submit"]');
      btn.disabled = true;
      statusEl.textContent = 'Saving…';
      try {
        const r = await CD.api(`/api/people/${personId}`, { method: 'PATCH', body });
        statusEl.textContent = r.changed ? 'Saved' : 'No change';
        if (r.changed) {
          CD.toast('Details saved.');
          // The page's headings and facts list are server-rendered from
          // the same row; reload rather than keep a second renderer in
          // step with the first.
          window.setTimeout(() => window.location.reload(), 700);
        }
      } catch (err) {
        setError(err.message || 'That did not save.');
        statusEl.textContent = '';
      } finally {
        btn.disabled = false;
      }
    });

    const addForm = pe.querySelector('[data-variant-add]');
    if (addForm) {
      addForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const variant = addForm.elements.variant.value.trim();
        if (!variant) return;
        const btn = addForm.querySelector('[type="submit"]');
        btn.disabled = true;
        try {
          await CD.api(`/api/people/${personId}/variants`, {
            method: 'POST',
            body: { variant, kind: addForm.elements.kind.value },
          });
          window.location.reload();
        } catch (err) {
          CD.toast(err.message || 'That variant did not save.', 'error');
          btn.disabled = false;
        }
      });
    }

    const variantList = pe.querySelector('[data-variant-list]');
    if (variantList) {
      // Removal is a soft-delete on the server (fix-up 1): the row keeps
      // its id so the flag can travel to the laptop, and re-adding the
      // same name brings it back rather than failing on the key.
      variantList.addEventListener('click', async (e) => {
        const rm = e.target.closest('[data-act="remove-variant"]');
        if (rm) {
          const li = rm.closest('[data-variant-id]');
          const name = li.querySelector('[data-variant-text]').textContent;
          if (!window.confirm(`Remove "${name}" as a name for this person?`)) return;
          rm.disabled = true;
          try {
            await CD.api(`/api/people/${personId}/variants/${li.dataset.variantId}`,
              { method: 'DELETE' });
            li.remove();
            CD.toast('Name variant removed.');
          } catch (err) {
            CD.toast(err.message || 'That did not save.', 'error');
            rm.disabled = false;
          }
          return;
        }
        const btn = e.target.closest('[data-act="edit-variant"]');
        if (!btn) return;
        const li = btn.closest('[data-variant-id]');
        const textEl = li.querySelector('[data-variant-text]');
        const next = window.prompt('Correct this name variant:', textEl.textContent);
        if (next == null) return;
        const variant = next.trim();
        if (!variant || variant === textEl.textContent) return;
        btn.disabled = true;
        try {
          await CD.api(`/api/people/${personId}/variants/${li.dataset.variantId}`, {
            method: 'PATCH', body: { variant },
          });
          textEl.textContent = variant;
          CD.toast('Variant corrected.');
        } catch (err) {
          CD.toast(err.message || 'That did not save.', 'error');
        } finally {
          btn.disabled = false;
        }
      });
    }
  }

  // ---- /albums — a cover whose file isn't on the server yet -----------------
  function coverFailed(img) {
    const cover = img.closest('.album-cover');
    if (!cover) return;
    cover.classList.add('missing');
    img.remove();
  }
  document.addEventListener('error', (e) => {
    const img = e.target;
    if (img && img.tagName === 'IMG' && img.closest) coverFailed(img);
  }, true);
  document.querySelectorAll('.album-cover img').forEach((img) => {
    if (img.complete && img.naturalWidth === 0) coverFailed(img);
  });
})();
