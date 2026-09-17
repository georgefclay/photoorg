// /api/people — read (services/people.js) + admin create; relationships as
// suggestions.
// Person, place, and album resources are global — a person exists across
// groups — but any list of PHOTOS derived from them is visibility-scoped.
// Face counts on a person page reflect only the photos the viewer can
// see (answer #5).

const express = require('express');
const { requireUser, requireAdmin } = require('../middleware/require-user');
const { audit } = require('../services/audit');
const { getScope } = require('../services/scope');
const { listPhotos } = require('../services/photos');
const { listPeople, autocompletePeople, getPerson } = require('../services/people');
const { suggestRelationship } = require('../services/people-pages');

function toInt(v) { const n = parseInt(v, 10); return Number.isInteger(n) ? n : null; }


module.exports = function apiPeopleRoutes({ pool }) {
  const router = express.Router();
  router.use(requireUser);
  router.use(express.json({ limit: '32kb' }));

  // GET /api/people?q=&cursor= — name-sorted, counts scoped.
  router.get('/', async (req, res, next) => {
    try {
      const { scope } = await getScope(req, pool);
      res.json(await listPeople(pool, req.user, {
        q: req.query.q, cursor: req.query.cursor, limit: req.query.limit, scope,
      }));
    } catch (err) { next(err); }
  });

  // GET /api/people/autocomplete?q=  → ≤ 10 results (prefix + trigram).
  router.get('/autocomplete', async (req, res, next) => {
    try {
      res.json({ items: await autocompletePeople(pool, req.query.q) });
    } catch (err) { next(err); }
  });

  // GET /api/people/:id — names, relationships, visible + scoped photos.
  router.get('/:id(\\d+)', async (req, res, next) => {
    try {
      const id = Number(req.params.id);
      const person = await getPerson(pool, id);
      if (!person) return res.status(404).json({ error: 'not found' });
      const { scope } = await getScope(req, pool);
      const photos = await listPhotos(pool, req.user, {
        filters: { person_id: id }, cursor: req.query.cursor, limit: req.query.limit, scope,
      });
      res.json({ ...person, photos: photos.items, next: photos.next });
    } catch (err) { next(err); }
  });

  // POST /api/people — admin only (Phase 10, answer A2). Contributors
  // name a new person inside a `person` suggestion; nothing exists until
  // an admin accepts it.
  router.post('/', requireAdmin, async (req, res, next) => {
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
    try {
      // Shared with the no-JS form on /people/:id (routes/pages-people.js).
      const r = await suggestRelationship(pool, req.user, { person_a_id: a, person_b_id: bId, type });
      if (r.error) return res.status(400).json({ error: r.error });
      if (r.already) return res.status(409).json({ error: 'already recorded' });
      if (r.duplicate) return res.status(200).json({ id: r.id, duplicate: true });
      res.status(201).json({ id: r.id });
    } catch (err) { next(err); }
  });

  return router;
};
