// Header search box: suggests people and places as you type, and jumps
// straight to them when one is picked. Without JavaScript the box is a
// plain GET form to /search, which does the same thing on the server.
(function () {
  'use strict';
  const input = document.getElementById('site-search');
  if (!input || !window.CD || !window.CD.autocomplete) return;
  const form = input.form;

  window.CD.autocomplete(input, {
    minChars: 2,
    async source(q, signal) {
      const data = await window.CD.api(`/api/search/autocomplete?q=${encodeURIComponent(q)}`, { signal });
      return [
        ...(data.people || []).map((p) => ({ ...p, __kind: 'person' })),
        ...(data.places || []).map((p) => ({ ...p, __kind: 'place' })),
      ];
    },
    label: (i) => i.name,
    detail: (i) => {
      const who = i.__kind === 'place' ? 'place' : 'person';
      const n = i.photo_count === 1 ? '1 photo' : `${i.photo_count} photos`;
      return `${who} · ${n}`;
    },
    onPick: (item) => {
      if (!item) return;
      const key = item.__kind === 'place' ? 'place_id' : 'person_id';
      window.location.href = `/search?${key}=${encodeURIComponent(item.id)}`;
    },
  });

  // Enter with nothing highlighted runs the ordinary text search.
  if (form) form.addEventListener('submit', () => { input.blur(); });
})();
