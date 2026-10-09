// /admin/corrections — bulk find & replace across pending suggestions.
//
// Same three steps as the desktop's Corrections tool, and the same reason
// for each: preview shows the exact string that will be stored (the server
// computes it, so what you read is what gets written), apply audits every
// row under one batch id, and undo reads those audit rows back.
//
// Progressive, like every other write on this site: with JS off the page
// still renders the batch list and the explanation, and the find & replace
// is simply unavailable rather than half-working.
(function () {
  const form = document.querySelector('[data-correction-form]');
  if (!form) return;

  const preview = document.querySelector('[data-preview]');
  const groupsEl = preview.querySelector('[data-groups]');
  const countEl = preview.querySelector('[data-preview-count]');
  const applyBtn = preview.querySelector('[data-act="apply"]');
  const applyStatus = preview.querySelector('[data-apply-status]');
  const errorEl = form.querySelector('[data-form-error]');

  let current = { q: '', replace: '', matchCase: true, groups: [] };

  function showError(msg) {
    errorEl.textContent = msg || '';
    errorEl.hidden = !msg;
  }

  function elide(s, n = 120) {
    const t = String(s == null ? '' : s).replace(/\n/g, ' ⏎ ');
    return t.length <= n ? t : `${t.slice(0, n - 1)}…`;
  }

  const KIND_LABELS = {
    description: 'Descriptions',
    transcription: 'Back-of-print transcriptions',
    date: 'Date evidence',
  };

  function render() {
    groupsEl.textContent = '';
    let total = 0;
    let selectable = 0;
    for (const g of current.groups) {
      total += g.count;
      const section = document.createElement('section');
      section.className = 'correction-group';
      section.dataset.kind = g.kind;

      const head = document.createElement('h3');
      const box = document.createElement('input');
      box.type = 'checkbox';
      box.checked = true;
      box.dataset.groupBox = '1';
      const label = document.createElement('label');
      label.append(box, document.createTextNode(
        ` ${KIND_LABELS[g.kind] || g.kind} (${g.count.toLocaleString('en-US')})`));
      head.append(label);
      section.append(head);

      const table = document.createElement('table');
      table.className = 'data-table';
      table.innerHTML = '<thead><tr><th scope="col"></th><th scope="col">Where</th>'
        + '<th scope="col">Now</th><th scope="col">After</th></tr></thead>';
      const tbody = document.createElement('tbody');
      for (const r of g.rows) {
        const changes = r.new_value !== r.value;
        if (changes) selectable += 1;
        const tr = document.createElement('tr');

        const tdBox = document.createElement('td');
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.checked = changes;
        cb.disabled = !changes;
        cb.dataset.rowBox = '1';
        // The row travels back to the server verbatim: it carries the
        // value the preview was computed from, which is what lets apply
        // skip anything that has changed since.
        cb.dataset.row = JSON.stringify({ id: r.id, kind: r.kind, value: r.value });
        tdBox.append(cb);

        const where = document.createElement('td');
        const link = document.createElement('a');
        link.href = r.photo_id ? `/photos/${r.photo_id}` : '#';
        link.textContent = r.photo_id ? `#${r.id} · photo ${r.photo_id}` : `#${r.id}`;
        where.append(link);

        const now = document.createElement('td');
        now.textContent = elide(r.value);
        now.title = r.value || '';
        const after = document.createElement('td');
        after.textContent = elide(r.new_value);
        after.title = r.new_value || '';

        tr.append(tdBox, where, now, after);
        tbody.append(tr);
      }
      table.append(tbody);
      section.append(table);

      if (g.truncated) {
        const note = document.createElement('p');
        note.className = 'muted small-text';
        note.textContent = `Showing the first ${g.rows.length.toLocaleString('en-US')} of `
          + `${g.count.toLocaleString('en-US')}. Apply changes the rows listed here; `
          + 'search again after applying to work through the rest.';
        section.append(note);
      }

      box.addEventListener('change', () => {
        section.querySelectorAll('[data-row-box]').forEach((cb) => {
          if (!cb.disabled) cb.checked = box.checked;
        });
      });

      groupsEl.append(section);
    }

    countEl.textContent = total
      ? `— ${total.toLocaleString('en-US')} row(s) match, ${selectable.toLocaleString('en-US')} would change`
      : '— nothing matches';
    applyBtn.disabled = selectable === 0;
    preview.hidden = false;
  }

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    showError('');
    applyStatus.textContent = '';
    const q = form.elements.q.value;
    if (!q) { showError('Type the text to find.'); return; }
    const replace = form.elements.replace.value;
    const matchCase = form.elements.match_case.checked;
    const btn = form.querySelector('[type="submit"]');
    btn.disabled = true;
    try {
      const url = `/api/admin/corrections/search?q=${encodeURIComponent(q)}`
        + `&replace=${encodeURIComponent(replace)}&match_case=${matchCase}`;
      const r = await CD.api(url);
      current = { q, replace, matchCase, groups: r.groups || [] };
      render();
    } catch (err) {
      showError(err.message || 'Search failed.');
    } finally {
      btn.disabled = false;
    }
  });

  applyBtn.addEventListener('click', async () => {
    const rows = [...groupsEl.querySelectorAll('[data-row-box]')]
      .filter((cb) => cb.checked && !cb.disabled)
      .map((cb) => JSON.parse(cb.dataset.row));
    if (!rows.length) { applyStatus.textContent = 'Nothing ticked.'; return; }
    const ok = window.confirm(
      `Replace "${current.q}" with "${current.replace}" in ${rows.length} suggestion(s)?`
      + '\n\nEvery change is audited and the batch can be undone.');
    if (!ok) return;

    applyBtn.disabled = true;
    applyStatus.textContent = 'Applying…';
    try {
      const r = await CD.api('/api/admin/corrections/apply', {
        method: 'POST',
        body: { q: current.q, replace: current.replace, match_case: current.matchCase, rows },
      });
      let msg = `Changed ${r.changed} row(s).`;
      if (r.skipped && r.skipped.length) {
        msg += ` Skipped ${r.skipped.length}: ${r.skipped.slice(0, 3).join('; ')}`;
      }
      applyStatus.textContent = msg;
      CD.toast(msg);
      // The batch list is server-rendered; reload so the new batch (and
      // its Undo button) appears without inventing a second renderer.
      window.setTimeout(() => window.location.reload(), 900);
    } catch (err) {
      applyStatus.textContent = err.message || 'Apply failed.';
      applyBtn.disabled = false;
    }
  });

  const batchTable = document.querySelector('.correction-batches');
  if (batchTable) {
    batchTable.addEventListener('click', async (e) => {
      const btn = e.target.closest('[data-act="undo"]');
      if (!btn) return;
      const tr = btn.closest('[data-batch]');
      const status = tr.querySelector('.status');
      btn.disabled = true;
      status.textContent = 'Undoing…';
      try {
        const r = await CD.api('/api/admin/corrections/undo', {
          method: 'POST', body: { batch_id: tr.dataset.batch },
        });
        let msg = `Restored ${r.restored} row(s).`;
        if (r.skipped && r.skipped.length) {
          msg += ` Left alone (changed since): ${r.skipped.slice(0, 3).join('; ')}`;
        }
        status.textContent = msg;
        CD.toast(msg);
      } catch (err) {
        status.textContent = err.message || 'Undo failed.';
        btn.disabled = false;
      }
    });
  }
}());
