// Phase 13 fix-up 2 — the description correction loop on the site.
//
// A member proposes a description (pending web-born suggestion, shown on the
// photo page); an admin accepts it from the queue (409 with both texts when
// the photo already has one, `force` replaces) and the
// `photo.description.set` row goes down /sync/pull/confirmed. An admin's own
// edit skips the queue: `photo.description.edit` with previous and new, the
// same pull, and an empty text clears the description.
const test = require('node:test');
const { after, before, beforeEach } = require('node:test');
const {
  pool, makeApp, assert, request, resetDb, seedWorld, agentFor,
} = require('./page-helpers');

let app;
before(() => { app = makeApp(); });
after(async () => { await pool.end(); });
beforeEach(resetDb);

const SVC = () => `Bearer ${process.env.SERVICE_TOKEN}`;
const editUrl = (id) => `/api/admin/photos/${id}/description`;
const desc = async (id) => (await pool.query(`select description_ai from photos where id = $1`, [id])).rows[0].description_ai;
const pulled = async (action) => (await request(app).get('/sync/pull/confirmed').set('Authorization', SVC()))
  .body.fact_audits.filter((x) => x.action === action);

test('the photo page offers a member "Suggest a description" and an admin "Edit description"', async () => {
  const w = await seedWorld();
  const alice = await agentFor(app, w.alice);
  const m = await alice.get(`/photos/${w.photos.clay1}`);
  assert.equal(m.status, 200);
  assert.match(m.text, /Suggest a description/);
  assert.match(m.text, new RegExp(`data-desc-form[^>]*data-post-url="/api/photos/${w.photos.clay1}/suggestions"`));
  assert.match(m.text, /<textarea[^>]*data-desc-input[^>]*>Easter picnic at the lake<\/textarea>/);
  assert.doesNotMatch(m.text, /data-admin data-url/);

  const admin = await agentFor(app, w.admin);
  const a = await admin.get(`/photos/${w.photos.clay1}`);
  assert.match(a.text, /Edit description/);
  assert.match(a.text, new RegExp(`data-admin data-url="${editUrl(w.photos.clay1)}"`));
});

test('member suggests, admin accepts from the queue, the fact is pulled', async () => {
  const w = await seedWorld();
  const id = w.photos.clay1;
  const alice = await agentFor(app, w.alice);
  const s = await alice.post(`/api/photos/${id}/suggestions`).set('X-CSRF-Token', alice.csrf)
    .send({ kind: 'description', text: '  Easter picnic at Lake Erie  ' });
  assert.equal(s.status, 201);
  const row = (await pool.query(`select user_id, status, source, payload from suggestions where id = $1`, [s.body.id])).rows[0];
  assert.equal(Number(row.user_id), Number(w.alice.id));
  assert.equal(row.status, 'pending');
  assert.equal(row.source, 'human');
  assert.equal(row.payload.text, 'Easter picnic at Lake Erie');

  // The photo page shows it waiting.
  const page = await alice.get(`/photos/${id}`);
  assert.match(page.text, /suggested the description “Easter picnic at Lake Erie”/);

  // Too long is a plain 400, never stored.
  const long = await alice.post(`/api/photos/${id}/suggestions`).set('X-CSRF-Token', alice.csrf)
    .send({ kind: 'description', text: 'x'.repeat(2001) });
  assert.equal(long.status, 400);

  const admin = await agentFor(app, w.admin);
  const conflict = await admin.post(`/api/admin/suggestions/${s.body.id}/accept`).set('X-CSRF-Token', admin.csrf).send({});
  assert.equal(conflict.status, 409, 'the photo already has a description');
  assert.equal(conflict.body.current.description_ai, 'Easter picnic at the lake');
  assert.equal(conflict.body.proposed.description_ai, 'Easter picnic at Lake Erie');
  assert.equal(await desc(id), 'Easter picnic at the lake');

  const ok = await admin.post(`/api/admin/suggestions/${s.body.id}/accept`).set('X-CSRF-Token', admin.csrf).send({ force: true });
  assert.equal(ok.status, 200);
  assert.equal(await desc(id), 'Easter picnic at Lake Erie');
  const [a] = await pulled('photo.description.set');
  assert.equal(Number(a.entity_id), id);
  assert.equal(a.previous_value.description_ai, 'Easter picnic at the lake');
  assert.equal(a.new_value.description_ai, 'Easter picnic at Lake Erie');
});

test('admin direct edit: audited, pulled, no-op when unchanged, empty clears', async () => {
  const w = await seedWorld();
  const id = w.photos.clay1;
  const admin = await agentFor(app, w.admin);
  const r = await admin.patch(editUrl(id)).set('X-CSRF-Token', admin.csrf).send({ text: 'Easter picnic at the lake, 1962' });
  assert.equal(r.status, 200);
  assert.equal(r.body.changed, true);
  assert.equal(await desc(id), 'Easter picnic at the lake, 1962');

  const same = await admin.patch(editUrl(id)).set('X-CSRF-Token', admin.csrf).send({ text: ' Easter picnic at the lake, 1962 ' });
  assert.equal(same.body.changed, false);

  const cleared = await admin.patch(editUrl(id)).set('X-CSRF-Token', admin.csrf).send({ text: '' });
  assert.equal(cleared.status, 200);
  assert.equal(await desc(id), null);

  const rows = await pulled('photo.description.edit');
  assert.equal(rows.length, 2, 'one row per real change, none for the no-op');
  assert.equal(rows[0].previous_value.description_ai, 'Easter picnic at the lake');
  assert.equal(rows[0].new_value.description_ai, 'Easter picnic at the lake, 1962');
  assert.equal(rows[0].actor, w.admin.email);
  assert.equal(rows[1].new_value.description_ai, null);

  // The search index follows the fact.
  const hit = (await pool.query(
    `select count(*)::int as n from photo_search where photo_id = $1 and tsv @@ plainto_tsquery('english', 'picnic')`, [id],
  )).rows[0].n;
  assert.equal(hit, 0, 'the cleared text is no longer searchable');
});

test('direct edit: members refused, private and missing photos 404, overlong 400', async () => {
  const w = await seedWorld();
  const alice = await agentFor(app, w.alice);
  const c = await alice.patch(editUrl(w.photos.clay1)).set('X-CSRF-Token', alice.csrf).send({ text: 'mine now' });
  assert.equal(c.status, 403);
  assert.equal(await desc(w.photos.clay1), 'Easter picnic at the lake');

  const admin = await agentFor(app, w.admin);
  assert.equal((await admin.patch(editUrl(w.photos.priv)).set('X-CSRF-Token', admin.csrf).send({ text: 'x' })).status, 404);
  assert.equal((await admin.patch(editUrl(999999)).set('X-CSRF-Token', admin.csrf).send({ text: 'x' })).status, 404);
  assert.equal((await admin.patch(editUrl(w.photos.clay1)).set('X-CSRF-Token', admin.csrf).send({ text: 'x'.repeat(2001) })).status, 400);
  assert.equal((await admin.patch(editUrl(w.photos.clay1)).set('X-CSRF-Token', admin.csrf).send({})).status, 400);
});
