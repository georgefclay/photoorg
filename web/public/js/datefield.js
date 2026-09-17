// Free-text date suggestion field with live interpretation.
//
//   <form data-datefield data-post-url="/api/photos/12/suggestions">
//     <input data-date-input> <output data-date-output></output>
//     <p data-form-error hidden></p>
//     <button type="submit" data-date-submit disabled>
//   </form>
//
// While typing, GET /api/dates/interpret?text= (debounced) shows exactly
// what will be recorded ("We'll record: March 1962, precision month").
// Submit stays disabled until the current text interprets. On success the
// form dispatches `cd:suggested` (bubbles) with
// detail { kind: 'date', text: <label>, response }.
(function () {
  'use strict';

  function errorText(err) {
    if (err.status === 429) return "You've sent a lot this hour. Please try again later.";
    if (err.status === 404) return "This photo isn't available any more.";
    if (err.status === 401 || err.status === 403) return 'Your session has expired. Reload the page and try again.';
    if (err.status === 400 && err.body && err.body.error) return err.body.error;
    return "Something went wrong. Check your connection and try again.";
  }

  function setup(form) {
    const input = form.querySelector('[data-date-input]');
    const out = form.querySelector('[data-date-output]');
    const submit = form.querySelector('[data-date-submit]');
    const errEl = form.querySelector('[data-form-error]');
    let timer = null;
    let ctrl = null;
    let current = null; // { text, result }

    function showError(msg) {
      errEl.textContent = msg || '';
      errEl.hidden = !msg;
    }

    function setResult(text, result) {
      current = { text, result };
      out.classList.toggle('ok', !!result.ok);
      out.classList.toggle('bad', !result.ok && !result.empty);
      out.textContent = result.message || '';
      submit.disabled = !result.ok;
    }

    async function interpret(text) {
      if (ctrl) ctrl.abort();
      ctrl = new AbortController();
      out.classList.remove('ok', 'bad');
      out.textContent = 'Checking…';
      try {
        const r = await window.CD.api(`/api/dates/interpret?text=${encodeURIComponent(text)}`, { signal: ctrl.signal });
        if (input.value.trim() === text) setResult(text, r);
      } catch (err) {
        if (err.name === 'AbortError') return;
        out.textContent = "Couldn't check that right now.";
        submit.disabled = true;
      }
    }

    input.addEventListener('input', () => {
      showError('');
      submit.disabled = true;
      clearTimeout(timer);
      const text = input.value.trim();
      if (!text) { if (ctrl) ctrl.abort(); setResult('', { ok: false, empty: true, message: '' }); return; }
      timer = setTimeout(() => interpret(text), 300);
    });

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const text = input.value.trim();
      if (!current || current.text !== text || !current.result.ok) return;
      showError('');
      submit.disabled = true;
      submit.setAttribute('aria-busy', 'true');
      try {
        const response = await window.CD.api(form.dataset.postUrl, { method: 'POST', body: { kind: 'date', text } });
        const label = current.result.label || text;
        form.dispatchEvent(new CustomEvent('cd:suggested', {
          bubbles: true, detail: { kind: 'date', text: label, response },
        }));
        input.value = '';
        setResult('', { ok: false, empty: true, message: '' });
      } catch (err) {
        showError(errorText(err));
        submit.disabled = false;
      } finally {
        submit.removeAttribute('aria-busy');
      }
    });
  }

  document.querySelectorAll('[data-datefield]').forEach(setup);
})();
