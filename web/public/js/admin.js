// Admin review actions: suggestions queue, disputes, contribution uploads,
// rescan print button. Server renders the data; this file only POSTs to the
// JSON API (window.CD.api from site.js) and updates the card in place.
(function () {
  'use strict';
  const CD = window.CD;

  function setStatus(card, text, kind) {
    const el = card.querySelector('.review-actions .status, .status');
    if (!el) return;
    el.textContent = text;
    el.className = `status ${kind || ''}`;
  }

  function busy(btns, on) {
    btns.forEach((b) => { b.disabled = on; if (on) b.setAttribute('aria-busy', 'true'); else b.removeAttribute('aria-busy'); });
  }

  function finish(card, text) {
    card.classList.add('done');
    setStatus(card, text, 'ok');
    CD.toast(text);
  }

  // ---- suggestions -----------------------------------------------------
  function initSuggestions(root) {
    root.addEventListener('click', async (e) => {
      const btn = e.target.closest('button[data-act]');
      if (!btn) return;
      const card = btn.closest('[data-suggestion]');
      if (!card) return;
      const id = card.dataset.suggestion;
      const act = btn.dataset.act;
      const conflict = card.querySelector('.conflict');
      const buttons = [...card.querySelectorAll('button[data-act]')];

      if (act === 'cancel') {
        conflict.hidden = true;
        setStatus(card, '');
        return;
      }
      const force = act === 'force';
      const url = `/api/admin/suggestions/${id}/${act === 'reject' ? 'reject' : 'accept'}`;
      busy(buttons, true);
      setStatus(card, act === 'reject' ? 'Rejecting…' : 'Saving…');
      try {
        await CD.api(url, { method: 'POST', body: force ? { force: true } : {} });
        conflict.hidden = true;
        finish(card, act === 'reject' ? 'Rejected' : (force ? 'Accepted — replaced' : 'Accepted'));
      } catch (err) {
        if (err.status === 409 && err.body && (err.body.current || err.body.proposed)) {
          conflict.querySelector('[data-now]').textContent = card.dataset.current || describe(err.body.current);
          conflict.querySelector('[data-new]').textContent = card.dataset.proposed || describe(err.body.proposed);
          conflict.hidden = false;
          setStatus(card, '');
          conflict.querySelector('[data-act="force"]').focus();
        } else if (err.status === 409) {
          finish(card, 'Already decided by someone else');
        } else {
          setStatus(card, err.message || 'Something went wrong', 'error');
        }
      } finally {
        busy(buttons, false);
      }
    });
  }

  function describe(v) {
    if (v == null) return '—';
    if (typeof v !== 'object') return String(v);
    return Object.entries(v).map(([k, x]) => `${k.replace(/_/g, ' ')}: ${x}`).join(', ');
  }

  // ---- disputes --------------------------------------------------------
  function initDisputes(cards) {
    cards.forEach((card) => {
      const faceId = card.dataset.dispute;
      const panel = card.querySelector('.reassign');
      const input = card.querySelector('[data-reassign-input]');
      const go = card.querySelector('[data-act="reassign-go"]');
      let picked = null;
      if (input && CD.autocomplete) {
        CD.autocomplete(input, {
          url: '/api/people/autocomplete?q=',
          label: (p) => p.display_name,
          detail: (p) => (p.birth_year || p.death_year ? `${p.birth_year || '?'}–${p.death_year || ''}` : ''),
          onPick: (p) => { picked = p; go.disabled = !p; },
        });
      }
      async function resolve(action, personId) {
        const buttons = [...card.querySelectorAll('button[data-act]')];
        busy(buttons, true);
        setStatus(card, 'Saving…');
        try {
          await CD.api(`/api/admin/faces/${faceId}/resolve`, { method: 'POST', body: { action, person_id: personId } });
          panel.hidden = true;
          finish(card, action === 'keep' ? 'Tag kept' : action === 'unassign' ? 'Name removed' : `Tagged as ${picked.display_name}`);
        } catch (err) {
          setStatus(card, err.message || 'Something went wrong', 'error');
        } finally {
          busy(buttons, false);
          if (!picked) go.disabled = true;
        }
      }
      card.addEventListener('click', (e) => {
        const btn = e.target.closest('button[data-act]');
        if (!btn) return;
        const act = btn.dataset.act;
        if (act === 'keep' || act === 'unassign') resolve(act);
        else if (act === 'reassign') { panel.hidden = false; input.focus(); }
        else if (act === 'reassign-cancel') { panel.hidden = true; }
        else if (act === 'reassign-go' && picked) resolve('reassign', picked.id);
      });
    });
  }

  // ---- contributions ---------------------------------------------------
  function initContributions(sections) {
    sections.forEach((sec) => {
      const cid = sec.dataset.contribution;
      sec.addEventListener('click', async (e) => {
        const btn = e.target.closest('button[data-act]');
        if (!btn) return;
        const act = btn.dataset.act;
        if (act === 'approve-all' || act === 'reject-all') {
          const n = sec.querySelectorAll('.file.status-pending').length;
          const verb = act === 'approve-all' ? 'Approve' : 'Reject';
          if (!window.confirm(`${verb} all ${n} pending files in this upload?`)) return;
          busy([...sec.querySelectorAll('button')], true);
          try {
            const r = await CD.api(`/api/admin/contributions/${cid}/${act}`, { method: 'POST', body: {} });
            sec.querySelectorAll('.file.status-pending').forEach((f) => markFile(f, act === 'approve-all' ? 'approved' : 'rejected'));
            sec.querySelector('.batch-actions').remove();
            CD.toast(`${verb}d ${r.approved != null ? r.approved : r.rejected} files`);
          } catch (err) {
            CD.toast(err.message || 'Something went wrong', 'error');
            busy([...sec.querySelectorAll('button')], false);
          }
          return;
        }
        const file = btn.closest('[data-file]');
        if (!file) return;
        const buttons = [...file.querySelectorAll('button')];
        busy(buttons, true);
        try {
          await CD.api(`/api/admin/contributions/${cid}/files/${file.dataset.file}/${act}`, { method: 'POST', body: {} });
          markFile(file, act === 'approve' ? 'approved' : 'rejected');
          // A moderator's reject only drops their group; other groups may still approve.
          if (act === 'reject' && !sec.closest('[data-admin]')) file.querySelector('.file-status').textContent = 'Not for your group';
          const left = sec.querySelectorAll('.file.status-pending').length;
          const batch = sec.querySelector('.batch-actions');
          if (batch && !left) batch.remove();
          else if (batch) batch.querySelector('[data-act="approve-all"]').textContent = `Approve all ${left}`;
        } catch (err) {
          const st = file.querySelector('.file-status');
          st.textContent = err.message || 'Failed';
          busy(buttons, false);
        }
      });
    });
  }

  function markFile(file, status) {
    file.classList.remove('status-pending');
    file.classList.add(`status-${status}`);
    const actions = file.querySelector('.file-actions');
    if (actions) actions.remove();
    file.querySelector('.file-status').textContent = status;
  }

  // Upload thumbnails that can't be generated (HEIC, broken files).
  document.addEventListener('error', (e) => {
    const img = e.target;
    if (!img || img.tagName !== 'IMG') return;
    if (img.dataset.fallback) {
      const span = document.createElement('span');
      span.className = 'noprev';
      span.textContent = img.dataset.fallback;
      img.replaceWith(span);
    } else if (img.parentElement && img.parentElement.classList.contains('thumb')) {
      img.parentElement.classList.add('missing');
    }
  }, true);

  let started = false;
  document.addEventListener('DOMContentLoaded', init);
  if (document.readyState !== 'loading') init();
  function init() {
    if (started) return;
    started = true;
    const sug = document.querySelector('[data-suggestions]');
    if (sug) initSuggestions(sug);
    const disputes = document.querySelectorAll('[data-dispute]');
    if (disputes.length) initDisputes(disputes);
    const contribs = document.querySelectorAll('[data-contribution]');
    if (contribs.length) initContributions(contribs);
    // Images that failed before this deferred script ran.
    document.querySelectorAll('img[data-fallback], .thumb img').forEach((img) => {
      if (img.complete && img.naturalWidth === 0) img.dispatchEvent(new Event('error'));
    });
    document.querySelectorAll('[data-print]').forEach((b) => {
      b.hidden = false;
      b.addEventListener('click', () => window.print());
    });
  }
})();
