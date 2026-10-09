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

  // PATCH /api/people/:id — admin only. Phase 15: a person's name is
  // human-visible text, so it must be correctable from the app a relative
  // is looking at, not only from the laptop.
  //
  // This is the cross-tier case. People are born on the desktop (all 118
  // of them), so the next push re-sends every column. `edited_on_web_at`
  // is what makes the correction stick: `/sync/people` keeps the newer
  // *human* edit, and `/sync/pull/web_edits` carries this one down to the
  // laptop. `updated_at` could not have done the job — every push sets it,
  // so on the web it means "when a push last touched this row".
  router.patch('/:id(\\d+)', requireAdmin, async (req, res, next) => {
    const id = Number(req.params.id);
    const b = req.body || {};
    // Only the fields actually supplied are touched, so a PATCH carrying
    // one corrected surname cannot blank out the notes.
    const TEXT = ['given_name', 'middle_name', 'surname', 'maiden_name',
                  'nickname', 'suffix', 'notes'];
    const YEARS = ['birth_year', 'death_year'];
    const fields = {};
    for (const k of TEXT) {
      if (Object.prototype.hasOwnProperty.call(b, k)) {
        fields[k] = String(b[k] == null ? '' : b[k]).trim() || null;
      }
    }
    for (const k of YEARS) {
      if (Object.prototype.hasOwnProperty.call(b, k)) {
        const v = b[k] === '' || b[k] == null ? null : toInt(b[k]);
        if (v != null && (v < 1 || v > 3000)) {
          return res.status(400).json({ error: `bad ${k}` });
        }
        fields[k] = v;
      }
    }
    if (!Object.keys(fields).length) return res.status(400).json({ error: 'nothing to change' });

    const client = await pool.connect();
    try {
      await client.query('begin');
      const prev = (await client.query(
        `select given_name, middle_name, surname, maiden_name, nickname, suffix,
                birth_year, death_year, notes, is_deleted
           from people where id = $1 for update`, [id],
      )).rows[0];
      if (!prev || prev.is_deleted) {
        await client.query('rollback');
        return res.status(404).json({ error: 'not found' });
      }
      const merged = { ...prev, ...fields };
      if (!merged.given_name && !merged.surname && !merged.nickname) {
        await client.query('rollback');
        return res.status(400).json({ error: 'need at least given_name, surname, or nickname' });
      }
      const changed = Object.keys(fields).filter((k) => prev[k] !== fields[k]);
      if (!changed.length) {
        await client.query('rollback');
        return res.json({ ok: true, changed: false });
      }

      const sets = changed.map((k, i) => `${k} = $${i + 2}`).join(', ');
      const { rows } = await client.query(
        `update people set ${sets}, edited_on_web_at = now(), updated_at = now()
          where id = $1 returning display_name`,
        [id, ...changed.map((k) => fields[k])],
      );
      await audit(client, {
        actor: req.user.email, action: 'person.update',
        entityType: 'person', entityId: id, userId: req.user.id,
        previousValue: Object.fromEntries(changed.map((k) => [k, prev[k]])),
        newValue: Object.fromEntries(changed.map((k) => [k, fields[k]])),
      });
      await client.query('commit');
      res.json({ ok: true, changed: true, display_name: rows[0].display_name });
    } catch (err) {
      await client.query('rollback').catch(() => {});
      next(err);
    } finally {
      client.release();
    }
  });

  // POST /api/people/:id/variants — admin only. A nickname or alternative
  // spelling is text the family searches by, so it gets an edit path too.
  router.post('/:id(\\d+)/variants', requireAdmin, async (req, res, next) => {
    const id = Number(req.params.id);
    const variant = String((req.body && req.body.variant) || '').trim();
    const kind = String((req.body && req.body.kind) || 'nickname');
    if (!variant) return res.status(400).json({ error: 'variant required' });
    if (variant.length > 200) return res.status(400).json({ error: 'variant too long' });
    // The `name_variant_kind` enum, exactly.
    if (!['nickname', 'misspelling', 'alternate_spelling'].includes(kind)) {
      return res.status(400).json({ error: 'bad kind' });
    }
    const client = await pool.connect();
    try {
      await client.query('begin');
      const person = (await client.query(
        'select 1 from people where id = $1 and is_deleted = false', [id],
      )).rows[0];
      if (!person) { await client.query('rollback'); return res.status(404).json({ error: 'not found' }); }
      const ins = await client.query(
        `insert into person_name_variants (person_id, variant, kind, edited_on_web_at)
         values ($1, $2, $3, now())
         on conflict do nothing
         returning id`,
        [id, variant, kind],
      );
      if (!ins.rowCount) {
        await client.query('rollback');
        return res.status(409).json({ error: 'that variant is already on this person' });
      }
      await audit(client, {
        actor: req.user.email, action: 'person.variant.add',
        entityType: 'person', entityId: id, userId: req.user.id,
        newValue: { variant, kind, variant_id: Number(ins.rows[0].id) },
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

  // PATCH /api/people/:id/variants/:variantId — correct a variant's text.
  // A *web-born* variant (id at or above the floor) is web-authoritative
  // and never travels, so it is edited here outright; a desktop-born one
  // goes through the same LWW as the person's own name.
  router.patch('/:id(\\d+)/variants/:variantId(\\d+)', requireAdmin, async (req, res, next) => {
    const personId = Number(req.params.id);
    const variantId = Number(req.params.variantId);
    const variant = String((req.body && req.body.variant) || '').trim();
    if (!variant) return res.status(400).json({ error: 'variant required' });
    if (variant.length > 200) return res.status(400).json({ error: 'variant too long' });
    const client = await pool.connect();
    try {
      await client.query('begin');
      const prev = (await client.query(
        `select variant, kind from person_name_variants
          where id = $1 and person_id = $2 for update`, [variantId, personId],
      )).rows[0];
      if (!prev) { await client.query('rollback'); return res.status(404).json({ error: 'not found' }); }
      if (prev.variant === variant) {
        await client.query('rollback');
        return res.json({ ok: true, changed: false });
      }
      const upd = await client.query(
        `update person_name_variants set variant = $1, edited_on_web_at = now()
          where id = $2 returning id`,
        [variant, variantId],
      );
      if (!upd.rowCount) { await client.query('rollback'); return res.status(404).json({ error: 'not found' }); }
      await audit(client, {
        actor: req.user.email, action: 'person.variant.edit',
        entityType: 'person', entityId: personId, userId: req.user.id,
        previousValue: { variant_id: variantId, variant: prev.variant },
        newValue: { variant_id: variantId, variant },
      });
      await client.query('commit');
      res.json({ ok: true, changed: true });
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
