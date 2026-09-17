// Browse (/), the group switcher, the shared layout, and the JSON the
// grid's infinite scroll uses.
const test = require('node:test');
const { after, before, beforeEach } = require('node:test');
const {
  pool, makeApp, assert, request, resetDb, seedWorld, agentFor, insertPhoto, addToGroup,
} = require('./page-helpers');

let app;
before(() => { app = makeApp(); });
after(async () => { await pool.end(); });
beforeEach(resetDb);

function tileIds(html) {
  return [...html.matchAll(/href="\/photos\/(\d+)\?from=/g)].map((m) => Number(m[1]));
}

test('anonymous / is the landing page; other pages redirect to /login', async () => {
  const res = await request(app).get('/');
  assert.equal(res.status, 200);
  assert.match(res.text, /Request access/);
  assert.doesNotMatch(res.text, /tab-bar/);
  for (const path of ['/people', '/albums', '/upload', '/who-is-this', '/search?q=x', '/photos/1', '/admin']) {
    const r = await request(app).get(path);
    assert.equal(r.status, 302, path);
    assert.equal(r.headers.location, '/login', path);
  }
});

test('browse renders with no photos and no groups (designed empty state)', async () => {
  const w = await seedWorld();
  const a = await agentFor(app, w.olive);
  const res = await a.get('/');
  assert.equal(res.status, 200);
  assert.match(res.text, /No photos yet/);
  assert.match(res.text, /All my groups/);
  assert.deepEqual(tileIds(res.text), []);
});

test('browse shows only visible photos; contributors never see unfiled or private', async () => {
  const w = await seedWorld();
  const alice = await agentFor(app, w.alice);
  const res = await alice.get('/');
  assert.equal(res.status, 200);
  assert.deepEqual(tileIds(res.text).sort(), [w.photos.clay1, w.photos.clay2].sort());
  assert.match(res.text, /Clay Family/, 'switcher lists her group');
  assert.doesNotMatch(res.text, /Boots Family/);
  assert.doesNotMatch(res.text, /value="unfiled"/);

  const admin = await agentFor(app, w.admin);
  const ares = await admin.get('/');
  assert.deepEqual(tileIds(ares.text).sort(),
    [w.photos.clay1, w.photos.clay2, w.photos.boots1, w.photos.unfiled].sort());
  assert.match(ares.text, /value="unfiled"/);
  assert.match(ares.text, /href="\/admin"/, 'admin sees the Admin nav');
  assert.doesNotMatch(res.text, /href="\/admin"/, 'contributor does not');
});

test('needs-attention strip counts and links (scoped)', async () => {
  const w = await seedWorld();
  const alice = await agentFor(app, w.alice);
  const res = await alice.get('/');
  assert.match(res.text, /1 photo with no date/);
  assert.match(res.text, /1 photo with untagged faces/);
  assert.match(res.text, /Who is this\? \(1\)/);
  const api = await alice.get('/api/attention');
  assert.deepEqual(
    { no_date: api.body.no_date, untagged_faces: api.body.untagged_faces, unknown_faces: api.body.unknown_faces },
    { no_date: 1, untagged_faces: 1, unknown_faces: 1 },
  );
  const nd = await alice.get('/?has_no_date=1&sort=liked');
  assert.deepEqual(tileIds(nd.text), [w.photos.clay2]);
  assert.match(nd.text, /from=nd\.liked/);
});

test('group switcher: POST /scope filters every grid and count; bad values fall back to all', async () => {
  const w = await seedWorld();
  const admin = await agentFor(app, w.admin);
  let r = await admin.post('/scope').type('form').send({ _csrf: admin.csrf, scope: String(w.boots), return_to: '/?cursor=5' });
  assert.equal(r.status, 303);
  assert.equal(r.headers.location, '/', 'continuation cursor dropped');
  let res = await admin.get('/');
  assert.deepEqual(tileIds(res.text), [w.photos.boots1]);
  assert.match(res.text, new RegExp(`value="${w.boots}" selected`));
  const counts = await admin.get('/api/attention');
  assert.equal(counts.body.unknown_faces, 1);

  await admin.post('/scope').type('form').send({ _csrf: admin.csrf, scope: 'unfiled', return_to: '/' });
  res = await admin.get('/');
  assert.deepEqual(tileIds(res.text), [w.photos.unfiled]);

  const alice = await agentFor(app, w.alice);
  await alice.post('/scope').type('form').send({ _csrf: alice.csrf, scope: String(w.boots), return_to: '//evil.example' });
  res = await alice.get('/');
  assert.deepEqual(tileIds(res.text).sort(), [w.photos.clay1, w.photos.clay2].sort(), 'not her group → all');
  r = await alice.post('/scope').type('form').send({ _csrf: alice.csrf, scope: 'unfiled', return_to: '//evil.example' });
  assert.equal(r.headers.location, '/', 'open redirect refused');
  const noCsrf = await alice.post('/scope').type('form').send({ scope: 'all' });
  assert.equal(noCsrf.status, 403);
});

test('sorts and composite cursors page through every photo exactly once', async () => {
  const w = await seedWorld();
  const g = w.clay;
  const extra = [];
  for (let i = 0; i < 9; i++) {
    const id = await insertPhoto({
      capture_date: i % 3 === 0 ? null : `19${50 + (i % 4)}-01-01`,
      capture_date_precision: i % 3 === 0 ? 'unknown' : 'year',
    });
    await addToGroup(id, g);
    extra.push(id);
  }
  // likes: ties on purpose
  await pool.query(`insert into likes (user_id, photo_id) values ($1, $2), ($3, $2), ($1, $4)`,
    [w.alice.id, extra[0], w.bob.id, extra[1]]);
  const alice = await agentFor(app, w.alice);
  const expected = [w.photos.clay1, w.photos.clay2, ...extra].sort((a, b) => a - b);
  for (const sort of ['recent', 'liked', 'incomplete', 'oldest', 'newest']) {
    const seen = [];
    let cursor = null;
    for (let guard = 0; guard < 20; guard++) {
      const r = await alice.get('/api/photos').query({ sort, limit: 3, ...(cursor ? { cursor } : {}) });
      assert.equal(r.status, 200, sort);
      seen.push(...r.body.items.map((p) => p.id));
      cursor = r.body.next;
      if (!cursor) break;
    }
    assert.deepEqual([...seen].sort((a, b) => a - b), expected, `${sort}: every photo once`);
    assert.equal(new Set(seen).size, seen.length, `${sort}: no duplicates`);
  }
  const liked = await alice.get('/api/photos').query({ sort: 'liked', limit: 2 });
  assert.equal(liked.body.items[0].id, extra[0]);
  const oldest = await alice.get('/api/photos').query({ sort: 'oldest', limit: 50 });
  const dates = oldest.body.items.map((p) => p.capture_date).filter(Boolean);
  assert.deepEqual(dates, [...dates].sort(), 'oldest ascending, nulls last');
  assert.equal(oldest.body.items.at(-1).capture_date, null);
});

test('no-JS "More photos" link carries the composite cursor', async () => {
  const w = await seedWorld();
  for (let i = 0; i < 61; i++) await addToGroup(await insertPhoto({}), w.clay);
  const alice = await agentFor(app, w.alice);
  const res = await alice.get('/?sort=liked');
  assert.equal(tileIds(res.text).length, 60);
  const m = /data-grid-more href="([^"]+)"/.exec(res.text);
  assert.ok(m, 'more link present');
  const page2 = await alice.get(m[1].replace(/&amp;/g, '&'));
  assert.equal(page2.status, 200);
  assert.equal(tileIds(page2.text).length, 3);
  assert.match(page2.text, /Back to the start/);
});

test('/api/photos/:id?from= returns prev/next inside the list; 404 for non-members', async () => {
  const w = await seedWorld();
  const alice = await agentFor(app, w.alice);
  const d = await alice.get(`/api/photos/${w.photos.clay1}`).query({ from: 'b.recent' });
  assert.equal(d.status, 200);
  assert.deepEqual(d.body.neighbours, { prev: w.photos.clay2, next: null, from: 'b.recent' });
  const carol = await agentFor(app, w.carol);
  assert.equal((await carol.get(`/api/photos/${w.photos.clay1}`)).status, 404);
  assert.equal((await alice.get(`/api/photos/${w.photos.priv}`)).status, 404);
});

test('static assets are served', async () => {
  for (const p of ['/css/site.css', '/js/site.js', '/js/grid.js']) {
    const r = await request(app).get(p);
    assert.equal(r.status, 200, p);
  }
});
