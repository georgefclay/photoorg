// Contributor content endpoints:
//   POST   /api/photos/:id/suggestions
//   POST   /api/photos/:id/faces         (draw a bbox → face + person suggestion)
//   POST   /api/faces/:id/dispute
//   POST   /api/photos/:id/comments
//   POST   /api/comments/:id/hide        (admin / moderator of a group the photo is in)
//   POST   /api/photos/:id/like          (toggle)
//
// All visibility-gated: a contributor can't post on a photo they can't see.
// Rate-limited per user (300 / hour, admins exempt).

const express = require('express');
const { requireUser, requireAdmin } = require('../middleware/require-user');
const { assertPhotoVisible, isModerator } = require('../middleware/visibility');
const { makeContribLimiter } = require('../middleware/contrib-rate-limit');
const { audit } = require('../services/audit');
const { parseDateFreetext } = require('../services/date-parse');

function toInt(v) { const n = parseInt(v, 10); return Number.isInteger(n) ? n : null; }

function validateBbox(b) {
  if (!b || typeof b !== 'object') return null;
  const x = Number(b.x), y = Number(b.y), w = Number(b.w), h = Number(b.h);
  if (![x, y, w, h].every(Number.isFinite)) return null;
  if (w <= 0 || h <= 0) return null;
  if (x < 0 || y < 0) return null;
  return { x, y, w, h };
}

// A contributor's "someone new" (answer A2): the person is only created when
// an admin accepts, from given_name / middle_name / surname / … — so a bare
// typed name ("Great Aunt Ada", or { display_name }) is split into given
// name + surname here. Returns null when there is no usable name.
const PERSON_TEXT_FIELDS = ['given_name', 'middle_name', 'surname', 'maiden_name', 'nickname', 'suffix', 'display_name'];
function normalizeNewPerson(np) {
  if (typeof np === 'string') np = { display_name: np };
  if (!np || typeof np !== 'object' || Array.isArray(np)) return null;
  const out = {};
  for (const k of PERSON_TEXT_FIELDS) {
    const v = typeof np[k] === 'string' ? np[k].trim().replace(/\s+/g, ' ').slice(0, 100) : '';
    if (v) out[k] = v;
  }
  if (!out.display_name && typeof np.name === 'string' && np.name.trim()) {
    out.display_name = np.name.trim().replace(/\s+/g, ' ').slice(0, 100);
  }
  for (const k of ['birth_year', 'death_year']) {
    const n = toInt(np[k]);
    if (n != null && n > 1700 && n < 2200) out[k] = n;
  }
  if (typeof np.notes === 'string' && np.notes.trim()) out.notes = np.notes.trim().slice(0, 1000);
  if (!out.given_name && !out.surname && !out.nickname && out.display_name) {
    const parts = out.display_name.split(' ');
    out.given_name = parts.length > 1 ? parts.slice(0, -1).join(' ') : parts[0];
    if (parts.length > 1) out.surname = parts[parts.length - 1];
  }
  return out.given_name || out.surname || out.nickname ? out : null;
}

function normalizeNewPlace(np) {
  const name = typeof np === 'string' ? np : (np && typeof np.name === 'string' ? np.name : '');
  const clean = name.trim().replace(/\s+/g, ' ').slice(0, 200);
  return clean ? { name: clean } : null;
}

module.exports = function apiContribRoutes({ pool }) {
  const router = express.Router();
  router.use(requireUser);
  router.use(express.json({ limit: '32kb' }));
  const contribLimit = makeContribLimiter();

  // ---- Suggestions ----------------------------------------------------

  router.post('/photos/:id(\\d+)/suggestions', contribLimit, async (req, res, next) => {
    const photoId = Number(req.params.id);
    if (!(await assertPhotoVisible(pool, req.user, photoId))) {
      return res.status(404).json({ error: 'not found' });
    }
    const b = req.body || {};
    const kind = String(b.kind || '');
    let payload;
    if (kind === 'date') {
      const text = String(b.text || b.date || '').trim();
      if (!text) return res.status(400).json({ error: 'need a date string' });
      const parsed = parseDateFreetext(text);
      if (!parsed) {
        return res.status(400).json({
          error: `couldn't understand "${text}". Try "1962", "March 1962", "1962-03-01", "sometime in the 60s"`,
        });
      }
      payload = { date: parsed.date, precision: parsed.precision, evidence: b.evidence || text };
    } else if (kind === 'person') {
      const personId = toInt(b.person_id);
      const faceId = toInt(b.face_id);
      if (!personId && !b.new_person) return res.status(400).json({ error: 'need person_id or new_person' });
      const newPerson = personId ? null : normalizeNewPerson(b.new_person);
      if (!personId && !newPerson) return res.status(400).json({ error: 'need a name for the new person' });
      payload = personId ? { person_id: personId } : { new_person: newPerson };
      if (faceId != null) {
        if (!Number.isSafeInteger(faceId)) return res.status(400).json({ error: 'bad face_id' });
        const onPhoto = await pool.query(
          `select 1 from faces where id = $1 and photo_id = $2 and is_deleted = false`, [faceId, photoId],
        );
        if (!onPhoto.rows.length) return res.status(400).json({ error: "that face isn't on this photo" });
        payload.face_id = faceId;
      }
    } else if (kind === 'place') {
      const placeId = toInt(b.place_id);
      if (!placeId && !b.new_place) return res.status(400).json({ error: 'need place_id or new_place' });
      const newPlace = placeId ? null : normalizeNewPlace(b.new_place);
      if (!placeId && !newPlace) return res.status(400).json({ error: 'need a name for the new place' });
      payload = placeId ? { place_id: placeId } : { new_place: newPlace };
    } else if (kind === 'description') {
      const text = String(b.text || '').trim();
      if (!text) return res.status(400).json({ error: 'need description text' });
      payload = { text };
    } else {
      return res.status(400).json({ error: 'bad kind (date|person|place|description)' });
    }

    const client = await pool.connect();
    try {
      await client.query('begin');
      const ins = await client.query(
        `insert into suggestions (photo_id, user_id, kind, payload, source, status)
         values ($1, $2, $3, $4::jsonb, 'human', 'pending')
         returning id`,
        [photoId, req.user.id, kind, payload],
      );
      await audit(client, {
        actor: req.user.email, action: 'suggestion.create',
        entityType: 'suggestion', entityId: ins.rows[0].id, userId: req.user.id,
        newValue: { kind, photo_id: photoId, payload },
      });
      await client.query('commit');
      res.status(201).json({ id: Number(ins.rows[0].id), kind, payload });
    } catch (err) {
      await client.query('rollback').catch(() => {});
      next(err);
    } finally {
      client.release();
    }
  });

  // ---- Face-tag (contributor draws a box) ------------------------------
  // Creates an unassigned `faces` row and a `person` suggestion carrying
  // that face_id. Admin acceptance sets `faces.person_id`.
  router.post('/photos/:id(\\d+)/faces', contribLimit, async (req, res, next) => {
    const photoId = Number(req.params.id);
    if (!(await assertPhotoVisible(pool, req.user, photoId))) {
      return res.status(404).json({ error: 'not found' });
    }
    const b = req.body || {};
    const bbox = validateBbox(b.bbox);
    if (!bbox) return res.status(400).json({ error: 'need a valid bbox {x,y,w,h}' });
    const personId = toInt(b.person_id);
    if (!personId && !b.new_person) return res.status(400).json({ error: 'need person_id or new_person' });
    const newPerson = personId ? null : normalizeNewPerson(b.new_person);
    if (!personId && !newPerson) return res.status(400).json({ error: 'need a name for the new person' });

    const client = await pool.connect();
    try {
      await client.query('begin');
      const faceIns = await client.query(
        `insert into faces (photo_id, person_id, bbox, source, created_by)
         values ($1, null, $2::jsonb, 'human', $3)
         returning id`,
        [photoId, bbox, req.user.id],
      );
      const faceId = Number(faceIns.rows[0].id);
      const payload = personId
        ? { person_id: personId, face_id: faceId }
        : { new_person: newPerson, face_id: faceId };
      const sugIns = await client.query(
        `insert into suggestions (photo_id, user_id, kind, payload, source, status)
         values ($1, $2, 'person', $3::jsonb, 'human', 'pending')
         returning id`,
        [photoId, req.user.id, payload],
      );
      await audit(client, {
        actor: req.user.email, action: 'face.create',
        entityType: 'face', entityId: faceId, userId: req.user.id,
        newValue: { photo_id: photoId, bbox, via: 'contributor_tag' },
      });
      await audit(client, {
        actor: req.user.email, action: 'suggestion.create',
        entityType: 'suggestion', entityId: sugIns.rows[0].id, userId: req.user.id,
        newValue: { kind: 'person', photo_id: photoId, payload },
      });
      await client.query('commit');
      res.status(201).json({ face_id: faceId, suggestion_id: Number(sugIns.rows[0].id) });
    } catch (err) {
      await client.query('rollback').catch(() => {});
      next(err);
    } finally {
      client.release();
    }
  });

  // ---- Dispute someone else's face tag --------------------------------
  router.post('/faces/:id(\\d+)/dispute', contribLimit, async (req, res, next) => {
    const faceId = Number(req.params.id);
    const note = String((req.body && req.body.note) || '').trim() || null;

    const client = await pool.connect();
    try {
      await client.query('begin');
      const faceRow = await client.query(
        `select f.id, f.photo_id, f.person_id, f.is_disputed, f.dispute_note
           from faces f
          where f.id = $1
            and f.is_deleted = false`,
        [faceId],
      );
      const face = faceRow.rows[0];
      if (!face) { await client.query('rollback'); return res.status(404).json({ error: 'not found' }); }
      // Visibility gate on the parent photo.
      const visible = await assertPhotoVisible(pool, req.user, Number(face.photo_id));
      if (!visible) { await client.query('rollback'); return res.status(404).json({ error: 'not found' }); }
      if (!face.person_id) { await client.query('rollback'); return res.status(400).json({ error: 'face has no person to dispute' }); }
      await client.query(
        `update faces
            set is_disputed = true,
                disputed_by = $1,
                dispute_note = $2
          where id = $3`,
        [req.user.id, note, faceId],
      );
      await audit(client, {
        actor: req.user.email, action: 'face.dispute',
        entityType: 'face', entityId: faceId, userId: req.user.id,
        previousValue: { is_disputed: face.is_disputed, dispute_note: face.dispute_note },
        newValue: { is_disputed: true, dispute_note: note },
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

  // ---- Comments -------------------------------------------------------
  router.post('/photos/:id(\\d+)/comments', contribLimit, async (req, res, next) => {
    const photoId = Number(req.params.id);
    if (!(await assertPhotoVisible(pool, req.user, photoId))) {
      return res.status(404).json({ error: 'not found' });
    }
    const body = String((req.body && req.body.body) || '').trim();
    if (!body) return res.status(400).json({ error: 'empty comment' });
    if (body.length > 4000) return res.status(400).json({ error: 'comment too long (max 4000 chars)' });

    const client = await pool.connect();
    try {
      await client.query('begin');
      const ins = await client.query(
        `insert into comments (photo_id, user_id, body) values ($1, $2, $3) returning id, created_at`,
        [photoId, req.user.id, body],
      );
      await audit(client, {
        actor: req.user.email, action: 'comment.create',
        entityType: 'comment', entityId: ins.rows[0].id, userId: req.user.id,
        newValue: { photo_id: photoId, body_preview: body.slice(0, 60) },
      });
      await client.query('commit');
      res.status(201).json({ id: Number(ins.rows[0].id), created_at: ins.rows[0].created_at });
    } catch (err) {
      await client.query('rollback').catch(() => {});
      next(err);
    } finally {
      client.release();
    }
  });

  // Hide/unhide: admin OR moderator of a group the photo is in.
  router.post('/comments/:id(\\d+)/hide', async (req, res, next) => {
    const id = Number(req.params.id);
    const client = await pool.connect();
    try {
      await client.query('begin');
      const row = (await client.query(
        `select c.id, c.photo_id, c.is_hidden from comments c where c.id = $1`, [id],
      )).rows[0];
      if (!row) { await client.query('rollback'); return res.status(404).json({ error: 'not found' }); }

      let allowed = req.user.role === 'admin';
      if (!allowed) {
        // Moderator of ANY group the photo is in?
        const modOk = await client.query(
          `select 1 from photo_groups pg
             join group_members gm using (group_id)
            where pg.photo_id = $1
              and pg.is_deleted = false
              and gm.user_id = $2
              and gm.role = 'moderator'
              and gm.is_deleted = false
            limit 1`,
          [row.photo_id, req.user.id],
        );
        allowed = modOk.rows.length > 0;
      }
      if (!allowed) { await client.query('rollback'); return res.status(403).json({ error: 'forbidden' }); }

      await client.query(
        `update comments
            set is_hidden = true,
                hidden_by = $1,
                hidden_at = now()
          where id = $2
            and is_hidden = false`,
        [req.user.id, id],
      );
      await audit(client, {
        actor: req.user.email, action: 'comment.hide',
        entityType: 'comment', entityId: id, userId: req.user.id,
        previousValue: { is_hidden: row.is_hidden },
        newValue: { is_hidden: true, photo_id: Number(row.photo_id) },
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

  router.post('/comments/:id(\\d+)/unhide', async (req, res, next) => {
    const id = Number(req.params.id);
    const client = await pool.connect();
    try {
      await client.query('begin');
      const row = (await client.query(
        `select c.id, c.photo_id, c.is_hidden from comments c where c.id = $1`, [id],
      )).rows[0];
      if (!row) { await client.query('rollback'); return res.status(404).json({ error: 'not found' }); }

      let allowed = req.user.role === 'admin';
      if (!allowed) {
        const modOk = await client.query(
          `select 1 from photo_groups pg
             join group_members gm using (group_id)
            where pg.photo_id = $1
              and pg.is_deleted = false
              and gm.user_id = $2
              and gm.role = 'moderator'
              and gm.is_deleted = false
            limit 1`,
          [row.photo_id, req.user.id],
        );
        allowed = modOk.rows.length > 0;
      }
      if (!allowed) { await client.query('rollback'); return res.status(403).json({ error: 'forbidden' }); }

      await client.query(
        `update comments set is_hidden = false, hidden_by = null, hidden_at = null
          where id = $1 and is_hidden = true`,
        [id],
      );
      await audit(client, {
        actor: req.user.email, action: 'comment.unhide',
        entityType: 'comment', entityId: id, userId: req.user.id,
        previousValue: { is_hidden: true },
        newValue: { is_hidden: false, photo_id: Number(row.photo_id) },
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

  // ---- Like toggle ----------------------------------------------------
  router.post('/photos/:id(\\d+)/like', contribLimit, async (req, res, next) => {
    const photoId = Number(req.params.id);
    if (!(await assertPhotoVisible(pool, req.user, photoId))) {
      return res.status(404).json({ error: 'not found' });
    }
    const client = await pool.connect();
    try {
      await client.query('begin');
      const existing = await client.query(
        `select 1 from likes where user_id = $1 and photo_id = $2`, [req.user.id, photoId],
      );
      let liked;
      if (existing.rows.length) {
        await client.query(`delete from likes where user_id = $1 and photo_id = $2`, [req.user.id, photoId]);
        liked = false;
      } else {
        await client.query(`insert into likes (user_id, photo_id) values ($1, $2)`, [req.user.id, photoId]);
        liked = true;
      }
      await audit(client, {
        actor: req.user.email, action: liked ? 'like.create' : 'like.delete',
        entityType: 'photo', entityId: photoId, userId: req.user.id,
        newValue: { liked },
      });
      const count = (await client.query(
        `select count(*)::int as n from likes where photo_id = $1`, [photoId],
      )).rows[0].n;
      await client.query('commit');
      res.json({ ok: true, liked, count });
    } catch (err) {
      await client.query('rollback').catch(() => {});
      next(err);
    } finally {
      client.release();
    }
  });

  return router;
};
