// /upload, /upload/mine and /who-is-this (Phase 10 contrib pages).
const test = require('node:test');
const { after, before, beforeEach } = require('node:test');
const sharp = require('sharp');
const {
  pool, makeApp, assert, request, resetDb, seedWorld, agentFor, insertPhoto, addToGroup, insertFace,
} = require('./page-helpers');
const { faceBox } = require('../routes/pages-contrib');

let app;
before(() => { app = makeApp(); });
after(async () => { await pool.end(); });
beforeEach(resetDb);

const faceIds = (html) => [...html.matchAll(/data-face="(\d+)"/g)].map((m) => Number(m[1]));
const groupBoxes = (html) => [...html.matchAll(/name="group_ids" value="(\d+)"\s*(checked)?/g)]
  .map((m) => ({ id: Number(m[1]), checked: Boolean(m[2]) }));

let shaN = 0;
async function insertContribution(user, { groups = [], note = null, createdAt = null, files = [] } = {}) {
  const cid = Number((await pool.query(
    `insert into contributions (user_id, note, group_ids, created_at, finished_at)
     values ($1, $2, $3, coalesce($4::timestamptz, now()), now()) returning id`,
    [user.id, note, groups, createdAt],
  )).rows[0].id);
  const ids = [];
  for (const f of files) {
    shaN += 1;
    const sha = `c${String(shaN).padStart(63, '0')}`;
    ids.push(Number((await pool.query(
      `insert into contribution_files (contribution_id, original_filename, stored_path, sha256, size, mime, status, is_video)
       values ($1, $2, $3, $4, 1234, $5, $6, $7) returning id`,
      [cid, f.name || `f${shaN}.jpg`, `uploads/${cid}/${shaN}.jpg`, sha, f.video ? 'video/mp4' : 'image/jpeg',
        f.status || 'pending', Boolean(f.video)],
    )).rows[0].id));
  }
  return { cid, fileIds: ids };
}

test('anonymous visitors are sent to /login', async () => {
  for (const path of ['/upload', '/upload/mine', '/who-is-this', '/who-is-this?cursor=5']) {
    const r = await request(app).get(path);
    assert.equal(r.status, 302, path);
    assert.equal(r.headers.location, '/login', path);
  }
});

test('static assets for these pages are served', async () => {
  for (const p of ['/css/upload.css', '/js/upload.js', '/js/who.js']) {
    assert.equal((await request(app).get(p)).status, 200, p);
  }
});

test('/upload offers only the user\'s own groups; a single group is pre-selected', async () => {
  const w = await seedWorld();
  const alice = await agentFor(app, w.alice);
  const res = await alice.get('/upload');
  assert.equal(res.status, 200);
  assert.deepEqual(groupBoxes(res.text), [{ id: w.clay, checked: true }]);
  assert.match(res.text, /Clay Family/);
  assert.doesNotMatch(res.text, /Boots Family/);
  assert.match(res.text, /stay private until they(&#39;|')re approved/);
  assert.match(res.text, /<noscript>/);
  assert.match(res.text, /capture="environment"/);
  assert.match(res.text, /webkitdirectory/);
  assert.match(res.text, /\/js\/upload\.js/);

  const admin = await agentFor(app, w.admin);
  const ares = await admin.get('/upload');
  const boxes = groupBoxes(ares.text);
  assert.deepEqual(boxes.map((b) => b.id).sort(), [w.clay, w.boots].sort(), 'admins see all groups');
  assert.ok(boxes.every((b) => !b.checked), 'nothing pre-selected with two groups');

  const olive = await agentFor(app, w.olive);
  const ores = await olive.get('/upload');
  assert.equal(ores.status, 200, 'renders with no groups');
  assert.deepEqual(groupBoxes(ores.text), []);
  assert.match(ores.text, /not in a group yet/);
});

test('/upload/mine: summary counts, newest first, never another user\'s contributions', async () => {
  const w = await seedWorld();
  const older = await insertContribution(w.alice, {
    groups: [w.clay], note: 'Grandad fishing', createdAt: '2026-01-02T10:00:00Z',
    files: [{ status: 'pending' }, { status: 'pending' }, { status: 'approved' }, { status: 'rejected', name: 'blurry.jpg' }],
  });
  const newer = await insertContribution(w.alice, {
    groups: [], note: 'Easter 1971', createdAt: '2026-03-04T10:00:00Z',
    files: [{ status: 'pending', video: true, name: 'clip.mp4' }],
  });
  await insertContribution(w.alice, { note: 'empty batch — everything was skipped' });
  await insertContribution(w.carol, {
    groups: [w.boots], note: 'Carol private note', files: [{ status: 'pending', name: 'carol-secret.jpg' }],
  });

  const alice = await agentFor(app, w.alice);
  const res = await alice.get('/upload/mine');
  assert.equal(res.status, 200);
  assert.match(res.text, /You(&#39;|')ve sent 5 photos, 3 awaiting approval, 1 approved, 1 rejected/);
  assert.doesNotMatch(res.text, /Carol private note|carol-secret/);
  assert.doesNotMatch(res.text, /empty batch/, 'contributions with no files are hidden');
  const iNewer = res.text.indexOf('Easter 1971');
  const iOlder = res.text.indexOf('Grandad fishing');
  assert.ok(iNewer > 0 && iOlder > iNewer, 'newest first');
  assert.match(res.text, /Clay Family/);
  assert.match(res.text, /Admin will choose a group/);
  for (const id of older.fileIds) assert.match(res.text, new RegExp(`/media/contrib/${id}"`));
  assert.doesNotMatch(res.text, new RegExp(`/media/contrib/${newer.fileIds[0]}"`), 'videos get a placeholder, not an image');
  assert.match(res.text, /aria-label="Video: clip\.mp4"/);
  assert.match(res.text, /Awaiting approval/);
  assert.match(res.text, /Rejected/);

  const carol = await agentFor(app, w.carol);
  const cres = await carol.get('/upload/mine');
  assert.match(cres.text, /You(&#39;|')ve sent 1 photo, 1 awaiting approval, 0 approved/);
  assert.doesNotMatch(cres.text, /rejected/);
  assert.doesNotMatch(cres.text, /Grandad fishing/);

  const olive = await agentFor(app, w.olive);
  const ores = await olive.get('/upload/mine');
  assert.equal(ores.status, 200);
  assert.match(ores.text, /You haven(&#39;|')t sent any photos yet/);
  assert.match(ores.text, /href="\/upload"/);
});

test('/upload/mine reflects a real upload through the API', async () => {
  const w = await seedWorld();
  const alice = await agentFor(app, w.alice);
  const create = await alice.post('/api/contributions').set('X-CSRF-Token', alice.csrf)
    .send({ note: 'from the phone', group_ids: [w.clay] });
  assert.equal(create.status, 201);
  const jpg = await sharp({ create: { width: 16, height: 12, channels: 3, background: '#39a' } }).jpeg().toBuffer();
  const up = await alice.post(`/api/contributions/${create.body.id}/files`).set('X-CSRF-Token', alice.csrf)
    .attach('file', jpg, 'IMG_0001.jpg');
  assert.equal(up.status, 201);
  const head = await alice.head(`/api/contributions/${create.body.id}/files?sha256=${up.body.sha256}`);
  assert.equal(head.status, 204, 'pre-check reports the file as already on the server');
  const res = await alice.get('/upload/mine');
  assert.match(res.text, /You(&#39;|')ve sent 1 photo, 1 awaiting approval, 0 approved/);
  assert.match(res.text, /from the phone/);
  assert.match(res.text, new RegExp(`/media/contrib/${up.body.id}"`));
  const thumb = await alice.get(`/media/contrib/${up.body.id}`);
  assert.equal(thumb.status, 200);
});

test('/who-is-this shows each user only the unknown faces they can see', async () => {
  const w = await seedWorld();
  const alice = await agentFor(app, w.alice);
  const res = await alice.get('/who-is-this');
  assert.equal(res.status, 200);
  assert.deepEqual(faceIds(res.text), [w.faces.unknown]);
  assert.match(res.text, new RegExp(`/media/faces/${w.faces.unknown}"`));
  assert.match(res.text, new RegExp(`/media/thumbs/${w.photos.clay1}"`));
  assert.match(res.text, new RegExp(`href="/photos/${w.photos.clay1}\\?from=wi\\.recent"`));
  assert.match(res.text, /March 1962/);
  // bbox 400,120 140x140 on 1200x800
  assert.match(res.text, /left: 33\.33%; top: 15%; width: 11\.67%; height: 17\.5%/);
  assert.match(res.text, /data-identify/);
  assert.doesNotMatch(res.text, /data-wi-more/, 'no more link for a single page');

  const carol = await agentFor(app, w.carol);
  assert.deepEqual(faceIds((await carol.get('/who-is-this')).text), [w.faces.bootsUnknown]);

  const olive = await agentFor(app, w.olive);
  const ores = await olive.get('/who-is-this');
  assert.equal(ores.status, 200);
  assert.deepEqual(faceIds(ores.text), []);
  assert.match(ores.text, /Nobody to identify right now/);

  const admin = await agentFor(app, w.admin);
  assert.deepEqual(faceIds((await admin.get('/who-is-this')).text).sort(),
    [w.faces.unknown, w.faces.bootsUnknown].sort());
});

test('/who-is-this respects the header group scope', async () => {
  const w = await seedWorld();
  const admin = await agentFor(app, w.admin);
  let r = await admin.post('/scope').type('form').send({ _csrf: admin.csrf, scope: String(w.boots), return_to: '/who-is-this' });
  assert.equal(r.status, 303);
  assert.equal(r.headers.location, '/who-is-this');
  assert.deepEqual(faceIds((await admin.get('/who-is-this')).text), [w.faces.bootsUnknown]);

  await admin.post('/scope').type('form').send({ _csrf: admin.csrf, scope: 'unfiled', return_to: '/who-is-this' });
  const unfiled = await admin.get('/who-is-this');
  assert.deepEqual(faceIds(unfiled.text), []);
  assert.match(unfiled.text, /Nobody to identify right now/);
  assert.match(unfiled.text, /Every face in Unfiled/);

  const alice = await agentFor(app, w.alice);
  await alice.post('/scope').type('form').send({ _csrf: alice.csrf, scope: String(w.clay), return_to: '/who-is-this' });
  assert.deepEqual(faceIds((await alice.get('/who-is-this')).text), [w.faces.unknown]);
});

test('/who-is-this pages with the no-JS "More faces" cursor link', async () => {
  const w = await seedWorld();
  const extra = [];
  for (let i = 0; i < 25; i++) {
    const pid = await insertPhoto({ width: 800, height: 1200 });
    await addToGroup(pid, w.clay);
    extra.push(await insertFace(pid, { review: 'unknown' }));
  }
  const alice = await agentFor(app, w.alice);
  const page1 = await alice.get('/who-is-this');
  const ids1 = faceIds(page1.text);
  assert.equal(ids1.length, 24);
  const m = /data-wi-more href="([^"]+)"/.exec(page1.text);
  assert.ok(m, 'more link present');
  assert.match(m[1], /^\/who-is-this\?cursor=/);
  const page2 = await alice.get(m[1].replace(/&amp;/g, '&'));
  assert.equal(page2.status, 200);
  const ids2 = faceIds(page2.text);
  assert.equal(ids2.length, 2);
  assert.deepEqual([...ids1, ...ids2].sort((a, b) => a - b), [w.faces.unknown, ...extra].sort((a, b) => a - b));
  assert.match(page2.text, /Back to the start/);
  assert.doesNotMatch(page2.text, /data-wi-more/);

  const api = await alice.get('/api/faces/unknown').query({ limit: 24 });
  assert.deepEqual(api.body.items.map((f) => f.id), ids1, 'JSON feed matches the page');

  const bad = await alice.get('/who-is-this?cursor=not-a-cursor');
  assert.ok([200, 303].includes(bad.status), 'a mangled cursor never 500s');
});

test('a person suggestion for an unknown face shows "Thanks" on reload', async () => {
  const w = await seedWorld();
  const alice = await agentFor(app, w.alice);
  const before1 = await alice.get('/who-is-this');
  assert.doesNotMatch(before1.text, /data-wi-thanks/);

  const sug = await alice.post(`/api/photos/${w.photos.clay1}/suggestions`).set('X-CSRF-Token', alice.csrf)
    .send({ kind: 'person', face_id: w.faces.unknown, new_person: { given_name: 'Great Aunt', surname: 'Ada', display_name: 'Great Aunt Ada' } });
  assert.equal(sug.status, 201);

  const res = await alice.get('/who-is-this');
  assert.deepEqual(faceIds(res.text), [w.faces.unknown], 'still listed until an admin decides');
  assert.match(res.text, /data-wi-thanks/);
  assert.match(res.text, /Thanks — an admin will check it/);
  assert.doesNotMatch(res.text, /data-identify/);

  // Somebody else's suggestion doesn't mark it for bob.
  const bob = await agentFor(app, w.bob);
  const bres = await bob.get('/who-is-this');
  assert.match(bres.text, /data-identify/);
  assert.doesNotMatch(bres.text, /data-wi-thanks/);
});

test('faceBox clamps and rejects unusable boxes', () => {
  assert.deepEqual(faceBox({ bbox: { x: 0, y: 0, w: 100, h: 50 }, width: 200, height: 100 }),
    { left: 0, top: 0, width: 50, height: 50 });
  assert.deepEqual(faceBox({ bbox: { x: 150, y: 80, w: 100, h: 100 }, width: 200, height: 100 }),
    { left: 75, top: 80, width: 25, height: 20 });
  assert.equal(faceBox({ bbox: null, width: 200, height: 100 }), null);
  assert.equal(faceBox({ bbox: { x: 1, y: 1, w: 10, h: 10 }, width: null, height: 100 }), null);
  assert.equal(faceBox({ bbox: { x: 1, y: 1, w: 0, h: 10 }, width: 100, height: 100 }), null);
});
