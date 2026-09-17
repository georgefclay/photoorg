// /search: person and place pickers (autocomplete → hidden ids) and tidy
// query strings. Without JavaScript the server matches the typed names.
(function () {
  'use strict';
  const form = document.querySelector('[data-search-form]');
  if (!form || !window.CD || !window.CD.autocomplete) return;

  function yearSpan(a, b) {
    if (a == null && b == null) return null;
    if (a != null && b != null) return a === b ? String(a) : `${a}–${b}`;
    return a != null ? `${a}–` : `–${b}`;
  }

  function picker(textSel, idSel, opts) {
    const text = form.querySelector(textSel);
    const hidden = form.querySelector(idSel);
    if (!text || !hidden) return;
    // A picked value lives in the hidden id; editing the text drops it.
    window.CD.autocomplete(text, Object.assign({
      onPick: (item) => { hidden.value = item ? String(item.id) : ''; },
    }, opts));
    text.addEventListener('input', () => { hidden.value = ''; });
  }

  picker('[data-search-person]', '[data-search-person-id]', {
    url: '/api/people/autocomplete?q=',
    label: (p) => p.display_name,
    detail: (p) => yearSpan(p.birth_year, p.death_year),
  });
  picker('[data-search-place]', '[data-search-place-id]', {
    url: '/api/places/autocomplete?q=',
    label: (p) => p.name,
  });

  // Keep the URL short: skip empty fields, and don't send the typed name
  // when the id is already known.
  form.addEventListener('submit', () => {
    const personId = form.querySelector('[data-search-person-id]');
    const placeId = form.querySelector('[data-search-place-id]');
    const pairs = [[personId, form.querySelector('[data-search-person]')], [placeId, form.querySelector('[data-search-place]')]];
    pairs.forEach(([id, text]) => { if (id && text && id.value) text.disabled = true; });
    form.querySelectorAll('input, select').forEach((el) => {
      if ((el.type === 'hidden' || el.type === 'text' || el.type === 'number' || el.type === 'search' || el.tagName === 'SELECT') && !el.value) {
        el.disabled = true;
      }
    });
    // Re-enable after navigation starts so Back restores a usable form.
    setTimeout(() => form.querySelectorAll(':disabled').forEach((el) => { el.disabled = false; }), 0);
  });
})();
