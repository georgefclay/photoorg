// /who-is-this — infinite scroll over /api/faces/unknown (keyset cursor)
// and the inline "I know who this is" → name autocomplete → person
// suggestion flow. Without JavaScript the page still lists faces, the
// "More faces" link pages through them and "I know who this is" opens
// the photo page.
(function () {
  'use strict';

  const feed = document.querySelector('[data-wi-feed]');
  if (!feed) return;

  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
  const svg = (d) => `<svg class="icon" viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${d}</svg>`;
  const ICON_CAL = svg('<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>');
  const ICON_CHECK = svg('<path d="M5 12l5 5 9-10"/>');
  const THANKS = `<p class="wi-thanks" data-wi-thanks tabindex="-1">${ICON_CHECK} <span>Thanks — an admin will check it</span></p>`;
  let formSeq = 0;

  // Mirrors services/format.captureDate for the card caption.
  function dateText(f) {
    if (!f.capture_date) return 'No date yet';
    const [y, m, d] = f.capture_date.split('-').map(Number);
    const d0 = Math.floor(y / 10) * 10;
    const guess = !f.capture_date_confirmed;
    switch (f.capture_date_precision) {
      case 'exact': return `${guess ? 'About ' : ''}${d} ${MONTHS[m - 1]} ${y}`;
      case 'month': return `${guess ? 'About ' : ''}${MONTHS[m - 1]} ${y}`;
      case 'decade': return guess ? `About ${d0}–${d0 + 9}` : `${d0}s`;
      case 'unknown': return guess ? `Around ${y}` : String(y);
      default: return `${guess ? 'About ' : ''}${y}`;
    }
  }

  function boxStyle(f) {
    const b = f.bbox || {};
    const W = Number(f.width), H = Number(f.height);
    const x = Number(b.x), y = Number(b.y), w = Number(b.w), h = Number(b.h);
    if (!(W > 0 && H > 0) || ![x, y, w, h].every(Number.isFinite) || w <= 0 || h <= 0) return null;
    const pct = (v, dd) => Math.max(0, Math.min(100, (v / dd) * 100));
    const left = pct(x, W), top = pct(y, H);
    const r = (v) => +v.toFixed(2);
    return `left: ${r(left)}%; top: ${r(top)}%; width: ${r(Math.min(pct(w, W), 100 - left))}%; height: ${r(Math.min(pct(h, H), 100 - top))}%`;
  }

  // Must match views/partials/who-card.ejs.
  function cardHtml(f) {
    const href = `/photos/${f.photo_id}?from=wi.recent`;
    const ar = f.width > 0 && f.height > 0 ? +(f.width / f.height).toFixed(4) : 1.5;
    const box = boxStyle(f);
    const dims = f.width && f.height ? ` width="${Number(f.width)}" height="${Number(f.height)}"` : '';
    const action = f.suggested_by_me
      ? THANKS
      : `<a class="btn block" href="${href}" data-identify>I know who this is</a>`;
    return `<li class="card wi-card" data-face="${Number(f.id)}" data-photo="${Number(f.photo_id)}">
  <div class="wi-media">
    <span class="wi-crop"><img src="${esc(f.crop_url)}" alt="A face nobody has named yet" width="256" height="256" loading="lazy" decoding="async"></span>
    <a class="wi-photo" href="${href}" style="--ar: ${ar}">
      <img src="${esc(f.thumb_url)}" alt="The whole photo — open it"${dims} loading="lazy" decoding="async">
      ${box ? `<span class="wi-box" style="${box}" aria-hidden="true"></span>` : ''}
    </a>
  </div>
  <p class="wi-meta">${ICON_CAL} <span>${esc(dateText(f))}</span> <a href="${href}">Open photo</a></p>
  <div class="wi-action" data-wi-action>${action}</div>
</li>`;
  }

  // ---- "I know who this is" -------------------------------------------------
  function splitName(full) {
    const parts = full.trim().split(/\s+/).filter(Boolean);
    if (parts.length < 2) return { given_name: parts[0] || full.trim(), surname: null };
    return { given_name: parts.slice(0, -1).join(' '), surname: parts[parts.length - 1] };
  }

  function openForm(card) {
    const action = card.querySelector('[data-wi-action]');
    if (!action || action.querySelector('form')) return;
    const faceId = Number(card.dataset.face);
    const photoId = Number(card.dataset.photo);
    const id = `wi-name-${++formSeq}`;
    const original = action.innerHTML;
    action.innerHTML = `<form class="wi-form" novalidate>
  <div class="field">
    <label for="${id}">Who is this?</label>
    <input type="text" id="${id}" name="name" placeholder="Start typing a name" autocomplete="off" autocapitalize="words" enterkeyhint="done">
    <span class="hint">Pick them from the list, or add someone new.</span>
  </div>
  <p class="wi-err notice error" role="alert" hidden></p>
  <div class="actions">
    <button type="submit" class="btn">Send</button>
    <button type="button" class="btn ghost" data-cancel>Cancel</button>
  </div>
</form>`;
    const formEl = action.querySelector('form');
    const input = formEl.querySelector('input');
    const errEl = formEl.querySelector('.wi-err');
    const sendBtn = formEl.querySelector('[type="submit"]');
    let picked = null;

    const ac = window.CD.autocomplete(input, {
      url: '/api/people/autocomplete?q=',
      label: (p) => p.display_name,
      detail: (p) => {
        if (p.birth_year == null && p.death_year == null) return null;
        return `${p.birth_year != null ? p.birth_year : ''}–${p.death_year != null ? p.death_year : ''}`;
      },
      allowNew: (q) => ({ display_name: q }),
      newLabel: (q) => `Someone new: “${q}”`,
      onPick: (item) => { picked = item; errEl.hidden = true; },
    });

    function showError(msg) {
      errEl.textContent = msg;
      errEl.hidden = false;
    }

    formEl.querySelector('[data-cancel]').addEventListener('click', () => {
      ac.destroy();
      action.innerHTML = original;
      const btn = action.querySelector('[data-identify]');
      if (btn) btn.focus();
    });

    formEl.addEventListener('submit', async (e) => {
      e.preventDefault();
      const typed = input.value.trim();
      if (!typed) { showError('Type a name first.'); input.focus(); return; }
      let body;
      if (picked && !picked.isNew && picked.id && input.dataset.pickedId === String(picked.id)) {
        body = { kind: 'person', face_id: faceId, person_id: picked.id };
      } else {
        // Typed without picking, or "Someone new": the name travels inside
        // the suggestion; nothing is created until an admin accepts it.
        const np = splitName(typed);
        body = { kind: 'person', face_id: faceId, new_person: { given_name: np.given_name, surname: np.surname, display_name: typed } };
      }
      sendBtn.disabled = true;
      sendBtn.setAttribute('aria-busy', 'true');
      try {
        await window.CD.api(`/api/photos/${photoId}/suggestions`, { method: 'POST', body });
        ac.destroy();
        action.innerHTML = THANKS;
        const t = action.querySelector('[data-wi-thanks]');
        if (t) t.focus();
      } catch (err) {
        sendBtn.disabled = false;
        sendBtn.removeAttribute('aria-busy');
        if (err.status === 429) showError("You've sent a lot of suggestions this hour. Please try again a bit later.");
        else if (err.status === 404) showError("This photo isn't available any more.");
        else if (err.status === 401 || err.status === 403) showError('Your sign-in has expired. Reload the page and try again.');
        else if (!err.status) showError("Couldn't reach the server. Check your connection and try again.");
        else showError("Couldn't send that just now. Please try again.");
      }
    });

    input.focus();
  }

  feed.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-identify]');
    if (!btn) return;
    e.preventDefault();
    openForm(btn.closest('.wi-card'));
  });

  // ---- infinite scroll ---------------------------------------------------------
  const more = document.querySelector('[data-wi-more]');
  if (!more) return;
  const api = feed.dataset.api;
  let next = feed.dataset.next;
  let loading = false;
  let observer = null;

  async function load() {
    if (loading || !next) return;
    loading = true;
    more.setAttribute('aria-busy', 'true');
    more.textContent = 'Loading…';
    const skeletons = [];
    for (let i = 0; i < 2; i++) {
      const li = document.createElement('li');
      li.className = 'card wi-card skeleton-card skeleton';
      li.setAttribute('aria-hidden', 'true');
      feed.appendChild(li);
      skeletons.push(li);
    }
    try {
      const data = await window.CD.api(`${api}${api.includes('?') ? '&' : '?'}cursor=${encodeURIComponent(next)}`);
      skeletons.forEach((li) => li.remove());
      const seen = new Set(Array.from(feed.querySelectorAll('[data-face]')).map((el) => el.dataset.face));
      feed.insertAdjacentHTML('beforeend', (data.items || []).filter((f) => !seen.has(String(f.id))).map(cardHtml).join(''));
      next = data.next;
      if (!next) {
        more.parentElement.remove();
        if (observer) observer.disconnect();
      } else {
        more.href = `/who-is-this?cursor=${encodeURIComponent(next)}`;
      }
    } catch (err) {
      skeletons.forEach((li) => li.remove());
      window.CD.toast("Couldn't load more faces. Tap “More faces” to try again.", 'error');
    } finally {
      loading = false;
      more.removeAttribute('aria-busy');
      if (more.isConnected) more.textContent = 'More faces';
    }
  }

  more.addEventListener('click', (e) => { e.preventDefault(); load(); });
  if ('IntersectionObserver' in window) {
    observer = new IntersectionObserver((entries) => {
      if (entries.some((en) => en.isIntersecting)) load();
    }, { rootMargin: '600px 0px' });
    observer.observe(more);
  }
})();
