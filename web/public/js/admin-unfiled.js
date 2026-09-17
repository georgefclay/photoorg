// /admin/unfiled — person autocomplete for the filter form, photo picking,
// and bulk assign to a group: Preview (count) → Add to group (result).
// Uses POST /api/admin/photos/bulk-assign-groups[/preview] with only_unfiled.
(function () {
  'use strict';
  const CD = window.CD;

  // ---- person filter ----------------------------------------------------
  const nameInput = document.querySelector('[data-person-name]');
  const idInput = document.querySelector('[data-person-id]');
  if (nameInput && idInput && CD.autocomplete) {
    CD.autocomplete(nameInput, {
      url: '/api/people/autocomplete?q=',
      label: (p) => p.display_name,
      onPick: (p) => { idInput.value = p ? p.id : ''; },
    });
    nameInput.addEventListener('input', () => { if (!nameInput.value.trim()) idInput.value = ''; });
  }

  const panel = document.querySelector('[data-assign-panel]');
  if (!panel || panel.hasAttribute('data-no-groups')) return;

  const filter = JSON.parse(panel.dataset.filter || '{}');
  const groupSel = panel.querySelector('[data-assign-group]');
  const previewBtn = panel.querySelector('[data-act="preview"]');
  const applyBtn = panel.querySelector('[data-act="apply"]');
  const out = panel.querySelector('.assign-status');
  const pickedCount = panel.querySelector('[data-picked-count]');
  const picks = [...document.querySelectorAll('[data-pick]')];
  picks.forEach((cb) => { cb.closest('.pick').hidden = false; });

  let previewed = null; // body that the last preview answered for

  function which() { return (panel.querySelector('input[name="which"]:checked') || {}).value || 'filter'; }
  function pickedIds() { return picks.filter((c) => c.checked).map((c) => Number(c.value)); }
  function groupName() { return groupSel.options[groupSel.selectedIndex].text; }

  function body() {
    const gid = Number(groupSel.value);
    if (which() === 'ids') return { ids: pickedIds(), add: [gid] };
    const b = { only_unfiled: true, add: [gid] };
    Object.keys(filter).forEach((k) => { if (filter[k] != null && filter[k] !== '') b[k] = filter[k]; });
    return b;
  }

  function reset() {
    previewed = null;
    applyBtn.disabled = true;
    applyBtn.textContent = 'Add to group';
    out.textContent = '';
  }

  document.addEventListener('change', (e) => {
    if (e.target.matches('[data-pick]')) {
      const n = pickedIds().length;
      pickedCount.textContent = String(n);
      if (n && which() !== 'ids') panel.querySelector('input[name="which"][value="ids"]').checked = true;
      reset();
    } else if (panel.contains(e.target)) {
      reset();
    }
  });

  previewBtn.addEventListener('click', async () => {
    const b = body();
    if (b.ids && !b.ids.length) { out.textContent = 'Tick at least one photo first.'; return; }
    previewBtn.disabled = true;
    out.textContent = 'Counting…';
    try {
      const r = await CD.api('/api/admin/photos/bulk-assign-groups/preview', { method: 'POST', body: b });
      if (r.over_cap) {
        out.textContent = `${r.photos.toLocaleString()} photos match — more than the ${r.cap.toLocaleString()} limit for one go. Narrow the filters.`;
        return;
      }
      if (!r.photos) { out.textContent = 'No photos match.'; return; }
      const fresh = r.new_rows != null ? r.new_rows : r.photos;
      out.textContent = `This will put ${r.photos.toLocaleString()} ${r.photos === 1 ? 'photo' : 'photos'} into ${groupName()}`
        + (fresh !== r.photos ? ` (${fresh.toLocaleString()} not already there).` : '.');
      previewed = JSON.stringify(b);
      applyBtn.disabled = false;
      applyBtn.textContent = `Add ${r.photos.toLocaleString()} to ${groupName()}`;
      applyBtn.focus();
    } catch (err) {
      out.textContent = err.message || 'Could not count';
    } finally {
      previewBtn.disabled = false;
    }
  });

  applyBtn.addEventListener('click', async () => {
    const b = body();
    if (JSON.stringify(b) !== previewed) { reset(); out.textContent = 'The choice changed — preview again.'; return; }
    applyBtn.disabled = true;
    previewBtn.disabled = true;
    out.textContent = 'Saving…';
    try {
      const r = await CD.api('/api/admin/photos/bulk-assign-groups', { method: 'POST', body: b });
      out.innerHTML = '';
      out.append(`Done: ${r.photos.toLocaleString()} ${r.photos === 1 ? 'photo' : 'photos'} added to ${groupName()}. `);
      const a = document.createElement('a');
      a.href = window.location.pathname + window.location.search;
      a.textContent = 'Refresh the list';
      out.append(a);
      CD.toast(`Added ${r.photos} to ${groupName()}`);
      if (b.ids) {
        picks.filter((c) => c.checked).forEach((c) => { c.closest('li').remove(); });
      }
      previewed = null;
      applyBtn.textContent = 'Add to group';
    } catch (err) {
      out.textContent = err.message || 'Something went wrong';
      applyBtn.disabled = false;
    } finally {
      previewBtn.disabled = false;
    }
  });
})();
