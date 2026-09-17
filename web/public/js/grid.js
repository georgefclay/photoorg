// Infinite scroll for [data-grid] photo grids (views/partials/photo-grid.ejs).
// The "More photos" link keeps working without JavaScript; with it, the
// next page is fetched from data-api + cursor and appended in place.
(function () {
  'use strict';

  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];

  function dateText(p) {
    if (!p.capture_date) return null;
    const [y, m, d] = p.capture_date.split('-').map(Number);
    if (!p.capture_date_confirmed && p.capture_date_precision === 'decade') {
      const d0 = Math.floor(y / 10) * 10;
      return `${d0}–${d0 + 9}`;
    }
    switch (p.capture_date_precision) {
      case 'exact': return `${d} ${MONTHS[m - 1]} ${y}`;
      case 'month': return `${MONTHS[m - 1]} ${y}`;
      case 'decade': return `${Math.floor(y / 10) * 10}s`;
      default: return String(y);
    }
  }

  const HEART = '<svg class="icon icon-heart" viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="M12 20s-7.5-4.6-9.3-9.2C1.5 7.4 3.8 4 7.2 4c2 0 3.6 1.1 4.8 2.8C13.2 5.1 14.8 4 16.8 4c3.4 0 5.7 3.4 4.5 6.8C19.5 15.4 12 20 12 20z"/></svg>';

  // Must match views/partials/tile.ejs.
  function tileHtml(p, from) {
    const t = dateText(p);
    const alt = t ? `Photo, ${p.capture_date_confirmed ? '' : 'about '}${t}` : 'Undated photo';
    const badge = p.like_count ? `<span class="tile-badge">${HEART}${p.like_count}</span>` : '';
    return `<li><a class="tile" href="/photos/${p.id}?from=${encodeURIComponent(from)}"><img src="${esc(p.thumb_url)}" alt="${esc(alt)}" width="320" height="320" loading="lazy" decoding="async">${badge}</a></li>`;
  }

  function setup(grid) {
    const more = grid.parentElement.querySelector('[data-grid-more]');
    if (!more) return;
    const api = grid.dataset.api;
    const from = grid.dataset.from || 'b.recent';
    let next = grid.dataset.next;
    let loading = false;
    let observer = null;

    async function load() {
      if (loading || !next) return;
      loading = true;
      more.setAttribute('aria-busy', 'true');
      more.textContent = 'Loading…';
      const skeletons = [];
      for (let i = 0; i < 6; i++) {
        const li = document.createElement('li');
        li.innerHTML = '<span class="tile skeleton" aria-hidden="true"></span>';
        grid.appendChild(li);
        skeletons.push(li);
      }
      try {
        const url = `${api}${api.includes('?') ? '&' : '?'}cursor=${encodeURIComponent(next)}`;
        const data = await window.CD.api(url);
        skeletons.forEach((li) => li.remove());
        grid.insertAdjacentHTML('beforeend', (data.items || data.photos || []).map((p) => tileHtml(p, from)).join(''));
        next = data.next;
        if (!next) {
          more.parentElement.remove();
          if (observer) observer.disconnect();
        } else {
          more.href = more.href.replace(/([?&]cursor=)[^&]*/, `$1${encodeURIComponent(next)}`);
        }
      } catch (err) {
        skeletons.forEach((li) => li.remove());
        window.CD.toast("Couldn't load more photos. Tap to try again.", 'error');
      } finally {
        loading = false;
        more.removeAttribute('aria-busy');
        if (more.isConnected) more.textContent = 'More photos';
      }
    }

    more.addEventListener('click', (e) => { e.preventDefault(); load(); });
    if ('IntersectionObserver' in window) {
      observer = new IntersectionObserver((entries) => {
        if (entries.some((en) => en.isIntersecting)) load();
      }, { rootMargin: '800px 0px' });
      observer.observe(more);
    }
  }

  document.querySelectorAll('[data-grid]').forEach(setup);
  window.CD = window.CD || {};
  window.CD.tileHtml = tileHtml;
})();
