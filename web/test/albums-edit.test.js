// Phase 12 — albums editable on the web (admins), and how each edit goes
// home: a web-born album through /sync/pull/web_origin, an edit to a
// desktop-born album through /sync/pull/web_edits under the Phase 15
// human-edit rule (ties to the web).
const test = require('node:test');
const { after, before, beforeEach } = require('node:test');
const {
  pool, makeApp, assert, request, resetDb, seedWorld, agentFor, insertPhoto,
} = require('./page-helpers');
const { WEB_ID_FLOOR } = require('../services/id-floor');

let app;
before(() => { app = makeApp(); });
after(async () => { await pool.end(); });
beforeEach(resetDb);

const TOKEN = () => `Bearer ${process.env.SERVICE_TOKEN}`;
const push = (url, body) => request(app).post(url).set('Authorization', TOKEN()).send(body);
const pull = (url) => request(app).get(url).set('Authorization', TOKEN());
const one = async (sql, params) => (await pool.query(sql, params)).rows[0];
const actions = async (albumId) => (await pool.query(
  `select action from audit_log where entity_type = 'album' and entity_id = $1 order by id`, [albumId],
)).rows.map((r) => r.action);

test('an admin creates, fills, re-orders, renames and removes a web-born album', async () => {
  const w = await seedWorld();
  const a = await agentFor(app, w.admin);
  const p3 = await insertPhoto({});

  const made = await a.post('/albums').type('form').send({ _csrf: a.csrf, name: '  Reunion   2026 ', description: 'Lake' });
  assert.equal(made.status, 303);
  const id = Number(made.headers.location.split('/').pop());
  assert.ok(id >= WEB_ID_FLOOR, 'web-born id');
  assert.equal((await one('select name from albums where id = $1', [id])).name, 'Reunion 2026');

  for (const pid of [w.photos.clay1, w.photos.clay2, p3]) {
    const r = await a.post(`/photos/${pid}/albums`).type('form').send({ _csrf: a.csrf, album_id: id });
    assert.equal(r.status, 303);
  }
  const order = async () => (await pool.query(
    `select photo_id from album_photos where album_id = $1 and is_deleted = false order by position`, [id],
  )).rows.map((r) => Number(r.photo_id));
  assert.deepEqual(await order(), [w.photos.clay1, w.photos.clay2, p3]);

  await a.post(`/albums/${id}/photos/${p3}/move`).type('form').send({ _csrf: a.csrf, dir: 'up' });
  assert.deepEqual(await order(), [w.photos.clay1, p3, w.photos.clay2]);

  await a.post(`/albums/${id}/edit`).type('form').send({ _csrf: a.csrf, name: 'Reunion at the lake', description: '' });
  assert.deepEqual(await one('select name, description from albums where id = $1', [id]),
    { name: 'Reunion at the lake', description: null });

  await a.post(`/albums/${id}/photos/${w.photos.clay2}/remove`).type('form').send({ _csrf: a.csrf });
  assert.deepEqual(await order(), [w.photos.clay1, p3]);
  // Re-adding flips the flag back instead of failing on the key.
  await a.post(`/photos/${w.photos.clay2}/albums`).type('form').send({ _csrf: a.csrf, album_id: id });
  assert.deepEqual(await order(), [w.photos.clay1, p3, w.photos.clay2]);

  assert.deepEqual(await actions(id), [
    'album.create', 'album_photo.add', 'album_photo.add', 'album_photo.add', 'album.reorder',
    'album.update', 'album_photo.remove', 'album_photo.add',
  ]);
  const upd = await one(`select previous_value, new_value from audit_log where action = 'album.update' and entity_id = $1`, [id]);
  assert.deepEqual(upd.previous_value, { name: 'Reunion 2026', description: 'Lake' });

  // The edit page and the album page render; the album page offers Edit.
  assert.equal((await a.get(`/albums/${id}/edit`)).status, 200);
  assert.match((await a.get(`/albums/${id}`)).text, /Edit album/);

  // Removed, then restored.
  await a.post(`/albums/${id}/delete`).type('form').send({ _csrf: a.csrf });
  assert.equal((await one('select is_deleted from albums where id = $1', [id])).is_deleted, true);
  assert.match((await a.get('/albums')).text, /Removed albums \(1\)/);
  await a.post(`/albums/${id}/restore`).type('form').send({ _csrf: a.csrf });
  assert.equal((await one('select is_deleted from albums where id = $1', [id])).is_deleted, false);

  // It all comes down through web_origin with its web id.
  const wo = (await pull('/sync/pull/web_origin')).body;
  assert.deepEqual(wo.albums.map((x) => Number(x.id)), [id]);
  assert.deepEqual(wo.album_photos.filter((x) => !x.is_deleted).sort((x, y) => x.position - y.position)
    .map((x) => Number(x.photo_id)), [w.photos.clay1, p3, w.photos.clay2]);
  // ...and never through web_edits, which is for desktop-born rows.
  const we = (await pull('/sync/pull/web_edits')).body;
  assert.equal(we.albums.length, 0);
  assert.equal(we.album_photos.length, 0);
});

test('contributors stay read-only', async () => {
  const w = await seedWorld();
  const c = await agentFor(app, w.alice);
  assert.doesNotMatch((await c.get(`/albums/${w.album}`)).text, /Edit album/);
  assert.equal((await c.post('/albums').type('form').send({ _csrf: c.csrf, name: 'Mine' })).status, 403);
  assert.equal((await c.post(`/albums/${w.album}/photos/${w.photos.clay1}/remove`).type('form').send({ _csrf: c.csrf })).status, 403);
  assert.equal((await c.get(`/albums/${w.album}/edit`)).status, 403);
  assert.equal((await one('select count(*)::int as n from albums')).n, 1);
});

test('web edits to a desktop-born album come down as web_edits and survive a stale push', async () => {
  const w = await seedWorld();
  const a = await agentFor(app, w.admin);
  // Desktop-born album 77 (seedWorld): clay2 at 1, clay1 at 2.
  await a.post(`/albums/${w.album}/photos/${w.photos.clay1}/move`).type('form').send({ _csrf: a.csrf, dir: 'up' });
  await a.post(`/albums/${w.album}/photos/${w.photos.clay2}/remove`).type('form').send({ _csrf: a.csrf });
  await a.post(`/albums/${w.album}/edit`).type('form').send({ _csrf: a.csrf, name: 'Summer 1992 — Canada!' });

  const we = (await pull('/sync/pull/web_edits')).body;
  assert.equal(we.albums[0].name, 'Summer 1992 — Canada!');
  const rows = Object.fromEntries(we.album_photos.map((r) => [Number(r.photo_id), r]));
  assert.equal(rows[w.photos.clay1].position, 1);
  assert.equal(rows[w.photos.clay2].is_deleted, true);
  assert.match(we.cursors.album_photos, /^\d{4}-.+\|77\|\d+$/);
  // The cursor pages: asking again from it returns nothing new.
  const again = (await pull(`/sync/pull/web_edits?cursors=${encodeURIComponent(JSON.stringify(we.cursors))}`)).body;
  assert.equal(again.album_photos.length, 0);

  // The laptop's push still carries its old view (no human edit of its
  // own, a fresher updated_at because the pull re-wrote it): the web keeps
  // its edits.
  const fresh = new Date(Date.now() + 5000).toISOString();
  const r = await push('/sync/album_photos', { items: [
    { album_id: w.album, photo_id: w.photos.clay2, position: 1, is_deleted: false, updated_at: fresh },
    { album_id: w.album, photo_id: w.photos.clay1, position: 2, is_deleted: false, updated_at: fresh },
  ] });
  assert.equal(r.status, 200);
  assert.deepEqual(await one('select position, is_deleted from album_photos where album_id = $1 and photo_id = $2',
    [w.album, w.photos.clay2]), { position: 2, is_deleted: true }, 'the web re-ordered it to 2, then removed it');
  assert.equal((await one('select position from album_photos where album_id = $1 and photo_id = $2',
    [w.album, w.photos.clay1])).position, 1);
  await push('/sync/albums', { items: [{ id: w.album, name: 'Summer 1992 — Canada', source: 'import', is_deleted: false }] });
  assert.equal((await one('select name from albums where id = $1', [w.album])).name, 'Summer 1992 — Canada!');

  // A later human edit on the laptop wins.
  const later = new Date(Date.now() + 60000).toISOString();
  await push('/sync/album_photos', { items: [
    { album_id: w.album, photo_id: w.photos.clay2, position: 3, is_deleted: false, updated_at: later, edited_on_desktop_at: later },
  ] });
  assert.deepEqual(await one('select position, is_deleted from album_photos where album_id = $1 and photo_id = $2',
    [w.album, w.photos.clay2]), { position: 3, is_deleted: false });
});

test('a web removal of a desktop-born album is guarded like its name', async () => {
  const w = await seedWorld();
  const a = await agentFor(app, w.admin);
  await a.post(`/albums/${w.album}/delete`).type('form').send({ _csrf: a.csrf });
  await push('/sync/albums', { items: [{ id: w.album, name: 'Summer 1992 — Canada', source: 'import', is_deleted: false }] });
  assert.equal((await one('select is_deleted from albums where id = $1', [w.album])).is_deleted, true);
  const we = (await pull('/sync/pull/web_edits')).body;
  assert.equal(we.albums[0].is_deleted, true);
});

test('the desktop can never push into a web-born album', async () => {
  const w = await seedWorld();
  const r = await push('/sync/album_photos', { items: [
    { album_id: WEB_ID_FLOOR + 5, photo_id: w.photos.clay1, position: 1, is_deleted: false },
  ] });
  assert.equal(r.status, 400);
  assert.match(r.body.error, /web-origin/);
});
