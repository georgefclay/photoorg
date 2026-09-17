// /admin/groups and /admin/groups/:id — create/rename/delete (admin), add and
// remove members (admin + moderator of the group), role changes (admin).
// Admin writes go to /api/admin/groups/*; moderator writes to /api/groups/:id/*.
(function () {
  'use strict';
  const CD = window.CD;

  function status(form, text, kind) {
    const el = form.querySelector('.status');
    if (el) { el.textContent = text; el.className = `status ${kind || ''}`; }
  }

  // ---- create (list page) ---------------------------------------------
  const create = document.querySelector('[data-create-group]');
  if (create) {
    create.addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = create.querySelector('button[type="submit"]');
      btn.disabled = true;
      status(create, 'Creating…');
      try {
        const g = await CD.api('/api/admin/groups', {
          method: 'POST',
          body: { name: create.elements.name.value, description: create.elements.description.value },
        });
        window.location.href = `/admin/groups/${g.id}`;
      } catch (err) {
        status(create, err.status === 409 ? 'A group with that name already exists.' : err.message, 'error');
        btn.disabled = false;
      }
    });
  }

  const root = document.querySelector('[data-group]');
  if (!root) return;
  const gid = root.dataset.group;
  const isAdmin = root.dataset.admin === '1';

  // ---- rename / describe ----------------------------------------------
  const edit = root.querySelector('[data-edit-group]');
  if (edit) {
    edit.addEventListener('submit', async (e) => {
      e.preventDefault();
      status(edit, 'Saving…');
      try {
        await CD.api(`/api/admin/groups/${gid}`, {
          method: 'PATCH',
          body: { name: edit.elements.name.value, description: edit.elements.description.value },
        });
        window.location.reload();
      } catch (err) {
        status(edit, err.status === 409 ? 'A group with that name already exists.' : err.message, 'error');
      }
    });
  }

  // ---- add member -----------------------------------------------------
  const add = root.querySelector('[data-add-member]');
  if (add) {
    const input = add.querySelector('[data-member-lookup]');
    const btn = add.querySelector('[data-add-btn]');
    let picked = null;
    CD.autocomplete(input, {
      minChars: 2,
      source: async (q, signal) => {
        const data = await CD.api(`/api/groups/${gid}/user-lookup?q=${encodeURIComponent(q)}`, { signal });
        return data.items || [];
      },
      label: (u) => u.display_name,
      detail: (u) => (u.is_member ? `${u.email} · already a member` : u.email),
      onPick: (u) => { picked = u; btn.disabled = !u; },
    });
    add.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (!picked) return;
      const role = isAdmin ? (add.querySelector('input[name="role"]:checked') || {}).value || 'member' : 'member';
      btn.disabled = true;
      status(add, 'Adding…');
      try {
        const url = isAdmin ? `/api/admin/groups/${gid}/members` : `/api/groups/${gid}/members`;
        await CD.api(url, { method: 'POST', body: { user_id: picked.id, role } });
        CD.toast(`${picked.display_name} added`);
        window.location.reload();
      } catch (err) {
        status(add, err.message, 'error');
        btn.disabled = false;
      }
    });
  }

  // ---- member rows ----------------------------------------------------
  root.addEventListener('change', async (e) => {
    const sel = e.target.closest('[data-role-select]');
    if (!sel) return;
    const li = sel.closest('[data-member]');
    const prev = sel.querySelector('option[selected]');
    sel.disabled = true;
    try {
      await CD.api(`/api/admin/groups/${gid}/members/${li.dataset.member}`, { method: 'PATCH', body: { role: sel.value } });
      CD.toast(`${li.dataset.name} is now a ${sel.value}`);
      sel.querySelectorAll('option').forEach((o) => { o.defaultSelected = o.value === sel.value; });
    } catch (err) {
      CD.toast(err.message, 'error');
      if (prev) sel.value = prev.value;
    } finally {
      sel.disabled = false;
    }
  });

  root.addEventListener('click', async (e) => {
    const btn = e.target.closest('button[data-act]');
    if (!btn) return;
    if (btn.dataset.act === 'remove-member') {
      const li = btn.closest('[data-member]');
      if (!window.confirm(`Remove ${li.dataset.name} from this group? They will stop seeing photos that are only in this group.`)) return;
      btn.disabled = true;
      try {
        const url = isAdmin
          ? `/api/admin/groups/${gid}/members/${li.dataset.member}/remove`
          : `/api/groups/${gid}/members/${li.dataset.member}/remove`;
        await CD.api(url, { method: 'POST', body: {} });
        li.classList.add('removed');
        li.querySelector('.member-actions').textContent = 'Removed';
        CD.toast(`${li.dataset.name} removed`);
      } catch (err) {
        CD.toast(err.message, 'error');
        btn.disabled = false;
      }
    } else if (btn.dataset.act === 'delete-group') {
      const name = document.querySelector('h1').textContent.trim();
      const typed = window.prompt(`Type the group name to delete it:\n${name}`);
      if (typed == null) return;
      if (typed.trim().toLowerCase() !== name.toLowerCase()) { CD.toast('Name did not match; nothing deleted', 'error'); return; }
      btn.disabled = true;
      try {
        await CD.api(`/api/admin/groups/${gid}/delete`, { method: 'POST', body: {} });
        window.location.href = '/admin/groups';
      } catch (err) {
        CD.toast(err.message, 'error');
        btn.disabled = false;
      }
    }
  });
})();
