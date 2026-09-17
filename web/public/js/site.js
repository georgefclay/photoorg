// Site-wide enhancements (every page). No dependencies.
//
//   window.CD.csrf()           → Promise<string>  (from /api/csrf, cached)
//   window.CD.api(url, opts)   → fetch JSON with CSRF header; throws Error with .status/.body
//   window.CD.toast(msg, kind) → brief status message (role=status)
(function () {
  'use strict';

  let csrfPromise = null;
  function csrf() {
    if (!csrfPromise) {
      csrfPromise = fetch('/api/csrf', { credentials: 'same-origin' })
        .then((r) => (r.ok ? r.json() : { csrfToken: '' }))
        .then((j) => j.csrfToken || '');
    }
    return csrfPromise;
  }

  async function api(url, opts = {}) {
    const method = (opts.method || 'GET').toUpperCase();
    const headers = Object.assign({ Accept: 'application/json' }, opts.headers || {});
    let body = opts.body;
    if (method !== 'GET' && method !== 'HEAD') {
      headers['X-CSRF-Token'] = await csrf();
      if (body && !(body instanceof FormData) && typeof body !== 'string') {
        headers['Content-Type'] = 'application/json';
        body = JSON.stringify(body);
      }
    }
    const res = await fetch(url, { method, headers, body, credentials: 'same-origin', signal: opts.signal });
    let data = null;
    const text = await res.text();
    try { data = text ? JSON.parse(text) : null; } catch { data = { error: text }; }
    if (!res.ok) {
      const err = new Error((data && data.error) || `Request failed (${res.status})`);
      err.status = res.status;
      err.body = data;
      throw err;
    }
    return data;
  }

  let toastEl = null;
  let toastTimer = null;
  function toast(message, kind) {
    if (!toastEl) {
      toastEl = document.createElement('div');
      toastEl.className = 'toast';
      toastEl.setAttribute('role', 'status');
      toastEl.setAttribute('aria-live', 'polite');
      document.body.appendChild(toastEl);
    }
    toastEl.textContent = message;
    toastEl.dataset.kind = kind || 'info';
    toastEl.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastEl.classList.remove('show'), 3500);
  }

  window.CD = { csrf, api, toast };

  // Group switcher and sort pickers submit on change.
  document.addEventListener('change', (e) => {
    const el = e.target;
    if (el.matches && el.matches('select[data-autosubmit]') && el.form) el.form.submit();
  });

  // A thumbnail whose file isn't on the server yet (metadata-only push):
  // show a quiet placeholder instead of a broken image.
  document.addEventListener('error', (e) => {
    const img = e.target;
    if (img && img.tagName === 'IMG' && img.parentElement && img.parentElement.classList.contains('tile')) {
      img.parentElement.classList.add('missing');
    }
  }, true);

  // Close the account menu when tapping elsewhere.
  document.addEventListener('click', (e) => {
    document.querySelectorAll('details.account-menu[open]').forEach((d) => {
      if (!d.contains(e.target)) d.removeAttribute('open');
    });
  });
})();
