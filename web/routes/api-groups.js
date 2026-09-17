// Groups routes.
//
// Read (any signed-in user):
//   GET  /api/groups           — groups the user is a member of; admins see all.
//   GET  /api/groups/:id       — detail with member list (admin/moderator only).
//
// Admin:
//   POST   /api/admin/groups                    create
//   PATCH  /api/admin/groups/:id                update (name, description)
//   POST   /api/admin/groups/:id/delete         soft-delete
//   POST   /api/admin/groups/:id/members        add member (role default member)
//   PATCH  /api/admin/groups/:id/members/:uid   change role
//   POST   /api/admin/groups/:id/members/:uid/remove
//   POST   /api/admin/photos/bulk-assign-groups {filter, add:[gid], remove:[gid]}
//     — synchronous, cap 20,000 rows per call, one transaction, returns counts.
//     filter may include only_unfiled: true (non-private photos in no live group).
//   POST   /api/admin/photos/bulk-assign-groups/preview {filter, ids?, add?}
//     — { photos, new_rows, cap, over_cap }; writes nothing.
//   GET    /api/groups/:groupId/user-lookup?q=   (moderator of group or admin)
//   GET    /api/admin/unfiled                    (in api-admin.js)
//
// Moderator (of :groupId):
//   POST   /api/groups/:groupId/photos/:photoId/remove   soft-delete photo_groups row
//   POST   /api/groups/:groupId/members                  add member (member role only)
//   POST   /api/groups/:groupId/members/:userId/remove   remove member (never role change)
//
// Sync (service token) is in api-sync.js.

const express = require('express');
const { requireUser, requireAdmin } = require('../middleware/require-user');
const { requireModerator } = require('../middleware/require-moderator');
const { audit } = require('../services/audit');
const { userGroupIds, userModeratorGroupIds } = require('../middleware/visibility');
const { bulkFilterClauses, lookupUsers, BULK_CAP } = require('../services/admin');

function toInt(v) { const n = parseInt(v, 10); return Number.isInteger(n) ? n : null; }
function toIntArr(v) {
  if (!Array.isArray(v)) return [];
  return v.map(toInt).filter((n) => Number.isInteger(n) && n > 0);
}

module.exports = function apiGroupsRoutes({ pool }) {
  const router = express.Router();
  router.use(requireUser);
  router.use(express.json({ limit: '256kb' }));

  // ---- Read --------------------------------------------------------

  router.get('/', async (req, res, next) => {
    try {
      if (req.user.role === 'admin') {
        const { rows } = await pool.query(
          `select g.id, g.name, g.description, g.created_at,
                  (select count(*)::int from group_members gm
                    where gm.group_id = g.id and gm.is_deleted = false) as member_count,
                  (select count(*)::int from photo_groups pg
                    where pg.group_id = g.id and pg.is_deleted = false) as photo_count
             from groups g
            where g.is_deleted = false
            order by g.name`,
        );
        return res.json({ items: rows.map((r) => ({
          id: Number(r.id), name: r.name, description: r.description,
          created_at: r.created_at, member_count: r.member_count, photo_count: r.photo_count,
          my_role: 'admin',
        })) });
      }
      const { rows } = await pool.query(
        `select g.id, g.name, g.description, g.created_at, gm.role,
                (select count(*)::int from group_members gm2
                  where gm2.group_id = g.id and gm2.is_deleted = false) as member_count,
                (select count(*)::int from photo_groups pg
                  where pg.group_id = g.id and pg.is_deleted = false) as photo_count
           from groups g
           join group_members gm on gm.group_id = g.id
          where gm.user_id = $1
            and gm.is_deleted = false
            and g.is_deleted = false
          order by g.name`,
        [req.user.id],
      );
      res.json({ items: rows.map((r) => ({
        id: Number(r.id), name: r.name, description: r.description,
        created_at: r.created_at, member_count: r.member_count, photo_count: r.photo_count,
        my_role: r.role,
      })) });
    } catch (err) { next(err); }
  });

  router.get('/:id(\\d+)', async (req, res, next) => {
    try {
      const groupId = Number(req.params.id);
      const group = (await pool.query(
        `select id, name, description, created_at from groups where id = $1 and is_deleted = false`, [groupId],
      )).rows[0];
      if (!group) return res.status(404).json({ error: 'not found' });
      // Members list is admin/moderator-only.
      let allowed = req.user.role === 'admin';
      if (!allowed) {
        const modIds = await userModeratorGroupIds(pool, req.user);
        allowed = modIds.includes(groupId);
      }
      const members = allowed
        ? (await pool.query(
            `select u.id, u.email, u.display_name, gm.role, gm.added_at
               from group_members gm join users u on u.id = gm.user_id
              where gm.group_id = $1 and gm.is_deleted = false
              order by u.display_name nulls last, u.email`,
            [groupId],
          )).rows
        : null;
      res.json({ ...group, id: Number(group.id), members });
    } catch (err) { next(err); }
  });

  // ---- Moderator (or admin): find a user to add as a member --------
  // ≥ 2 characters; email prefix or display-name substring; active users only.
  router.get(
    '/:groupId(\\d+)/user-lookup',
    ...requireModerator({ pool, groupIdParam: 'groupId' }),
    async (req, res, next) => {
      try {
        res.json({ items: await lookupUsers(pool, req.query.q, req.moderatedGroupId) });
      } catch (err) { next(err); }
    },
  );

  // ---- Moderator: remove photo from group / manage members ---------

  router.post(
    '/:groupId(\\d+)/photos/:photoId(\\d+)/remove',
    ...requireModerator({ pool, groupIdParam: 'groupId' }),
    async (req, res, next) => {
      const groupId = req.moderatedGroupId;
      const photoId = Number(req.params.photoId);
      const client = await pool.connect();
      try {
        await client.query('begin');
        const row = (await client.query(
          `select is_deleted from photo_groups where photo_id = $1 and group_id = $2`,
          [photoId, groupId],
        )).rows[0];
        if (!row || row.is_deleted) { await client.query('rollback'); return res.status(404).json({ error: 'not in this group' }); }
        await client.query(
          `update photo_groups set is_deleted = true, deleted_at = now(), deleted_by = $1
            where photo_id = $2 and group_id = $3`,
          [req.user.id, photoId, groupId],
        );
        await audit(client, {
          actor: req.user.email, action: 'photo_group.remove',
          entityType: 'photo', entityId: photoId, userId: req.user.id,
          newValue: { group_id: groupId, via: 'moderator' },
        });
        // Determine if unfiled now.
        const stillGrouped = (await client.query(
          `select 1 from photo_groups where photo_id = $1 and is_deleted = false limit 1`, [photoId],
        )).rows.length > 0;
        await client.query('commit');
        res.json({ ok: true, unfiled: !stillGrouped });
      } catch (err) {
        await client.query('rollback').catch(() => {});
        next(err);
      } finally {
        client.release();
      }
    },
  );

  router.post(
    '/:groupId(\\d+)/members',
    ...requireModerator({ pool, groupIdParam: 'groupId' }),
    async (req, res, next) => {
      const groupId = req.moderatedGroupId;
      const userId = toInt(req.body && req.body.user_id);
      if (!userId) return res.status(400).json({ error: 'user_id required' });
      const client = await pool.connect();
      try {
        await client.query('begin');
        const u = (await client.query(`select id, email from users where id = $1 and status = 'active'`, [userId])).rows[0];
        if (!u) { await client.query('rollback'); return res.status(404).json({ error: 'user not found' }); }
        // Moderator can only add 'member' role.
        await client.query(
          `insert into group_members (group_id, user_id, role, added_by, is_deleted)
           values ($1, $2, 'member', $3, false)
           on conflict (group_id, user_id) do update
             set role = case when group_members.role = 'moderator' then 'moderator' else 'member' end,
                 is_deleted = false, deleted_at = null, deleted_by = null, updated_at = now()`,
          [groupId, userId, req.user.id],
        );
        await audit(client, {
          actor: req.user.email, action: 'group_member.add',
          entityType: 'group', entityId: groupId, userId: req.user.id,
          newValue: { user_id: userId, role: 'member', via: 'moderator' },
        });
        await client.query('commit');
        res.json({ ok: true });
      } catch (err) {
        await client.query('rollback').catch(() => {});
        next(err);
      } finally {
        client.release();
      }
    },
  );

  router.post(
    '/:groupId(\\d+)/members/:userId(\\d+)/remove',
    ...requireModerator({ pool, groupIdParam: 'groupId' }),
    async (req, res, next) => {
      const groupId = req.moderatedGroupId;
      const userId = Number(req.params.userId);
      const client = await pool.connect();
      try {
        await client.query('begin');
        const gm = (await client.query(
          `select role, is_deleted from group_members where group_id = $1 and user_id = $2`,
          [groupId, userId],
        )).rows[0];
        if (!gm || gm.is_deleted) { await client.query('rollback'); return res.status(404).json({ error: 'not a member' }); }
        await client.query(
          `update group_members set is_deleted = true, deleted_at = now(), deleted_by = $1
            where group_id = $2 and user_id = $3`,
          [req.user.id, groupId, userId],
        );
        await audit(client, {
          actor: req.user.email, action: 'group_member.remove',
          entityType: 'group', entityId: groupId, userId: req.user.id,
          newValue: { user_id: userId, prev_role: gm.role, via: 'moderator' },
        });
        await client.query('commit');
        res.json({ ok: true });
      } catch (err) {
        await client.query('rollback').catch(() => {});
        next(err);
      } finally {
        client.release();
      }
    },
  );

  return router;
};

// -----------------------------------------------------------------------
// Admin group routes — mounted separately at /api/admin/groups so the
// requireAdmin check is uniform.
// -----------------------------------------------------------------------
module.exports.adminGroupsRouter = function adminGroupsRouter({ pool }) {
  const router = express.Router();
  router.use(requireAdmin);
  router.use(express.json({ limit: '256kb' }));

  router.post('/', async (req, res, next) => {
    const name = String((req.body && req.body.name) || '').trim();
    const description = String((req.body && req.body.description) || '').trim() || null;
    if (!name) return res.status(400).json({ error: 'name required' });
    const client = await pool.connect();
    try {
      await client.query('begin');
      const ins = await client.query(
        `insert into groups (name, description, created_by) values ($1, $2, $3)
         returning id, name, description, created_at`,
        [name, description, req.user.id],
      );
      await audit(client, {
        actor: req.user.email, action: 'group.create',
        entityType: 'group', entityId: ins.rows[0].id, userId: req.user.id,
        newValue: { name, description },
      });
      await client.query('commit');
      res.status(201).json({ ...ins.rows[0], id: Number(ins.rows[0].id) });
    } catch (err) {
      await client.query('rollback').catch(() => {});
      if (err.code === '23505') return res.status(409).json({ error: 'name already exists' });
      next(err);
    } finally {
      client.release();
    }
  });

  router.patch('/:id(\\d+)', async (req, res, next) => {
    const id = Number(req.params.id);
    const name = req.body && req.body.name != null ? String(req.body.name).trim() : null;
    const description = req.body && req.body.description != null ? String(req.body.description).trim() : null;
    const client = await pool.connect();
    try {
      await client.query('begin');
      const prev = (await client.query(
        `select name, description from groups where id = $1 and is_deleted = false for update`, [id],
      )).rows[0];
      if (!prev) { await client.query('rollback'); return res.status(404).json({ error: 'not found' }); }
      const nextName = name != null ? name : prev.name;
      const nextDesc = description != null ? description : prev.description;
      await client.query(
        `update groups set name = $1, description = $2 where id = $3`,
        [nextName, nextDesc, id],
      );
      await audit(client, {
        actor: req.user.email, action: 'group.update',
        entityType: 'group', entityId: id, userId: req.user.id,
        previousValue: prev, newValue: { name: nextName, description: nextDesc },
      });
      await client.query('commit');
      res.json({ ok: true });
    } catch (err) {
      await client.query('rollback').catch(() => {});
      if (err.code === '23505') return res.status(409).json({ error: 'name already exists' });
      next(err);
    } finally {
      client.release();
    }
  });

  router.post('/:id(\\d+)/delete', async (req, res, next) => {
    const id = Number(req.params.id);
    const client = await pool.connect();
    try {
      await client.query('begin');
      const prev = (await client.query(
        `select is_deleted from groups where id = $1 for update`, [id],
      )).rows[0];
      if (!prev || prev.is_deleted) { await client.query('rollback'); return res.status(404).json({ error: 'not found' }); }
      await client.query(
        `update groups set is_deleted = true, deleted_at = now(), deleted_by = $1 where id = $2`,
        [req.user.id, id],
      );
      // Soft-delete member and photo rows so listings/counts drop them.
      await client.query(
        `update group_members set is_deleted = true, deleted_at = now(), deleted_by = $1
          where group_id = $2 and is_deleted = false`,
        [req.user.id, id],
      );
      await client.query(
        `update photo_groups set is_deleted = true, deleted_at = now(), deleted_by = $1
          where group_id = $2 and is_deleted = false`,
        [req.user.id, id],
      );
      await audit(client, {
        actor: req.user.email, action: 'group.delete',
        entityType: 'group', entityId: id, userId: req.user.id,
        newValue: { via: 'admin' },
      });
      await client.query('commit');
      res.json({ ok: true });
    } catch (err) {
      await client.query('rollback').catch(() => {});
      next(err);
    } finally {
      client.release();
    }
  });

  router.post('/:id(\\d+)/members', async (req, res, next) => {
    const groupId = Number(req.params.id);
    const userId = toInt(req.body && req.body.user_id);
    const role = String((req.body && req.body.role) || 'member');
    if (!userId) return res.status(400).json({ error: 'user_id required' });
    if (!['member', 'moderator'].includes(role)) return res.status(400).json({ error: 'bad role' });
    const client = await pool.connect();
    try {
      await client.query('begin');
      await client.query(
        `insert into group_members (group_id, user_id, role, added_by, is_deleted)
         values ($1, $2, $3, $4, false)
         on conflict (group_id, user_id) do update
           set role = excluded.role, is_deleted = false, deleted_at = null, deleted_by = null, updated_at = now()`,
        [groupId, userId, role, req.user.id],
      );
      await audit(client, {
        actor: req.user.email, action: 'group_member.add',
        entityType: 'group', entityId: groupId, userId: req.user.id,
        newValue: { user_id: userId, role, via: 'admin' },
      });
      await client.query('commit');
      res.json({ ok: true });
    } catch (err) {
      await client.query('rollback').catch(() => {});
      next(err);
    } finally {
      client.release();
    }
  });

  router.patch('/:id(\\d+)/members/:uid(\\d+)', async (req, res, next) => {
    const groupId = Number(req.params.id);
    const userId = Number(req.params.uid);
    const role = String((req.body && req.body.role) || '');
    if (!['member', 'moderator'].includes(role)) return res.status(400).json({ error: 'bad role' });
    const client = await pool.connect();
    try {
      await client.query('begin');
      const prev = (await client.query(
        `select role, is_deleted from group_members where group_id = $1 and user_id = $2 for update`,
        [groupId, userId],
      )).rows[0];
      if (!prev || prev.is_deleted) { await client.query('rollback'); return res.status(404).json({ error: 'not a member' }); }
      if (prev.role === role) { await client.query('rollback'); return res.json({ ok: true }); }
      await client.query(`update group_members set role = $1 where group_id = $2 and user_id = $3`,
        [role, groupId, userId]);
      await audit(client, {
        actor: req.user.email, action: 'group_member.role_change',
        entityType: 'group', entityId: groupId, userId: req.user.id,
        previousValue: { user_id: userId, role: prev.role },
        newValue: { user_id: userId, role },
      });
      await client.query('commit');
      res.json({ ok: true });
    } catch (err) {
      await client.query('rollback').catch(() => {});
      next(err);
    } finally {
      client.release();
    }
  });

  router.post('/:id(\\d+)/members/:uid(\\d+)/remove', async (req, res, next) => {
    const groupId = Number(req.params.id);
    const userId = Number(req.params.uid);
    const client = await pool.connect();
    try {
      await client.query('begin');
      const prev = (await client.query(
        `select role, is_deleted from group_members where group_id = $1 and user_id = $2 for update`,
        [groupId, userId],
      )).rows[0];
      if (!prev || prev.is_deleted) { await client.query('rollback'); return res.status(404).json({ error: 'not a member' }); }
      await client.query(
        `update group_members set is_deleted = true, deleted_at = now(), deleted_by = $1
          where group_id = $2 and user_id = $3`,
        [req.user.id, groupId, userId],
      );
      await audit(client, {
        actor: req.user.email, action: 'group_member.remove',
        entityType: 'group', entityId: groupId, userId: req.user.id,
        previousValue: { user_id: userId, role: prev.role },
        newValue: { via: 'admin' },
      });
      await client.query('commit');
      res.json({ ok: true });
    } catch (err) {
      await client.query('rollback').catch(() => {});
      next(err);
    } finally {
      client.release();
    }
  });

  return router;
};

// -----------------------------------------------------------------------
// Bulk assign — synchronous, cap 20,000 rows per call (answer #7).
// Filter shape:
//   { album_id, scan_batch, person_id, year, decade, source_folder, ids: [...] }
// Combined as AND. `ids` (photo id list) overrides other filters if provided.
// -----------------------------------------------------------------------
module.exports.adminBulkAssignRouter = function adminBulkAssignRouter({ pool }) {
  const router = express.Router();
  router.use(requireAdmin);
  router.use(express.json({ limit: '2mb' }));

  // Count preview for the /admin/unfiled page — same filter shape (plus
  // optional `only_unfiled: true`), writes nothing. `add` is optional; when
  // given, `new_rows` says how many photos are not yet live in those groups.
  router.post('/bulk-assign-groups/preview', async (req, res, next) => {
    try {
      const b = req.body || {};
      const add = toIntArr(b.add);
      const ids = toIntArr(b.ids);
      const params = [];
      let where;
      if (ids.length) {
        params.push(ids);
        where = `p.id = any($1::bigint[]) and p.is_deleted = false`;
      } else {
        where = bulkFilterClauses(b, params).join(' and ');
      }
      let newRows = null;
      if (add.length) {
        params.push(add);
        newRows = `(select count(*)::int from photos p cross join unnest($${params.length}::bigint[]) g(gid)
                     where ${where}
                       and not exists (select 1 from photo_groups x where x.photo_id = p.id and x.group_id = g.gid and x.is_deleted = false))`;
      }
      const { rows } = await pool.query(
        `select (select count(*)::int from photos p where ${where}) as photos
                ${newRows ? `, ${newRows} as new_rows` : ''}`,
        params,
      );
      const photos = rows[0].photos;
      res.json({
        photos,
        new_rows: rows[0].new_rows != null ? rows[0].new_rows : null,
        cap: BULK_CAP,
        over_cap: photos > BULK_CAP,
      });
    } catch (err) { next(err); }
  });

  router.post('/bulk-assign-groups', async (req, res, next) => {
    const b = req.body || {};
    const add = toIntArr(b.add);
    const remove = toIntArr(b.remove);
    if (add.length === 0 && remove.length === 0) {
      return res.status(400).json({ error: 'need at least one add or remove' });
    }
    // Resolve target photo id list.
    let ids = toIntArr(b.ids);
    if (ids.length === 0) {
      const params = [];
      const clauses = bulkFilterClauses(b, params);
      const q = await pool.query(
        `select id from photos p where ${clauses.join(' and ')} order by id limit ${BULK_CAP + 1}`,
        params,
      );
      ids = q.rows.map((r) => Number(r.id));
    }
    if (ids.length > BULK_CAP) {
      return res.status(400).json({ error: `too many photos in scope (${ids.length}); cap is ${BULK_CAP}` });
    }
    if (ids.length === 0) return res.json({ ok: true, added: 0, removed: 0, photos: 0 });

    // Validate groups exist.
    const allGroups = [...new Set([...add, ...remove])];
    const grpCheck = await pool.query(
      `select id from groups where id = any($1::bigint[]) and is_deleted = false`,
      [allGroups],
    );
    const validGroups = new Set(grpCheck.rows.map((r) => Number(r.id)));
    for (const g of allGroups) if (!validGroups.has(g)) return res.status(400).json({ error: `group ${g} not found` });

    const client = await pool.connect();
    try {
      await client.query('begin');
      let added = 0;
      let removed = 0;
      for (const gid of add) {
        const r = await client.query(
          `insert into photo_groups (photo_id, group_id, added_by, is_deleted)
             select unnest($1::bigint[]) as pid, $2, $3, false
           on conflict (photo_id, group_id) do update
             set is_deleted = false, deleted_at = null, deleted_by = null, updated_at = now()
           returning photo_id`,
          [ids, gid, req.user.id],
        );
        added += r.rowCount;
      }
      for (const gid of remove) {
        const r = await client.query(
          `update photo_groups
              set is_deleted = true, deleted_at = now(), deleted_by = $1
            where group_id = $2
              and photo_id = any($3::bigint[])
              and is_deleted = false`,
          [req.user.id, gid, ids],
        );
        removed += r.rowCount;
      }
      await audit(client, {
        actor: req.user.email, action: 'photo_group.bulk',
        entityType: 'photo_groups', entityId: null, userId: req.user.id,
        newValue: { photos: ids.length, add, remove, added, removed },
      });
      await client.query('commit');
      res.json({ ok: true, photos: ids.length, added, removed });
    } catch (err) {
      await client.query('rollback').catch(() => {});
      next(err);
    } finally {
      client.release();
    }
  });

  return router;
};
