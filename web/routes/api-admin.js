// Admin API routes:
//   GET  /api/admin/suggestions?status=pending&kind=
//   POST /api/admin/suggestions/:id/accept     (with 409 on conflicting confirmed value)
//   POST /api/admin/suggestions/:id/reject
//   GET  /api/admin/disputes
//   POST /api/admin/faces/:id/resolve          (keep | unassign | reassign)
//   GET  /api/admin/audit?entity_type=&entity_id=&limit=&cursor=
//   GET  /api/admin/report/monthly?month=YYYY-MM
//   GET  /api/admin/rescan-list                (JSON)
//   GET  /admin/rescan-list                    (printable page; wired elsewhere)
//   GET  /api/admin/unfiled                    (paginated list of unfiled photos)

const express = require('express');
const { requireAdmin } = require('../middleware/require-user');
const { audit } = require('../services/audit');
const { parseListQuery } = require('../services/pagination');

function toInt(v) { const n = parseInt(v, 10); return Number.isInteger(n) ? n : null; }

module.exports = function apiAdminRoutes({ pool }) {
  const router = express.Router();
  router.use(requireAdmin);
  router.use(express.json({ limit: '32kb' }));

  // -----------------------------------------------------------------
  //  Suggestions queue
  // -----------------------------------------------------------------
  router.get('/suggestions', async (req, res, next) => {
    try {
      const { limit, cursor } = parseListQuery(req.query, { max: 200, def: 100 });
      const params = [];
      const clauses = [];
      const status = String(req.query.status || 'pending');
      params.push(status);
      clauses.push(`s.status = $${params.length}`);
      if (req.query.kind) {
        params.push(String(req.query.kind));
        clauses.push(`s.kind = $${params.length}`);
      }
      if (cursor != null) {
        params.push(cursor);
        clauses.push(`s.id < $${params.length}`);
      }
      params.push(limit);
      const { rows } = await pool.query(
        `select s.id, s.photo_id, s.user_id, s.kind, s.payload, s.confidence,
                s.source, s.status, s.model, s.created_at,
                u.display_name as user_display_name, u.email as user_email
           from suggestions s
      left join users u on u.id = s.user_id
          where ${clauses.join(' and ')}
          order by s.id desc
          limit $${params.length}`,
        params,
      );
      const next = rows.length === limit ? Number(rows[rows.length - 1].id) : null;
      res.json({
        items: rows.map((r) => ({
          id: Number(r.id),
          photo_id: r.photo_id != null ? Number(r.photo_id) : null,
          user_id: r.user_id != null ? Number(r.user_id) : null,
          user_display_name: r.user_display_name || r.user_email,
          kind: r.kind, payload: r.payload, confidence: r.confidence,
          source: r.source, status: r.status, model: r.model, created_at: r.created_at,
        })),
        next,
      });
    } catch (err) { next(err); }
  });

  router.post('/suggestions/:id(\\d+)/accept', async (req, res, next) => {
    const id = Number(req.params.id);
    const force = req.body && (req.body.force === true || req.body.force === 'true');
    const note = String((req.body && req.body.note) || '').trim() || null;
    const client = await pool.connect();
    try {
      await client.query('begin');
      const sug = (await client.query(
        `select id, photo_id, user_id, kind, payload, status
           from suggestions where id = $1 for update`, [id],
      )).rows[0];
      if (!sug) { await client.query('rollback'); return res.status(404).json({ error: 'not found' }); }
      if (sug.status !== 'pending') { await client.query('rollback'); return res.status(409).json({ error: 'already resolved', status: sug.status }); }

      const payload = sug.payload || {};
      const result = { kind: sug.kind };

      if (sug.kind === 'date') {
        if (!sug.photo_id) { await client.query('rollback'); return res.status(400).json({ error: 'date suggestion has no photo_id' }); }
        const photo = (await client.query(
          `select capture_date, capture_date_precision, capture_date_confirmed
             from photos where id = $1 for update`, [sug.photo_id],
        )).rows[0];
        if (!photo) { await client.query('rollback'); return res.status(404).json({ error: 'photo not found' }); }
        const prev = {
          capture_date: photo.capture_date,
          capture_date_precision: photo.capture_date_precision,
          capture_date_confirmed: photo.capture_date_confirmed,
        };
        const wantDate = payload.date;
        const wantPrecision = payload.precision || 'exact';
        if (photo.capture_date_confirmed && !force
            && (String(prev.capture_date) !== String(wantDate)
                || prev.capture_date_precision !== wantPrecision)) {
          await client.query('rollback');
          return res.status(409).json({
            error: 'photo already has a confirmed date',
            current: prev,
            proposed: { capture_date: wantDate, capture_date_precision: wantPrecision },
          });
        }
        await client.query(
          `update photos
              set capture_date = $1,
                  capture_date_precision = $2,
                  capture_date_confirmed = true
            where id = $3`,
          [wantDate, wantPrecision, sug.photo_id],
        );
        await audit(client, {
          actor: req.user.email, action: 'photo.capture_date.set',
          entityType: 'photo', entityId: sug.photo_id, userId: req.user.id,
          previousValue: prev,
          newValue: { capture_date: wantDate, capture_date_precision: wantPrecision, capture_date_confirmed: true, via: 'suggestion.accept', suggestion_id: id, force: !!force },
        });
        await client.query(`select refresh_completeness($1)`, [sug.photo_id]);
        result.applied = { photo_id: Number(sug.photo_id), capture_date: wantDate, capture_date_precision: wantPrecision };
      }
      else if (sug.kind === 'person') {
        const faceId = toInt(payload.face_id);
        if (!faceId) { await client.query('rollback'); return res.status(400).json({ error: 'person suggestion has no face_id' }); }
        // Resolve target person id (existing or new_person).
        let personId = toInt(payload.person_id);
        if (!personId && payload.new_person) {
          const np = payload.new_person;
          const ins = await client.query(
            `insert into people (given_name, middle_name, surname, maiden_name, nickname, suffix, birth_year, death_year, notes)
             values ($1,$2,$3,$4,$5,$6,$7,$8,$9)
             returning id`,
            [np.given_name || null, np.middle_name || null, np.surname || null,
             np.maiden_name || null, np.nickname || null, np.suffix || null,
             toInt(np.birth_year), toInt(np.death_year), np.notes || null],
          );
          personId = Number(ins.rows[0].id);
          await audit(client, {
            actor: req.user.email, action: 'person.create',
            entityType: 'person', entityId: personId, userId: req.user.id,
            newValue: { via: 'suggestion.accept', suggestion_id: id, ...np },
          });
        }
        if (!personId) { await client.query('rollback'); return res.status(400).json({ error: 'no person to assign' }); }
        const face = (await client.query(
          `select id, photo_id, person_id, is_disputed from faces where id = $1 for update`,
          [faceId],
        )).rows[0];
        if (!face) { await client.query('rollback'); return res.status(404).json({ error: 'face not found' }); }
        if (face.person_id && Number(face.person_id) !== personId && !force) {
          await client.query('rollback');
          return res.status(409).json({
            error: 'face already assigned',
            current: { face_id: faceId, person_id: Number(face.person_id) },
            proposed: { face_id: faceId, person_id: personId },
          });
        }
        await client.query(
          `update faces
              set person_id = $1,
                  source = 'human',
                  is_disputed = false,
                  dispute_note = null,
                  created_by = $2
            where id = $3`,
          [personId, req.user.id, faceId],
        );
        await audit(client, {
          actor: req.user.email, action: 'face.assign',
          entityType: 'face', entityId: faceId, userId: req.user.id,
          previousValue: { person_id: face.person_id != null ? Number(face.person_id) : null, is_disputed: face.is_disputed },
          newValue: { person_id: personId, via: 'suggestion.accept', suggestion_id: id, force: !!force },
        });
        if (face.photo_id) await client.query(`select refresh_completeness($1)`, [face.photo_id]);
        result.applied = { face_id: faceId, person_id: personId, photo_id: face.photo_id != null ? Number(face.photo_id) : null };
      }
      else if (sug.kind === 'place') {
        if (!sug.photo_id) { await client.query('rollback'); return res.status(400).json({ error: 'place suggestion has no photo_id' }); }
        let placeId = toInt(payload.place_id);
        if (!placeId && payload.new_place) {
          const np = payload.new_place;
          const ins = await client.query(
            `insert into places (name, latitude, longitude, notes)
             values ($1, $2, $3, $4)
             on conflict do nothing
             returning id`,
            [np.name, np.latitude || null, np.longitude || null, np.notes || null],
          );
          if (ins.rows[0]) placeId = Number(ins.rows[0].id);
          else {
            const found = await client.query(
              `select id from places where lower(name) = lower($1) and is_deleted = false limit 1`, [np.name],
            );
            placeId = found.rows[0] ? Number(found.rows[0].id) : null;
          }
        }
        if (!placeId) { await client.query('rollback'); return res.status(400).json({ error: 'no place to assign' }); }
        await client.query(
          `insert into photo_places (photo_id, place_id, confirmed)
           values ($1, $2, true)
           on conflict (photo_id, place_id) do update set confirmed = true`,
          [sug.photo_id, placeId],
        );
        await audit(client, {
          actor: req.user.email, action: 'photo.place.set',
          entityType: 'photo', entityId: sug.photo_id, userId: req.user.id,
          newValue: { place_id: placeId, via: 'suggestion.accept', suggestion_id: id },
        });
        await client.query(`select refresh_completeness($1)`, [sug.photo_id]);
        result.applied = { photo_id: Number(sug.photo_id), place_id: placeId };
      }
      else if (sug.kind === 'description') {
        if (!sug.photo_id) { await client.query('rollback'); return res.status(400).json({ error: 'description suggestion has no photo_id' }); }
        const photo = (await client.query(
          `select description_ai from photos where id = $1 for update`, [sug.photo_id],
        )).rows[0];
        if (!photo) { await client.query('rollback'); return res.status(404).json({ error: 'photo not found' }); }
        const wantText = String(payload.text || '').trim();
        if (photo.description_ai && photo.description_ai !== wantText && !force) {
          await client.query('rollback');
          return res.status(409).json({
            error: 'photo already has a description',
            current: { description_ai: photo.description_ai },
            proposed: { description_ai: wantText },
          });
        }
        await client.query(`update photos set description_ai = $1 where id = $2`, [wantText, sug.photo_id]);
        await audit(client, {
          actor: req.user.email, action: 'photo.description.set',
          entityType: 'photo', entityId: sug.photo_id, userId: req.user.id,
          previousValue: { description_ai: photo.description_ai },
          newValue: { description_ai: wantText, via: 'suggestion.accept', suggestion_id: id, force: !!force },
        });
        result.applied = { photo_id: Number(sug.photo_id) };
      }
      else if (sug.kind === 'relationship') {
        const a = toInt(payload.person_a_id);
        const b = toInt(payload.person_b_id);
        const type = String(payload.type || '');
        if (!a || !b || a === b) { await client.query('rollback'); return res.status(400).json({ error: 'bad relationship payload' }); }
        if (!['parent', 'spouse', 'sibling'].includes(type)) { await client.query('rollback'); return res.status(400).json({ error: 'bad type' }); }
        await client.query(
          `insert into relationships (person_a_id, person_b_id, type, confirmed)
           values ($1,$2,$3,true)
           on conflict (person_a_id, person_b_id, type) do update set confirmed = true
           returning id`,
          [a, b, type],
        );
        await audit(client, {
          actor: req.user.email, action: 'relationship.confirm',
          entityType: 'relationship', entityId: null, userId: req.user.id,
          newValue: { person_a_id: a, person_b_id: b, type, via: 'suggestion.accept', suggestion_id: id },
        });
        result.applied = { person_a_id: a, person_b_id: b, type };
      }
      else if (sug.kind === 'classification' && payload.label === 'no_people') {
        if (!sug.photo_id) { await client.query('rollback'); return res.status(400).json({ error: 'classification suggestion has no photo_id' }); }
        const photo = (await client.query(
          `select has_no_people from photos where id = $1 for update`, [sug.photo_id],
        )).rows[0];
        if (!photo) { await client.query('rollback'); return res.status(404).json({ error: 'photo not found' }); }
        await client.query(`update photos set has_no_people = true where id = $1`, [sug.photo_id]);
        await audit(client, {
          actor: req.user.email, action: 'photo.has_no_people.set',
          entityType: 'photo', entityId: sug.photo_id, userId: req.user.id,
          previousValue: { has_no_people: photo.has_no_people },
          newValue: { has_no_people: true, via: 'suggestion.accept', suggestion_id: id },
        });
        await client.query(`select refresh_completeness($1)`, [sug.photo_id]);
        result.applied = { photo_id: Number(sug.photo_id) };
      }
      else if (sug.kind === 'transcription' || sug.kind === 'classification') {
        // transcription is handled by the desktop side (photo_backs); a
        // pending web-side suggestion of that kind is unusual — we accept
        // it into the pending resolution log but do no fact write here.
        result.applied = { note: 'kind is desktop-authoritative; suggestion marked accepted only' };
      }
      else {
        await client.query('rollback');
        return res.status(400).json({ error: `unsupported kind ${sug.kind}` });
      }

      await client.query(
        `update suggestions
            set status = 'accepted',
                resolved_by = $1,
                resolved_at = now(),
                resolution_note = $2
          where id = $3`,
        [req.user.id, note, id],
      );
      await audit(client, {
        actor: req.user.email, action: 'suggestion.accept',
        entityType: 'suggestion', entityId: id, userId: req.user.id,
        previousValue: { status: 'pending' },
        newValue: { status: 'accepted', ...result },
      });
      await client.query('commit');
      res.json({ ok: true, id, ...result });
    } catch (err) {
      await client.query('rollback').catch(() => {});
      next(err);
    } finally {
      client.release();
    }
  });

  router.post('/suggestions/:id(\\d+)/reject', async (req, res, next) => {
    const id = Number(req.params.id);
    const note = String((req.body && req.body.note) || '').trim() || null;
    const client = await pool.connect();
    try {
      await client.query('begin');
      const row = (await client.query(
        `select status from suggestions where id = $1 for update`, [id],
      )).rows[0];
      if (!row) { await client.query('rollback'); return res.status(404).json({ error: 'not found' }); }
      if (row.status !== 'pending') { await client.query('rollback'); return res.status(409).json({ error: 'already resolved' }); }
      await client.query(
        `update suggestions set status = 'rejected', resolved_by = $1, resolved_at = now(), resolution_note = $2 where id = $3`,
        [req.user.id, note, id],
      );
      await audit(client, {
        actor: req.user.email, action: 'suggestion.reject',
        entityType: 'suggestion', entityId: id, userId: req.user.id,
        previousValue: { status: 'pending' }, newValue: { status: 'rejected', note },
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

  // -----------------------------------------------------------------
  //  Disputes queue and resolve
  // -----------------------------------------------------------------
  router.get('/disputes', async (req, res, next) => {
    try {
      const { rows } = await pool.query(
        `select f.id, f.photo_id, f.person_id, f.bbox, f.disputed_by, f.dispute_note,
                p.display_name as person_display_name,
                u.display_name as disputed_by_display, u.email as disputed_by_email
           from faces f
      left join people p on p.id = f.person_id
      left join users u on u.id = f.disputed_by
          where f.is_disputed = true
            and f.is_deleted = false
          order by f.id desc`,
      );
      res.json({
        items: rows.map((r) => ({
          face_id: Number(r.id),
          photo_id: Number(r.photo_id),
          person_id: r.person_id != null ? Number(r.person_id) : null,
          person_display_name: r.person_display_name,
          bbox: r.bbox,
          disputed_by: r.disputed_by_display || r.disputed_by_email,
          dispute_note: r.dispute_note,
        })),
      });
    } catch (err) { next(err); }
  });

  router.post('/faces/:id(\\d+)/resolve', async (req, res, next) => {
    const id = Number(req.params.id);
    const action = String((req.body && req.body.action) || '');
    const targetPersonId = toInt(req.body && req.body.person_id);
    const client = await pool.connect();
    try {
      await client.query('begin');
      const face = (await client.query(
        `select id, photo_id, person_id, is_disputed, dispute_note from faces where id = $1 for update`, [id],
      )).rows[0];
      if (!face) { await client.query('rollback'); return res.status(404).json({ error: 'not found' }); }
      const prev = { person_id: face.person_id != null ? Number(face.person_id) : null, is_disputed: face.is_disputed };
      let next;
      if (action === 'keep') {
        await client.query(
          `update faces set is_disputed = false, dispute_note = null where id = $1`, [id],
        );
        next = { ...prev, is_disputed: false };
      } else if (action === 'unassign') {
        await client.query(
          `update faces set person_id = null, is_disputed = false, dispute_note = null where id = $1`, [id],
        );
        next = { person_id: null, is_disputed: false };
      } else if (action === 'reassign') {
        if (!targetPersonId) { await client.query('rollback'); return res.status(400).json({ error: 'need person_id' }); }
        await client.query(
          `update faces set person_id = $1, source = 'human', is_disputed = false, dispute_note = null where id = $2`,
          [targetPersonId, id],
        );
        next = { person_id: targetPersonId, is_disputed: false };
      } else {
        await client.query('rollback');
        return res.status(400).json({ error: 'action must be keep|unassign|reassign' });
      }
      await audit(client, {
        actor: req.user.email, action: 'face.dispute.resolve',
        entityType: 'face', entityId: id, userId: req.user.id,
        previousValue: prev, newValue: { ...next, via: action },
      });
      if (face.photo_id) await client.query(`select refresh_completeness($1)`, [face.photo_id]);
      await client.query('commit');
      res.json({ ok: true });
    } catch (err) {
      await client.query('rollback').catch(() => {});
      next(err);
    } finally {
      client.release();
    }
  });

  // -----------------------------------------------------------------
  //  Audit log browser
  // -----------------------------------------------------------------
  router.get('/audit', async (req, res, next) => {
    try {
      const { limit, cursor } = parseListQuery(req.query, { max: 500, def: 100 });
      const params = [];
      const clauses = [];
      if (req.query.entity_type) {
        params.push(String(req.query.entity_type));
        clauses.push(`entity_type = $${params.length}`);
      }
      const eid = toInt(req.query.entity_id);
      if (eid != null) { params.push(eid); clauses.push(`entity_id = $${params.length}`); }
      if (req.query.action) {
        params.push(String(req.query.action));
        clauses.push(`action = $${params.length}`);
      }
      if (cursor != null) {
        params.push(cursor);
        clauses.push(`id < $${params.length}`);
      }
      const where = clauses.length ? `where ${clauses.join(' and ')}` : '';
      params.push(limit);
      const { rows } = await pool.query(
        `select id, user_id, actor, action, entity_type, entity_id,
                previous_value, new_value, created_at
           from audit_log
           ${where}
           order by id desc
           limit $${params.length}`,
        params,
      );
      const next = rows.length === limit ? Number(rows[rows.length - 1].id) : null;
      res.json({
        items: rows.map((r) => ({ ...r, id: Number(r.id), entity_id: r.entity_id != null ? Number(r.entity_id) : null })),
        next,
      });
    } catch (err) { next(err); }
  });

  // -----------------------------------------------------------------
  //  Monthly report — from audit_log + likes.
  // -----------------------------------------------------------------
  router.get('/report/monthly', async (req, res, next) => {
    try {
      const month = String(req.query.month || '').match(/^\d{4}-\d{2}$/) ? req.query.month : null;
      if (!month) return res.status(400).json({ error: 'month=YYYY-MM required' });
      const start = `${month}-01`;
      const perUserRes = await pool.query(
        `with in_month as (
           select * from audit_log
            where created_at >= $1::date and created_at < ($1::date + interval '1 month')
         )
         select
           u.id as user_id,
           u.display_name, u.email,
           count(*) filter (where a.action = 'auth.login')            as logins,
           count(*) filter (where a.action = 'face.create')           as faces_tagged,
           count(*) filter (where a.action = 'comment.create')        as comments,
           count(*) filter (where a.action in ('like.create','like.delete')) as likes_toggled,
           count(*) filter (where a.action = 'suggestion.create')     as suggestions_made,
           count(*) filter (where a.action = 'suggestion.accept')     as suggestions_accepted,
           count(*) filter (where a.action = 'suggestion.reject')     as suggestions_rejected,
           count(*) filter (where a.action = 'photo.capture_date.set') as dates_confirmed
         from users u
    left join in_month a on a.user_id = u.id
        group by u.id, u.display_name, u.email
        having count(a.*) > 0
        order by u.display_name`,
        [start],
      );
      const totalLikes = (await pool.query(
        `select count(*)::int as n from likes
          where created_at >= $1::date and created_at < ($1::date + interval '1 month')`,
        [start],
      )).rows[0].n;
      res.json({
        month,
        per_user: perUserRes.rows.map((r) => ({
          user_id: r.user_id != null ? Number(r.user_id) : null,
          display_name: r.display_name || r.email,
          logins: Number(r.logins),
          faces_tagged: Number(r.faces_tagged),
          comments: Number(r.comments),
          likes_toggled: Number(r.likes_toggled),
          suggestions_made: Number(r.suggestions_made),
          suggestions_accepted: Number(r.suggestions_accepted),
          suggestions_rejected: Number(r.suggestions_rejected),
          dates_confirmed: Number(r.dates_confirmed),
        })),
        total_likes_in_month: totalLikes,
      });
    } catch (err) { next(err); }
  });

  // -----------------------------------------------------------------
  //  Rescan list — grouped by scan_batch, ordered by scan_sequence.
  // -----------------------------------------------------------------
  router.get('/rescan-list', async (req, res, next) => {
    try {
      const { rows } = await pool.query(
        `select scan_batch, scan_sequence, id, source_filename, physical_ref_note
           from photos
          where rescan_wanted = true
            and is_deleted = false
          order by scan_batch nulls last, scan_sequence nulls last, id`,
      );
      const groups = {};
      for (const r of rows) {
        const key = r.scan_batch || '(no batch)';
        if (!groups[key]) groups[key] = [];
        groups[key].push({
          id: Number(r.id),
          thumb_url: `/media/thumbs/${r.id}`,
          scan_sequence: r.scan_sequence,
          source_filename: r.source_filename,
          physical_ref_note: r.physical_ref_note,
        });
      }
      res.json({
        batches: Object.entries(groups).map(([batch, items]) => ({ batch, items })),
        total: rows.length,
      });
    } catch (err) { next(err); }
  });

  // -----------------------------------------------------------------
  //  Unfiled queue — photos in zero live groups (admin-only visibility).
  // -----------------------------------------------------------------
  router.get('/unfiled', async (req, res, next) => {
    try {
      const { limit, cursor } = parseListQuery(req.query);
      const params = [];
      const clauses = [
        `p.is_deleted = false`,
        `p.is_private = false`,
        `not exists (
          select 1 from photo_groups pg
           where pg.photo_id = p.id
             and pg.is_deleted = false
        )`,
      ];
      if (cursor != null) { params.push(cursor); clauses.push(`p.id < $${params.length}`); }
      params.push(limit);
      const { rows } = await pool.query(
        `select p.id, p.capture_date, p.capture_date_confirmed, p.completeness_score,
                p.scan_batch, p.source_folder
           from photos p
          where ${clauses.join(' and ')}
          order by p.id desc
          limit $${params.length}`,
        params,
      );
      const next = rows.length === limit ? Number(rows[rows.length - 1].id) : null;
      res.json({
        items: rows.map((r) => ({
          id: Number(r.id),
          thumb_url: `/media/thumbs/${r.id}`,
          capture_date: r.capture_date,
          capture_date_confirmed: r.capture_date_confirmed,
          completeness_score: r.completeness_score,
          scan_batch: r.scan_batch,
          source_folder: r.source_folder,
        })),
        next,
      });
    } catch (err) { next(err); }
  });

  return router;
};
