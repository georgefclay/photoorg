// One-tap like toggle for [data-like] buttons.
//
//   <button data-like data-url="/api/photos/12/like" aria-pressed="false">
//     … <span data-like-count>3</span>
//   </button>
//
// Optimistic: the button flips and the count moves immediately; the
// server's answer ({ liked, count }) then wins. On failure the button
// goes back to how it was and the error is announced.
(function () {
  'use strict';

  function setup(btn) {
    const countEl = btn.querySelector('[data-like-count]');
    let busy = false;

    function show(liked, count) {
      btn.setAttribute('aria-pressed', liked ? 'true' : 'false');
      if (countEl) {
        countEl.textContent = String(count);
        countEl.setAttribute('aria-label', `${count} ${count === 1 ? 'like' : 'likes'}`);
      }
    }

    btn.addEventListener('click', async () => {
      if (busy) return;
      busy = true;
      const was = btn.getAttribute('aria-pressed') === 'true';
      const wasCount = Number(countEl ? countEl.textContent : 0) || 0;
      show(!was, Math.max(0, wasCount + (was ? -1 : 1)));
      try {
        const data = await window.CD.api(btn.dataset.url, { method: 'POST' });
        show(!!data.liked, Number(data.count) || 0);
      } catch (err) {
        show(was, wasCount);
        const msg = err.status === 429 ? 'Too many taps this hour. Try again later.'
          : err.status === 404 ? 'This photo is no longer available.'
            : "Couldn't save your like. Check your connection and try again.";
        window.CD.toast(msg, 'error');
      } finally {
        busy = false;
      }
    });
  }

  document.querySelectorAll('[data-like]').forEach(setup);
})();
