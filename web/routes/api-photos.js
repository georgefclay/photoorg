// /api/photos — list and detail. The queries live in services/photos.js
// (shared with the server-rendered pages); visibility and the group scope
// are applied there.
//
// List: keyset-paginated with a composite (sort_value, id) cursor for every
// sort; limit ≤ 200. Filters combine as AND: year, decade, year_from,
// year_to, person_id, place_id, album_id, has_no_date, has_untagged_faces,
// has_unknown_faces, low_completeness, q. Sort: recent (default) | liked |
// incomplete | oldest | newest | position (album order, needs album_id).
// Scope: the session's group switcher, or ?scope=all|unfiled|<group id>.
//
// Detail: faces, people, comments (hidden ones only for moderators),
// places, likes, backs + transcription, pending suggestions, albums,
// groups (admin / moderator of one of its groups only), physical ref.
// ?from=<list key> adds { neighbours: { prev, next } }.

const express = require('express');
const { requireUser, requireAdmin } = require('../middleware/require-user');
const { audit } = require('../services/audit');
const { getScope } = require('../services/scope');
const {
  listPhotos, parseFilters, getPhotoDetail, photoNeighbours,
} = require('../services/photos');

module.exports = function apiPhotosRoutes({ pool }) {
  const router = express.Router();
  router.use(requireUser);

  router.get('/', async (req, res, next) => {
    try {
      const { scope } = await getScope(req, pool);
      const out = await listPhotos(pool, req.user, {
        filters: parseFilters(req.query),
        sort: String(req.query.sort || 'recent'),
        cursor: req.query.cursor,
        limit: req.query.limit,
        scope,
      });
      res.json(out);
    } catch (err) { next(err); }
  });

  router.get('/:id(\\d+)', async (req, res, next) => {
    try {
      const id = Number(req.params.id);
      const photo = await getPhotoDetail(pool, req.user, id);
      if (!photo) return res.status(404).json({ error: 'not found' });
      if (req.query.from != null) {
        const { scope } = await getScope(req, pool);
        photo.neighbours = await photoNeighbours(pool, req.user, id, req.query.from, scope);
      }
      res.json(photo);
    } catch (err) { next(err); }
  });

  // Admin-only rescan_wanted toggle.
  router.post('/:id(\\d+)/rescan_wanted', requireAdmin, express.json(), async (req, res, next) => {
    const client = await pool.connect();
    try {
      await client.query('begin');
      const id = Number(req.params.id);
      const wanted = req.body && !!req.body.wanted;
      const { rows } = await client.query(
        `select id, rescan_wanted, is_deleted, is_private from photos where id = $1`, [id],
      );
      if (!rows[0]) { await client.query('rollback'); return res.status(404).json({ error: 'not found' }); }
      const prev = rows[0].rescan_wanted;
      if (prev === wanted) { await client.query('rollback'); return res.json({ ok: true, rescan_wanted: wanted }); }
      await client.query(`update photos set rescan_wanted = $1 where id = $2`, [wanted, id]);
      await audit(client, {
        actor: req.user.email, action: 'photo.rescan_wanted',
        entityType: 'photo', entityId: id, userId: req.user.id,
        previousValue: { rescan_wanted: prev },
        newValue: { rescan_wanted: wanted },
      });
      await client.query('commit');
      res.json({ ok: true, rescan_wanted: wanted });
    } catch (err) {
      await client.query('rollback').catch(() => {});
      next(err);
    } finally {
      client.release();
    }
  });

  return router;
};
