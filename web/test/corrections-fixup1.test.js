// Phase 15 fix-up 1 — the last two real deletes.
//
// Phase 15 gave `album_photos` and `photo_places` soft-delete because a
// *removal* could not otherwise reach the VM: the push only upserts, so a
// hard-deleted row just stops being sent and the web keeps what it had.
// Two tables were left doing hard deletes and both had that same fault —
// a nickname or a place alias removed on the laptop went on showing here
// forever.
//
// One deliberate asymmetry is under test: `person_name_variants.is_deleted`
// is in `WEB_EDITABLE`, so `sync_web_edit_wins` settles a *removal* the
// same way it settles a rename. That is an exception to Phase 15's rule
// that a soft-delete is a decision and stays desktop-authoritative, and it
// is right here because the web's People editor can remove a variant too.
// `people.is_deleted` is not in that list and must stay desktop-only.
const test = require('node:test');
const { after, before, beforeEach } = require('node:test');
const {
  pool, makeApp, assert, request, resetDb, seedWorld, agentFor,
} = require('./page-helpers');

let app;
before(() => { app = makeApp(); });
after(async () => { await pool.end(); });
beforeEach(resetDb);

const TOKEN = () => `Bearer ${process.env.SERVICE_TOKEN}`;
function push(url, body) {
  return request(app).post(url).set('Authorization', TOKEN()).send(body);
}
function pull(url) {
  return request(app).get(url).set('Authorization', TOKEN());
}
async function one(sql, params) {
  const { rows } = await pool.query(sql, params);
  return rows[0];
}
const later = () => new Date(Date.now() + 60_000).toISOString();

// ---------------------------------------------------------------------
// person_name_variants
// ---------------------------------------------------------------------

test('a desktop variant removal reaches the web and the name stops matching', async () => {
  const w = await seedWorld();
  const a = await agentFor(app, w.admin);
  await push('/sync/people', { items: [{ id: 320, given_name: 'Margaret', surname: 'Clay' }] });
  await push('/sync/person_name_variants', {
    items: [{ id: 330, person_id: 320, variant: 'Pegleg', kind: 'nickname' }],
  });

  assert.ok((await a.get('/people/320')).text.includes('Pegleg'));
  assert.ok(JSON.stringify((await a.get('/api/search?q=Pegleg')).body).includes('Margaret'),
    'the nickname should find the person while it is live');

  // George removes it on the laptop; the push carries the flag.
  const res = await push('/sync/person_name_variants', {
    items: [{
      id: 330, person_id: 320, variant: 'Pegleg', kind: 'nickname',
      is_deleted: true, deleted_at: new Date().toISOString(),
      edited_on_desktop_at: new Date().toISOString(),
    }],
  });
  assert.equal(res.status, 200);
  const row = await one('select is_deleted, variant from person_name_variants where id = 330');
  assert.equal(row.is_deleted, true);
  assert.equal(row.variant, 'Pegleg', 'the row survives - no real deletes, ever');

  assert.ok(!(await a.get('/people/320')).text.includes('Pegleg'),
    'a removed nickname must leave the person page');
  assert.ok(!JSON.stringify((await a.get('/api/search?q=Pegleg')).body).includes('Margaret'),
    'and must stop finding the person');
});

test('an admin can remove a variant on the web, and the next push does not resurrect it', async () => {
  const w = await seedWorld();
  const a = await agentFor(app, w.admin);
  await push('/sync/people', { items: [{ id: 321, given_name: 'Margaret', surname: 'Clay' }] });
  await push('/sync/person_name_variants', {
    items: [{ id: 331, person_id: 321, variant: 'Peg', kind: 'nickname' }],
  });

  const del = await a.delete('/api/people/321/variants/331').set('X-CSRF-Token', a.csrf);
  assert.equal(del.status, 200);
  let row = await one(
    'select is_deleted, deleted_by, edited_on_web_at from person_name_variants where id = 331');
  assert.equal(row.is_deleted, true);
  assert.equal(Number(row.deleted_by), Number(w.admin.id));
  assert.ok(row.edited_on_web_at, 'stamped, or the next push would undo it');

  const audit = await one(
    `select previous_value, new_value from audit_log
      where action = 'person.variant_remove' and entity_id = 321`);
  assert.equal(audit.previous_value.variant, 'Peg');
  assert.equal(audit.new_value.is_deleted, true);

  // The laptop re-sends its still-live copy. The web's removal must hold:
  // this is why `is_deleted` is in WEB_EDITABLE for this one table.
  await push('/sync/person_name_variants', {
    items: [{ id: 331, person_id: 321, variant: 'Peg', kind: 'nickname', is_deleted: false }],
  });
  row = await one('select is_deleted from person_name_variants where id = 331');
  assert.equal(row.is_deleted, true, 'the web removal must survive the push');

  // A later desktop edit still wins, so the laptop is not locked out.
  await push('/sync/person_name_variants', {
    items: [{
      id: 331, person_id: 321, variant: 'Peg', kind: 'nickname', is_deleted: false,
      edited_on_desktop_at: later(),
    }],
  });
  row = await one('select is_deleted from person_name_variants where id = 331');
  assert.equal(row.is_deleted, false);
});

test('a person merge still re-parents a variant the web removed', async () => {
  // `person_id` is NOT guarded: a merge is a desktop decision about who
  // owns the name. The Phase 9 fix-up 2 bug must not come back through
  // the new flag.
  const w = await seedWorld();
  const a = await agentFor(app, w.admin);
  await push('/sync/people', {
    items: [{ id: 340, given_name: 'Margaret', surname: 'Clay' },
            { id: 341, given_name: 'Peggy', surname: 'Clay' }],
  });
  await push('/sync/person_name_variants', {
    items: [{ id: 350, person_id: 341, variant: 'Peg', kind: 'nickname' }],
  });
  await a.delete('/api/people/341/variants/350').set('X-CSRF-Token', a.csrf);

  await push('/sync/person_name_variants', {
    items: [{ id: 350, person_id: 340, variant: 'Peg', kind: 'nickname', is_deleted: false }],
  });
  const row = await one('select person_id, is_deleted from person_name_variants where id = 350');
  assert.equal(Number(row.person_id), 340, 'the merge must still re-parent it');
  assert.equal(row.is_deleted, true, 'but the web removal still holds');
});

test('re-adding a removed variant on the web brings the same row back', async () => {
  const w = await seedWorld();
  const a = await agentFor(app, w.admin);
  const id = w.people.peggy.id;
  const created = await a.post(`/api/people/${id}/variants`)
    .set('X-CSRF-Token', a.csrf).send({ variant: 'Maggie' });
  assert.equal(created.status, 201);
  const variantId = created.body.id;

  await a.delete(`/api/people/${id}/variants/${variantId}`).set('X-CSRF-Token', a.csrf);
  // A plain insert would 409 on the key here: the row it collides with is
  // the one that was just removed.
  const again = await a.post(`/api/people/${id}/variants`)
    .set('X-CSRF-Token', a.csrf).send({ variant: 'maggie' });
  assert.equal(again.status, 201);
  assert.equal(again.body.id, variantId, 'same row, so the laptop stays in step');
  assert.equal(again.body.restored, true);
  assert.equal((await one(
    'select count(*)::int as n from person_name_variants where person_id = $1', [id])).n, 1);
  // Still 409 when it is already live.
  assert.equal((await a.post(`/api/people/${id}/variants`)
    .set('X-CSRF-Token', a.csrf).send({ variant: 'Maggie' })).status, 409);
});

test('removing or renaming a variant is admin-only, and a removed one 404s', async () => {
  const w = await seedWorld();
  const admin = await agentFor(app, w.admin);
  const created = await admin.post(`/api/people/${w.people.peggy.id}/variants`)
    .set('X-CSRF-Token', admin.csrf).send({ variant: 'Maggie' });
  const vid = created.body.id;

  // A group moderator is not an admin: rewriting or removing somebody's
  // name is an admin act, exactly as with comment bodies.
  const bob = await agentFor(app, w.bob);
  assert.equal((await bob.delete(`/api/people/${w.people.peggy.id}/variants/${vid}`)
    .set('X-CSRF-Token', bob.csrf)).status, 403);
  const alice = await agentFor(app, w.alice);
  assert.equal((await alice.delete(`/api/people/${w.people.peggy.id}/variants/${vid}`)
    .set('X-CSRF-Token', alice.csrf)).status, 403);

  await admin.delete(`/api/people/${w.people.peggy.id}/variants/${vid}`)
    .set('X-CSRF-Token', admin.csrf);
  // Already removed: nothing live to act on.
  assert.equal((await admin.delete(`/api/people/${w.people.peggy.id}/variants/${vid}`)
    .set('X-CSRF-Token', admin.csrf)).status, 404);
  assert.equal((await admin.patch(`/api/people/${w.people.peggy.id}/variants/${vid}`)
    .set('X-CSRF-Token', admin.csrf).send({ variant: 'Mags' })).status, 404);
});

test('pull/web_edits offers a web variant removal to the laptop', async () => {
  const w = await seedWorld();
  const a = await agentFor(app, w.admin);
  await push('/sync/people', { items: [{ id: 322, given_name: 'Ann', surname: 'B' }] });
  await push('/sync/person_name_variants', {
    items: [{ id: 332, person_id: 322, variant: 'Annie', kind: 'nickname' }],
  });
  await a.delete('/api/people/322/variants/332').set('X-CSRF-Token', a.csrf);

  const pulled = await pull('/sync/pull/web_edits');
  assert.equal(pulled.status, 200);
  const v = pulled.body.person_name_variants.find((x) => Number(x.id) === 332);
  assert.ok(v, 'the removal has to be offered, or the laptop never learns');
  assert.equal(v.is_deleted, true);
  assert.ok(v.deleted_at, 'the timestamp travels with the flag it belongs to');
  assert.ok(v.edited_on_web_at);
});

// ---------------------------------------------------------------------
// place_aliases
// ---------------------------------------------------------------------

test('place aliases are flagged, not deleted, and come back', async () => {
  await seedWorld();
  await push('/sync/places', { items: [{ id: 410, name: 'Banff' }] });
  await push('/sync/place_aliases', {
    items: [{ place_id: 410, aliases: [{ alias: 'Banff Springs' }, { alias: 'Banff AB' }] }],
  });

  // The laptop removes one: it is simply absent from the live set it sends.
  let res = await push('/sync/place_aliases', {
    items: [{ place_id: 410, aliases: [{ alias: 'Banff Springs' }] }],
  });
  assert.equal(res.status, 200);
  assert.equal(res.body.removed, 1);
  let { rows } = await pool.query(
    'select alias, is_deleted from place_aliases where place_id = 410 order by alias');
  assert.equal(rows.length, 2, 'the row must survive - no real deletes, ever');
  assert.deepEqual(rows.map((r) => [r.alias, r.is_deleted]),
    [['Banff AB', true], ['Banff Springs', false]]);

  // And it comes back when the laptop sends it again.
  res = await push('/sync/place_aliases', {
    items: [{ place_id: 410, aliases: [{ alias: 'Banff Springs' }, { alias: 'Banff AB' }] }],
  });
  assert.equal(res.status, 200);
  ({ rows } = await pool.query(
    'select alias, is_deleted from place_aliases where place_id = 410 order by alias'));
  assert.equal(rows.length, 2, 'still two rows, never four');
  assert.deepEqual(rows.map((r) => r.is_deleted), [false, false]);

  // An empty set flags them all — the state the push sends alias-less
  // places in order to be able to express.
  await push('/sync/place_aliases', { items: [{ place_id: 410, aliases: [] }] });
  ({ rows } = await pool.query('select is_deleted from place_aliases where place_id = 410'));
  assert.deepEqual(rows.map((r) => r.is_deleted), [true, true]);
});

test('an alias rename that only changes case does not violate the unique', async () => {
  // The unique is on lower(alias), so a row whose `alias` no longer
  // matches but whose `lower(alias)` still does would make a plain
  // `on conflict (place_id, alias)` upsert insert, and then blow up.
  await seedWorld();
  await push('/sync/places', { items: [{ id: 411, name: 'Banff' }] });
  await push('/sync/place_aliases', {
    items: [{ place_id: 411, aliases: [{ alias: 'Banff West' }] }],
  });
  const res = await push('/sync/place_aliases', {
    items: [{ place_id: 411, aliases: [{ alias: 'banff west' }] }],
  });
  assert.equal(res.status, 200);
  const { rows } = await pool.query('select alias from place_aliases where place_id = 411');
  assert.deepEqual(rows.map((r) => r.alias), ['banff west']);
});

test('a removed alias stops matching search', async () => {
  const w = await seedWorld();
  const a = await agentFor(app, w.admin);
  await pool.query(
    `insert into place_aliases (place_id, alias, kind) values ($1, 'Hogtown', 'alias')`,
    [w.place]);

  let hits = await a.get('/api/search?q=Hogtown');
  assert.equal(hits.status, 200);
  assert.ok(JSON.stringify(hits.body).includes('Toronto'),
    'the alias should find the place while it is live');

  await pool.query(
    `update place_aliases set is_deleted = true, deleted_at = now()
      where place_id = $1 and alias = 'Hogtown'`, [w.place]);

  hits = await a.get('/api/search?q=Hogtown');
  assert.equal(hits.status, 200);
  assert.ok(!JSON.stringify(hits.body).includes('Toronto'),
    'a removed alias must stop finding the place');
});

// ---------------------------------------------------------------------
// Item 4 — re-adding a join row flips the flag back
// ---------------------------------------------------------------------

test('re-adding a photo to an album or place through sync flips the flag back', async () => {
  const w = await seedWorld();
  const a = await agentFor(app, w.admin);

  await push('/sync/album_photos', {
    items: [{ album_id: w.album, photo_id: w.photos.clay1, position: 2,
              is_deleted: true, deleted_at: new Date().toISOString(),
              updated_at: new Date().toISOString() }],
  });
  await push('/sync/photo_places', {
    items: [{ photo_id: w.photos.clay1, place_id: w.place, confirmed: true,
              is_deleted: true, deleted_at: new Date().toISOString(),
              updated_at: new Date().toISOString() }],
  });
  assert.ok(!(await a.get(`/albums/${w.album}`)).text.includes(`/photos/${w.photos.clay1}`));

  // Put them back. This must flip the flag, not fail on the composite key
  // and not create a second row.
  let res = await push('/sync/album_photos', {
    items: [{ album_id: w.album, photo_id: w.photos.clay1, position: 2,
              is_deleted: false, updated_at: later() }],
  });
  assert.equal(res.status, 200);
  res = await push('/sync/photo_places', {
    items: [{ photo_id: w.photos.clay1, place_id: w.place, confirmed: true,
              is_deleted: false, updated_at: later() }],
  });
  assert.equal(res.status, 200);

  assert.equal((await one(
    'select count(*)::int as n from album_photos where album_id = $1 and photo_id = $2',
    [w.album, w.photos.clay1])).n, 1);
  assert.equal((await one(
    'select is_deleted from album_photos where album_id = $1 and photo_id = $2',
    [w.album, w.photos.clay1])).is_deleted, false);
  assert.equal((await one(
    'select is_deleted from photo_places where photo_id = $1 and place_id = $2',
    [w.photos.clay1, w.place])).is_deleted, false);

  // And they are back on the pages.
  assert.ok((await a.get(`/albums/${w.album}`)).text.includes(`/photos/${w.photos.clay1}`));
  assert.ok((await a.get(`/photos/${w.photos.clay1}`)).text.includes('Toronto'));
});
