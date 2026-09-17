// Accessible autocomplete (ARIA 1.2 combobox + listbox), shared by the
// tagger, Who is this?, disputes, search filters and relationship forms.
//
//   const ac = CD.autocomplete(inputEl, {
//     url: '/api/people/autocomplete?q=',          // or source: async (q) => items
//     label: (item) => item.display_name,           // option text
//     detail: (item) => '1931–2004',                // optional muted second line
//     onPick: (item) => { … },                      // item is null when cleared
//     allowNew: (q) => ({ newValue: q, display_name: q }),  // optional "Someone new" row
//     newLabel: (q) => `Add “${q}” as someone new`,
//     minChars: 1,
//   });
//   ac.clear(); ac.focus(); ac.destroy();
//
// The picked item is also written to input.dataset.pickedId (or '' for a
// new value) so plain forms can read it.
(function () {
  'use strict';
  let seq = 0;

  function autocomplete(input, opts) {
    const o = Object.assign({ minChars: 1, label: (i) => i.display_name || i.name, detail: null, newLabel: (q) => `Add “${q}”` }, opts);
    const id = `ac-${++seq}`;
    const wrap = document.createElement('div');
    wrap.className = 'ac';
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);
    const list = document.createElement('ul');
    list.className = 'ac-list';
    list.id = `${id}-list`;
    list.setAttribute('role', 'listbox');
    list.hidden = true;
    wrap.appendChild(list);
    const status = document.createElement('div');
    status.className = 'sr-only';
    status.setAttribute('role', 'status');
    wrap.appendChild(status);

    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-autocomplete', 'list');
    input.setAttribute('aria-expanded', 'false');
    input.setAttribute('aria-controls', list.id);
    input.setAttribute('autocomplete', 'off');

    let items = [];
    let active = -1;
    let timer = null;
    let ctrl = null;
    let lastQ = null;

    function close() {
      list.hidden = true;
      input.setAttribute('aria-expanded', 'false');
      input.removeAttribute('aria-activedescendant');
      active = -1;
    }

    function render(q) {
      list.innerHTML = '';
      items.forEach((item, i) => {
        const li = document.createElement('li');
        li.id = `${id}-opt-${i}`;
        li.setAttribute('role', 'option');
        li.className = item.__new ? 'ac-new' : '';
        const main = document.createElement('span');
        main.textContent = item.__new ? o.newLabel(q) : o.label(item);
        li.appendChild(main);
        const d = !item.__new && o.detail ? o.detail(item) : null;
        if (d) {
          const s = document.createElement('small');
          s.textContent = d;
          li.appendChild(s);
        }
        li.addEventListener('mousedown', (e) => e.preventDefault()); // keep focus in input
        li.addEventListener('click', () => pick(i));
        list.appendChild(li);
      });
      if (!items.length) {
        const li = document.createElement('li');
        li.className = 'ac-empty';
        li.textContent = 'No matches';
        list.appendChild(li);
      }
      list.hidden = false;
      input.setAttribute('aria-expanded', 'true');
      status.textContent = items.length ? `${items.length} suggestions` : 'No matches';
    }

    function highlight(i) {
      const opts = list.querySelectorAll('[role="option"]');
      opts.forEach((el, j) => el.setAttribute('aria-selected', j === i ? 'true' : 'false'));
      active = i;
      if (i >= 0 && opts[i]) {
        input.setAttribute('aria-activedescendant', opts[i].id);
        opts[i].scrollIntoView({ block: 'nearest' });
      } else {
        input.removeAttribute('aria-activedescendant');
      }
    }

    function pick(i) {
      const item = items[i];
      if (!item) return;
      if (item.__new) {
        input.dataset.pickedId = '';
        input.value = item.display_name || item.name || input.value;
      } else {
        input.dataset.pickedId = String(item.id);
        input.value = o.label(item);
      }
      lastQ = input.value;
      close();
      if (o.onPick) o.onPick(item.__new ? Object.assign({}, item, { isNew: true }) : item);
    }

    async function query(q) {
      if (ctrl) ctrl.abort();
      ctrl = new AbortController();
      list.hidden = false;
      list.innerHTML = '<li class="ac-empty"><span class="spinner" aria-hidden="true"></span> Searching…</li>';
      input.setAttribute('aria-expanded', 'true');
      try {
        let found;
        if (o.source) found = await o.source(q, ctrl.signal);
        else {
          const data = await window.CD.api(o.url + encodeURIComponent(q), { signal: ctrl.signal });
          found = data.items || [];
        }
        items = found.slice(0, 10);
        if (o.allowNew && q.trim()) {
          const exact = items.some((it) => (o.label(it) || '').toLowerCase() === q.trim().toLowerCase());
          if (!exact) items.push(Object.assign({ __new: true }, o.allowNew(q.trim())));
        }
        render(q);
        highlight(items.length === 1 && !items[0].__new ? 0 : -1);
      } catch (err) {
        if (err.name === 'AbortError') return;
        list.innerHTML = '<li class="ac-empty">Couldn’t search right now</li>';
      }
    }

    function onInput() {
      delete input.dataset.pickedId;
      if (o.onPick && lastQ !== null) { lastQ = null; o.onPick(null); }
      const q = input.value.trim();
      clearTimeout(timer);
      if (q.length < o.minChars) { close(); return; }
      timer = setTimeout(() => query(q), 160);
    }

    function onKey(e) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        if (list.hidden) { onInput(); return; }
        highlight(Math.min(items.length - 1, active + 1));
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        highlight(Math.max(0, active - 1));
      } else if (e.key === 'Enter') {
        if (!list.hidden && active >= 0) { e.preventDefault(); pick(active); }
      } else if (e.key === 'Escape') {
        if (!list.hidden) { e.preventDefault(); close(); }
      }
    }

    input.addEventListener('input', onInput);
    input.addEventListener('keydown', onKey);
    input.addEventListener('blur', () => setTimeout(close, 150));

    return {
      clear() { input.value = ''; delete input.dataset.pickedId; lastQ = null; close(); },
      focus() { input.focus(); },
      close,
      destroy() {
        input.removeEventListener('input', onInput);
        input.removeEventListener('keydown', onKey);
        wrap.parentNode.insertBefore(input, wrap);
        wrap.remove();
      },
    };
  }

  window.CD = window.CD || {};
  window.CD.autocomplete = autocomplete;
})();
