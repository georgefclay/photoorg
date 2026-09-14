// /api/people — read + contributor create; relationships as suggestions.
// Person, place, and album resources are global — a person exists across
// groups — but any list of PHOTOS derived from them is visibility-scoped.
// Face counts on a person page reflect only the photos the viewer can
// see (answer #5).

const express = require('express');
const { requireUser } = require('../middleware/require-user');
const { photoVisibleSql, assertPhotoVisible } = require('../middleware/visibility');
const { parseListQuery } = require('../services/pagination');
const { audit } = require('../services/audit');

function toInt(v) { const n = parseInt(v, 10); return Number.isInteger(n) ? n : null; }

function buildVisibleParams(user, initialParams = []) {
  const params = [...initialParams];
  if (user.role === 'admin') return { params, index: null };
  params.push(user.id);
  return { params, index: params.length };
}

module.exports = function apiPeopleRoutes({ pool }) {
  const router = express.Router();
  router.use(requireUser);
  router.use(express.json({ limit: '32kb' }));

  // GET /api/people
  router.get('/', async (req, res, next) => {
    try {
      const q = (req.query.q || '').trim();
      const { limit, cursor } = parseListQuery(req.query, { max: 200, def: 100 });

      const params = [];
      const clauses = ['p.is_deleted = false'];
      if (q) {
        params.push(`%${q.toLowerCase()}%`);
        clauses.push(`(lower(p.display_name) like $${params.length}
                      or exists (select 1 from person_name_variants v
                                  where v.person_id = p.id
                                    and lower(v.variant) like $${params.length}))`);
      }
      if (cursor != null) {
        params.push(cursor);
        clauses.push(`p.id < $${params.length}`);
      }
      params.push(limit);

      // Face count is scoped to visible photos. Admins see everything.
      const { params: vParams, index: uIdx } = buildVisibleParams(req.user, params.slice(0, params.length - 1));
      // Rebuild params ordered: filter/cursor params, then user id (if any), then limit.
      const finalParams = [...vParams, limit];
      const vSql = photoVisibleSql(req.user, {
        alias: 'ph',
        paramIndex: req.user.role === 'admin' ? 0 : uIdx,
      });

      const sql = `
        select p.id, p.given_name, p.middle_name, p.surname, p.maiden_name,
               p.nickname, p.suffix, p.display_name, p.birth_year, p.death_year,
               p.notes,
               (select count(*)::int
                  from faces f
                  join photos ph on ph.id = f.photo_id
                 where f.person_id = p.id
                   and f.is_deleted = false
                   and ${vSql}) as face_count
          from people p
         where ${clauses.join(' and ')}
         order by p.id desc
         limit $${finalParams.length}
      `;
      const { rows } = await pool.query(sql, finalParams);
      const next = rows.length === limit ? Number(rows[rows.length - 1].id) : null;
      res.json({
        items: rows.map((r) => ({
          id: Number(r.id),
          given_name: r.given_name,
          middle_name: r.middle_name,
          surname: r.surname,
          maiden_name: r.maiden_name,
          nickname: r.nickname,
          suffix: r.suffix,
          display_name: r.display_name,
          birth_year: r.birth_year,
          death_year: r.death_year,
          face_count: r.face_count,
        })),
        next,
      });
    } catch (err) { next(err); }
  });

  // GET /api/people/autocomplete?q=  → ≤ 10 results (prefix + trigram).
  router.get('/autocomplete', async (req, res, next) => {
    try {
      const q = (req.query.q || '').trim();
      if (!q) return res.json({ items: [] });
      const prefix = q.toLowerCase().replace(/[%_]/g, '') + '%';
      // Prefix on display_name and any variant field; trigram fallback
      // on display_name + variants.
      const { rows } = await pool.query(
        `with prefix_hits as (
           select p.id, p.display_name, 0 as rank
             from people p
            where p.is_deleted = false
              and (lower(p.display_name) like $1
                   or lower(coalesce(p.given_name, '')) like $1
                   or lower(coalesce(p.surname, '')) like $1
                   or lower(coalesce(p.nickname, '')) like $1
                   or lower(coalesce(p.maiden_name, '')) like $1
                   or exists (select 1 from person_name_variants v
                               where v.person_id = p.id
                                 and lower(v.variant) like $1))
            limit 10
         ),
         trigram_hits as (
           select p.id, p.display_name,
                  (1 - similarity(lower(p.display_name), $2))::float as rank
             from people p
            where p.is_deleted = false
              and lower(p.display_name) % $2
            order by rank asc
            limit 10
         )
         select id, display_name from prefix_hits
         union
         select id, display_name from trigram_hits
         limit 10`,
        [prefix, q.toLowerCase()],
      );
      res.json({ items: rows.map((r) => ({ id: Number(r.id), display_name: r.display_name })) });
    } catch (err) { next(err); }
  });

  // GET /api/people/:id  — full detail (visibility-scoped photo list).
  router.get('/:id(\\d+)', async (req, res, next) => {
    try {
      const id = Number(req.params.id);
      const personRes = await pool.query(
        `select id, given_name, middle_name, surname, maiden_name,
                nickname, suffix, display_name, birth_year, death_year, notes
           from people where id = $1 and is_deleted = false`, [id],
      );
      if (!personRes.rows[0]) return res.status(404).json({ error: 'not found' });

      const variantsRes = await pool.query(
        `select variant, kind from person_name_variants where person_id = $1 order by variant`, [id],
      );

      const relRes = await pool.query(
        `select r.id, r.person_a_id, r.person_b_id, r.type, r.confirmed,
                pa.display_name as person_a_display, pb.display_name as person_b_display
           from relationships r
           join people pa on pa.id = r.person_a_id
           join people pb on pb.id = r.person_b_id
          where r.person_a_id = $1 or r.person_b_id = $1
          order by r.id`,
        [id],
      );

      // Photos of this person, visibility-scoped, keyset-paginated.
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
        `exists (select 1 from faces f
                  where f.photo_id = ph.id
                    and f.person_id = $1
                    and f.is_deleted = false
                    and f.is_disputed = false)`,
        vSql,
      ];
      if (cursor != null) {
        params.push(cursor);
        clauses.push(`ph.id < $${params.length}`);
      }
      params.push(limit);

      const photosRes = await pool.query(
        `select ph.id, ph.capture_date, ph.capture_date_precision, ph.capture_date_confirmed,
                ph.completeness_score
           from photos ph
          where ${clauses.join(' and ')}
          order by ph.id desc
          limit $${params.length}`,
        params,
      );
      const photos = photosRes.rows.map((p) => ({
        id: Number(p.id),
        thumb_url: `/media/thumbs/${p.id}`,
        capture_date: p.capture_date,
        capture_date_precision: p.capture_date_precision,
        capture_date_confirmed: p.capture_date_confirmed,
        completeness_score: p.completeness_score,
      }));
      const nextCursor = photos.length === limit ? photos[photos.length - 1].id : null;

      res.json({
        ...personRes.rows[0],
        id: Number(personRes.rows[0].id),
        variants: variantsRes.rows,
        relationships: relRes.rows.map((r) => ({
          id: Number(r.id),
          person_a_id: Number(r.person_a_id),
          person_a_display: r.person_a_display,
          person_b_id: Number(r.person_b_id),
          person_b_display: r.person_b_display,
          type: r.type,
          confirmed: r.confirmed,
        })),
        photos,
        next: nextCursor,
      });
    } catch (err) { next(err); }
  });

  // POST /api/people — contributors may create (a person is not a fact
  // about a photo). Insert live immediately.
  router.post('/', async (req, res, next) => {
    const b = req.body || {};
    const given = String(b.given_name || '').trim() || null;
    const middle = String(b.middle_name || '').trim() || null;
    const surname = String(b.surname || '').trim() || null;
    const maiden = String(b.maiden_name || '').trim() || null;
    const nickname = String(b.nickname || '').trim() || null;
    const suffix = String(b.suffix || '').trim() || null;
    const notes = String(b.notes || '').trim() || null;
    const birth = toInt(b.birth_year);
    const death = toInt(b.death_year);
    if (!given && !surname && !nickname) {
      return res.status(400).json({ error: 'need at least given_name, surname, or nickname' });
    }
    const client = await pool.connect();
    try {
      await client.query('begin');
      const { rows } = await client.query(
        `insert into people (given_name, middle_name, surname, maiden_name,
                             nickname, suffix, birth_year, death_year, notes)
         values ($1,$2,$3,$4,$5,$6,$7,$8,$9)
         returning id, display_name`,
        [given, middle, surname, maiden, nickname, suffix, birth, death, notes],
      );
      const person = rows[0];
      await audit(client, {
        actor: req.user.email,
        action: 'person.create',
        entityType: 'person',
        entityId: person.id,
        userId: req.user.id,
        newValue: { given_name: given, surname, nickname, maiden_name: maiden, suffix, birth_year: birth, death_year: death },
      });
      await client.query('commit');
      res.status(201).json({ id: Number(person.id), display_name: person.display_name });
    } catch (err) {
      await client.query('rollback').catch(() => {});
      next(err);
    } finally {
      client.release();
    }
  });

  return router;
};

// Relationships live at /api/relationships. Contributors post a
// relationship as a suggestion; admin acceptance sets `confirmed = true`.
module.exports.relationshipsRouter = function relationshipsRouter({ pool }) {
  const router = express.Router();
  router.use(requireUser);
  router.use(express.json({ limit: '32kb' }));

  router.post('/', async (req, res, next) => {
    const b = req.body || {};
    const a = toInt(b.person_a_id);
    const bId = toInt(b.person_b_id);
    const type = String(b.type || '');
    if (!a || !bId || a === bId) return res.status(400).json({ error: 'bad person ids' });
    if (!['parent', 'spouse', 'sibling'].includes(type)) return res.status(400).json({ error: 'bad type' });

    const client = await pool.connect();
    try {
      await client.query('begin');
      // Check both people exist and are not deleted.
      const check = await client.query(
        `select count(*)::int as n from people where id = any($1::bigint[]) and is_deleted = false`,
        [[a, bId]],
      );
      if (check.rows[0].n !== 2) { await client.query('rollback'); return res.status(400).json({ error: 'person not found' }); }
      const ins = await client.query(
        `insert into suggestions (photo_id, user_id, kind, payload, source, status)
         values (null, $1, 'relationship', $2::jsonb, 'human', 'pending')
         returning id`,
        [req.user.id, { person_a_id: a, person_b_id: bId, type }],
      );
      await audit(client, {
        actor: req.user.email, action: 'suggestion.create',
        entityType: 'suggestion', entityId: ins.rows[0].id, userId: req.user.id,
        newValue: { kind: 'relationship', person_a_id: a, person_b_id: bId, type },
      });
      await client.query('commit');
      res.status(201).json({ id: Number(ins.rows[0].id) });
    } catch (err) {
      await client.query('rollback').catch(() => {});
      next(err);
    } finally {
      client.release();
    }
  });

  return router;
};
