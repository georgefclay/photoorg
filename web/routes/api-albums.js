// /api/albums — read only. Albums are global; the photos inside each
// album are visibility-scoped, so an album's photo count and photo list
// only include what the viewer can see. Empty-for-me albums still appear
// in the list (the album exists site-wide) but with count 0.

const express = require('express');
const { requireUser } = require('../middleware/require-user');
const { photoVisibleSql } = require('../middleware/visibility');
const { parseListQuery } = require('../services/pagination');

module.exports = function apiAlbumsRoutes({ pool }) {
  const router = express.Router();
  router.use(requireUser);

  router.get('/', async (req, res, next) => {
    try {
      const isAdmin = req.user.role === 'admin';
      const params = [];
      let vSql;
      if (isAdmin) vSql = photoVisibleSql(req.user, { alias: 'ph', paramIndex: 0 });
      else { params.push(req.user.id); vSql = photoVisibleSql(req.user, { alias: 'ph', paramIndex: params.length }); }
      const { rows } = await pool.query(
        `select a.id, a.name, a.description, a.source, a.created_at,
                (select count(*)::int
                   from album_photos ap
                   join photos ph on ph.id = ap.photo_id
                  where ap.album_id = a.id
                    and ${vSql}) as photo_count
           from albums a
          where a.is_deleted = false
          order by a.name`,
        params,
      );
      res.json({
        items: rows.map((r) => ({
          id: Number(r.id), name: r.name, description: r.description,
          source: r.source, photo_count: r.photo_count, created_at: r.created_at,
        })),
      });
    } catch (err) { next(err); }
  });

  router.get('/:id(\\d+)', async (req, res, next) => {
    try {
      const id = Number(req.params.id);
      const albumRes = await pool.query(
        `select id, name, description, source, created_at
           from albums where id = $1 and is_deleted = false`, [id],
      );
      if (!albumRes.rows[0]) return res.status(404).json({ error: 'not found' });

      const { limit, cursor } = parseListQuery(req.query);
      const params = [id];
      let vSql;
      if (req.user.role === 'admin') {
        vSql = photoVisibleSql(req.user, { alias: 'ph', paramIndex: 0 });
      } else {
        params.push(req.user.id);
        vSql = photoVisibleSql(req.user, { alias: 'ph', paramIndex: params.length });
      }
      const clauses = [
        `ap.album_id = $1`,
        vSql,
      ];
      if (cursor != null) {
        params.push(cursor);
        clauses.push(`ph.id < $${params.length}`);
      }
      params.push(limit);
      const photosRes = await pool.query(
        `select ph.id, ph.capture_date, ph.capture_date_confirmed, ph.completeness_score,
                ap.position
           from album_photos ap
           join photos ph on ph.id = ap.photo_id
          where ${clauses.join(' and ')}
          order by ap.position asc nulls last, ph.id desc
          limit $${params.length}`,
        params,
      );
      const photos = photosRes.rows.map((p) => ({
        id: Number(p.id),
        thumb_url: `/media/thumbs/${p.id}`,
        capture_date: p.capture_date,
        capture_date_confirmed: p.capture_date_confirmed,
        completeness_score: p.completeness_score,
        position: p.position,
      }));
      const nextCursor = photos.length === limit ? photos[photos.length - 1].id : null;
      res.json({ ...albumRes.rows[0], id: Number(albumRes.rows[0].id), photos, next: nextCursor });
    } catch (err) { next(err); }
  });

  return router;
};
