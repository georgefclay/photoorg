// /api/photos — list and detail.
//
// Every list is keyset-paginated (by id desc, cursor is last-seen id),
// limit ≤ 100, visibility-scoped, private + deleted excluded. Filters
// combine as AND: year, decade, person_id, place_id, album_id,
// has_no_date, has_untagged_faces, low_completeness. Sort is one of
// recent (id desc — default), liked (like count desc), incomplete
// (completeness_score asc).
//
// Detail returns everything the photo page needs: faces (with person,
// disputed flag, bbox), people, comments (non-hidden), place, likes,
// backs with transcription, pending suggestions on this photo,
// physical reference, rescan_wanted.

const express = require('express');
const { requireUser, requireAdmin } = require('../middleware/require-user');
const { photoVisibleSql, assertPhotoVisible } = require('../middleware/visibility');
const { parseListQuery } = require('../services/pagination');
const { audit } = require('../services/audit');

function toInt(v) {
  const n = parseInt(v, 10);
  return Number.isInteger(n) ? n : null;
}

module.exports = function apiPhotosRoutes({ pool }) {
  const router = express.Router();
  router.use(requireUser);

  router.get('/', async (req, res, next) => {
    try {
      const { limit, cursor } = parseListQuery(req.query);
      const isAdmin = req.user.role === 'admin';

      const params = [];
      let vSql;
      if (isAdmin) {
        vSql = photoVisibleSql(req.user, { alias: 'p', paramIndex: 0 });
      } else {
        params.push(req.user.id);
        vSql = photoVisibleSql(req.user, { alias: 'p', paramIndex: params.length });
      }
      const clauses = [vSql];

      const year = toInt(req.query.year);
      if (year != null) {
        params.push(year);
        clauses.push(`extract(year from p.capture_date) = $${params.length}`);
      }
      const decade = toInt(req.query.decade);
      if (decade != null) {
        params.push(decade, decade + 9);
        clauses.push(`extract(year from p.capture_date) between $${params.length - 1} and $${params.length}`);
      }
      const personId = toInt(req.query.person_id);
      if (personId != null) {
        params.push(personId);
        clauses.push(`exists (
          select 1 from faces f
           where f.photo_id = p.id
             and f.person_id = $${params.length}
             and f.is_deleted = false
             and f.is_disputed = false
        )`);
      }
      const placeId = toInt(req.query.place_id);
      if (placeId != null) {
        params.push(placeId);
        clauses.push(`exists (select 1 from photo_places pp where pp.photo_id = p.id and pp.place_id = $${params.length})`);
      }
      const albumId = toInt(req.query.album_id);
      if (albumId != null) {
        params.push(albumId);
        clauses.push(`exists (select 1 from album_photos ap where ap.photo_id = p.id and ap.album_id = $${params.length})`);
      }
      if (req.query.has_no_date === 'true' || req.query.has_no_date === '1') {
        clauses.push(`p.capture_date_confirmed = false`);
      }
      if (req.query.has_untagged_faces === 'true' || req.query.has_untagged_faces === '1') {
        clauses.push(`exists (
          select 1 from faces f
           where f.photo_id = p.id
             and f.person_id is null
             and f.is_deleted = false
             and coalesce(f.review_status, 'pending') <> 'ignore'
        )`);
      }
      if (req.query.low_completeness === 'true' || req.query.low_completeness === '1') {
        clauses.push(`p.completeness_score < 60`);
      }

      const sort = String(req.query.sort || 'recent');
      let orderSql;
      if (sort === 'liked') {
        orderSql = `order by (select count(*) from likes l where l.photo_id = p.id) desc, p.id desc`;
      } else if (sort === 'incomplete') {
        orderSql = `order by p.completeness_score asc, p.id desc`;
      } else {
        orderSql = `order by p.id desc`;
      }

      // Cursor only applies to id-based orderings — for liked/incomplete
      // fall back to offset-safe id filter (still keyset on tie-break).
      if (cursor != null) {
        params.push(cursor);
        clauses.push(`p.id < $${params.length}`);
      }
      params.push(limit);

      const where = clauses.length ? `where ${clauses.join(' and ')}` : '';
      const sql = `
        select p.id,
               p.capture_date,
               p.capture_date_precision,
               p.capture_date_confirmed,
               p.scan_batch,
               p.scan_sequence,
               p.physical_ref_note,
               p.completeness_score,
               p.rescan_wanted,
               p.width,
               p.height,
               (select count(*) from likes l where l.photo_id = p.id)::int as like_count
          from photos p
          ${where}
          ${orderSql}
          limit $${params.length}
      `;
      const { rows } = await pool.query(sql, params);

      const items = rows.map((r) => ({
        id: Number(r.id),
        thumb_url: `/media/thumbs/${r.id}`,
        working_url: `/media/working/${r.id}`,
        capture_date: r.capture_date,
        capture_date_precision: r.capture_date_precision,
        capture_date_confirmed: r.capture_date_confirmed,
        scan_batch: r.scan_batch,
        scan_sequence: r.scan_sequence,
        physical_ref_note: r.physical_ref_note,
        completeness_score: r.completeness_score,
        rescan_wanted: r.rescan_wanted,
        width: r.width,
        height: r.height,
        like_count: r.like_count,
      }));
      const next = items.length === limit ? items[items.length - 1].id : null;
      res.json({ items, next });
    } catch (err) { next(err); }
  });

  router.get('/:id(\\d+)', async (req, res, next) => {
    try {
      const id = Number(req.params.id);
      const visible = await assertPhotoVisible(pool, req.user, id);
      if (!visible) return res.status(404).json({ error: 'not found' });

      const photoRes = await pool.query(
        `select id, capture_date, capture_date_precision, capture_date_confirmed,
                scan_batch, scan_sequence, source_folder, source_filename, physical_ref_note,
                rescan_wanted, completeness_score, description_ai, width, height, orientation,
                has_no_people, is_scan, exif_taken_at, exif_camera, exif_gps_lat, exif_gps_lon
           from photos where id = $1`,
        [id],
      );
      const photo = photoRes.rows[0];

      const facesRes = await pool.query(
        `select f.id, f.person_id, f.bbox, f.is_disputed, f.dispute_note,
                f.source, f.review_status,
                p.given_name, p.surname, p.display_name
           from faces f
      left join people p on p.id = f.person_id
          where f.photo_id = $1
            and f.is_deleted = false
          order by f.id`,
        [id],
      );

      const commentsRes = await pool.query(
        `select c.id, c.body, c.created_at,
                u.display_name, u.email, u.id as user_id
           from comments c
      left join users u on u.id = c.user_id
          where c.photo_id = $1
            and c.is_hidden = false
          order by c.created_at asc`,
        [id],
      );

      const placesRes = await pool.query(
        `select pl.id, pl.name, pl.latitude, pl.longitude, pp.confirmed
           from photo_places pp
           join places pl on pl.id = pp.place_id
          where pp.photo_id = $1
            and pl.is_deleted = false`,
        [id],
      );

      const likesRes = await pool.query(
        `select count(*)::int as like_count,
                bool_or(user_id = $2) as liked_by_me
           from likes
          where photo_id = $1`,
        [id, req.user.id],
      );

      const backsRes = await pool.query(
        `select id, transcribed_text, transcription_confidence, transcription_confirmed
           from photo_backs where photo_id = $1
          order by id`,
        [id],
      );

      const suggestionsRes = await pool.query(
        `select s.id, s.kind, s.payload, s.confidence, s.source, s.created_at,
                u.id as user_id, u.display_name, u.email
           from suggestions s
      left join users u on u.id = s.user_id
          where s.photo_id = $1
            and s.status = 'pending'
          order by s.id desc`,
        [id],
      );

      const albumsRes = await pool.query(
        `select a.id, a.name
           from album_photos ap
           join albums a on a.id = ap.album_id
          where ap.photo_id = $1
            and a.is_deleted = false
          order by a.name`,
        [id],
      );

      // For suggester identity policy (answer #13): show display_name to
      // admins and to moderators of any group the photo is in; "someone"
      // to everyone else. Emails are never shown.
      let canSeeSuggester = req.user.role === 'admin';
      if (!canSeeSuggester) {
        const modCheck = await pool.query(
          `select 1
             from photo_groups pg
             join group_members gm using (group_id)
            where pg.photo_id = $1
              and pg.is_deleted = false
              and gm.user_id = $2
              and gm.role = 'moderator'
              and gm.is_deleted = false
            limit 1`,
          [id, req.user.id],
        );
        canSeeSuggester = modCheck.rows.length > 0;
      }

      res.json({
        id: Number(photo.id),
        thumb_url: `/media/thumbs/${photo.id}`,
        working_url: `/media/working/${photo.id}`,
        capture_date: photo.capture_date,
        capture_date_precision: photo.capture_date_precision,
        capture_date_confirmed: photo.capture_date_confirmed,
        scan_batch: photo.scan_batch,
        scan_sequence: photo.scan_sequence,
        source_folder: photo.source_folder,
        source_filename: photo.source_filename,
        physical_ref_note: photo.physical_ref_note,
        rescan_wanted: photo.rescan_wanted,
        completeness_score: photo.completeness_score,
        description_ai: photo.description_ai,
        width: photo.width,
        height: photo.height,
        orientation: photo.orientation,
        has_no_people: photo.has_no_people,
        is_scan: photo.is_scan,
        exif: {
          taken_at: photo.exif_taken_at,
          camera:   photo.exif_camera,
          gps_lat:  photo.exif_gps_lat,
          gps_lon:  photo.exif_gps_lon,
        },
        faces: facesRes.rows.map((f) => ({
          id: Number(f.id),
          crop_url: `/media/faces/${f.id}`,
          person_id: f.person_id != null ? Number(f.person_id) : null,
          person_display_name: f.display_name,
          bbox: f.bbox,
          source: f.source,
          is_disputed: f.is_disputed,
          dispute_note: f.dispute_note,
          review_status: f.review_status,
        })),
        comments: commentsRes.rows.map((c) => ({
          id: Number(c.id),
          body: c.body,
          created_at: c.created_at,
          author_display_name: c.display_name || c.email || 'someone',
          user_id: c.user_id != null ? Number(c.user_id) : null,
        })),
        places: placesRes.rows,
        likes: {
          count: likesRes.rows[0].like_count,
          me:    likesRes.rows[0].liked_by_me || false,
        },
        backs: backsRes.rows.map((b) => ({
          id: Number(b.id),
          image_url: `/media/backs/${b.id}`,
          transcribed_text: b.transcribed_text,
          transcription_confidence: b.transcription_confidence,
          transcription_confirmed: b.transcription_confirmed,
        })),
        suggestions_pending: suggestionsRes.rows.map((s) => ({
          id: Number(s.id),
          kind: s.kind,
          payload: s.payload,
          confidence: s.confidence,
          source: s.source,
          created_at: s.created_at,
          author_display_name: canSeeSuggester
            ? (s.display_name || s.email || 'someone')
            : 'someone',
        })),
        albums: albumsRes.rows.map((a) => ({ id: Number(a.id), name: a.name })),
      });
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
