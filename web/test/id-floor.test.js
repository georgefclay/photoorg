// Phase 9 fix-up 1 — web-origin id range.
//
// Desktop-pushed rows keep their low ids; rows the web creates land at
// or above WEB_ID_FLOOR; the sync never lets the ranges cross. The test
// DB is migrated with PHOTOORG_DB_ROLE=web (tools/test-setup.js), and
// `truncate … restart identity` restarts each sequence at its START
// value — the floor.
const test = require('node:test');
const { after, before, beforeEach } = require('node:test');
const request = require('supertest');
const { pool, makeApp, insertUser, assert } = require('./helpers');
const { WEB_ID_FLOOR, checkIdFloor } = require('../services/id-floor');

let app;
before(() => { app = makeApp(); });
after(async () => { await pool.end(); });

beforeEach(async () => {
  await pool.query(`
    truncate table photo_groups, group_members, groups,
                   audit_log, magic_links, access_requests, "session",
                   contribution_files, contributions,
                   suggestions, likes, comments, faces, photo_backs,
                   album_photos, albums, photo_places, places, relationships,
                   person_name_variants, people,
                   photo_masters, photos, users
      restart identity cascade
  `);
});

const SVC = () => `Bearer ${process.env.SERVICE_TOKEN}`;

async function signIn(agent, userId) {
  const t = `t-${Math.random().toString(36).slice(2)}`;
  await pool.query(
    `insert into magic_links (user_id, token_hash, expires_at)
     values ($1, encode(sha256($2::bytea), 'hex'), now() + interval '15 minutes')`,
    [userId, t],
  );
  await agent.post(`/a/${t}`);
  return (await agent.get('/api/csrf')).body.csrfToken;
}

// What a desktop push leaves behind: photo 1 in a group, person 1, faces
// 1–2, AI suggestion 1 — all with the desktop's low ids.
async function seedDesktopPush() {
  const agent = request(app);
  let r = await agent.post('/sync/photos').set('Authorization', SVC()).send({
    photos: [{ id: 1, sha256: 'sha1', mime: 'image/jpeg', file_version: 1, width: 1000, height: 800,
      source_root: 'photos', source_folder: 'f', source_filename: 'a.jpg', triage_status: 'keep' }],
  });
  assert.equal(r.status, 200);
  r = await agent.post('/sync/people').set('Authorization', SVC())
    .send({ items: [{ id: 1, given_name: 'Peggy', surname: 'Clay' }] });
  assert.equal(r.status, 200);
  r = await agent.post('/sync/faces').set('Authorization', SVC()).send({ items: [
    { id: 1, photo_id: 1, person_id: 1, bbox: { x: 10, y: 10, w: 50, h: 50 }, source: 'ai' },
    { id: 2, photo_id: 1, person_id: null, bbox: { x: 200, y: 10, w: 50, h: 50 }, source: 'ai' },
  ] });
  assert.equal(r.status, 200);
  r = await agent.post('/sync/suggestions').set('Authorization', SVC()).send({ items: [
    { id: 1, photo_id: 1, kind: 'date', payload: { date: '1971-01-01', precision: 'year' },
      source: 'ai', status: 'pending', model: 'm' },
  ] });
  assert.equal(r.status, 200);
}

async function seedPeople() {
  const admin = await insertUser({ email: 'admin@example.com', role: 'admin' });
  const alice = await insertUser({ email: 'alice@example.com' });
  const g = Number((await pool.query(`insert into groups (name) values ('Clay') returning id`)).rows[0].id);
  await pool.query(`insert into group_members (group_id, user_id) values ($1, $2)`, [g, alice.id]);
  await pool.query(`insert into photo_groups (photo_id, group_id) values (1, $1)`, [g]);
  return { admin, alice };
}

test('every web-origin sequence starts at the floor (bigint, 1e12)', async () => {
  const r = await checkIdFloor(pool);
  assert.equal(r.floor, 1_000_000_000_000);
  assert.ok(r.ok, JSON.stringify(r.sequences));
  const types = (await pool.query(
    `select table_name, data_type from information_schema.columns
      where column_name = 'id' and table_schema = current_schema()
        and table_name = any($1::text[])`, [r.sequences.map((s) => s.table)],
  )).rows;
  for (const t of types) assert.equal(t.data_type, 'bigint', `${t.table_name}.id must be bigint`);
});

test('web inserts land at or above the floor next to desktop-pushed low ids', async () => {
  await seedDesktopPush();
  const { admin, alice } = await seedPeople();

  const aliceAgent = request.agent(app);
  const c1 = await signIn(aliceAgent, alice.id);
  const tag = await aliceAgent.post('/api/photos/1/faces').set('X-CSRF-Token', c1)
    .send({ bbox: { x: 400, y: 20, w: 60, h: 60 }, new_person: { given_name: 'Lola', surname: 'Clay' } });
  assert.equal(tag.status, 201, JSON.stringify(tag.body));
  assert.ok(tag.body.face_id >= WEB_ID_FLOOR, `face id ${tag.body.face_id}`);
  assert.ok(tag.body.suggestion_id >= WEB_ID_FLOOR, `suggestion id ${tag.body.suggestion_id}`);

  const date = await aliceAgent.post('/api/photos/1/suggestions').set('X-CSRF-Token', c1)
    .send({ kind: 'date', text: 'March 1962' });
  assert.equal(date.status, 201);
  assert.ok(date.body.id >= WEB_ID_FLOOR);

  const adminAgent = request.agent(app);
  const c2 = await signIn(adminAgent, admin.id);
  const acc = await adminAgent.post(`/api/admin/suggestions/${tag.body.suggestion_id}/accept`)
    .set('X-CSRF-Token', c2).send({});
  assert.equal(acc.status, 200, JSON.stringify(acc.body));
  const personId = acc.body.applied.person_id;
  assert.ok(personId >= WEB_ID_FLOOR, `person id ${personId}`);

  // Desktop rows untouched.
  const low = (await pool.query(`select id, person_id from faces where id < $1 order by id`, [WEB_ID_FLOOR])).rows;
  assert.deepEqual(low.map((f) => [Number(f.id), f.person_id && Number(f.person_id)]), [[1, 1], [2, null]]);
  const face = (await pool.query(`select person_id from faces where id = $1`, [tag.body.face_id])).rows[0];
  assert.equal(Number(face.person_id), personId);
});

test('a push carrying an id at or above the floor is refused and writes nothing', async () => {
  await seedDesktopPush();
  const agent = request(app);
  const bad = WEB_ID_FLOOR + 7;
  const cases = [
    ['faces', { id: bad, photo_id: 1, bbox: { x: 1, y: 1, w: 5, h: 5 }, source: 'ai' }],
    ['people', { id: bad, given_name: 'X' }],
    ['suggestions', { id: bad, photo_id: 1, kind: 'date', payload: {}, source: 'ai' }],
    ['places', { id: bad, name: 'Nowhere' }],
    ['albums', { id: bad, name: 'A', source: 'import' }],
    ['relationships', { id: bad, person_a_id: 1, person_b_id: 1, type: 'sibling' }],
    ['person_name_variants', { id: bad, person_id: 1, variant: 'Peg', kind: 'nickname' }],
  ];
  for (const [table, item] of cases) {
    // Mixed batch: a valid low id first — the whole batch must be refused.
    const ok = table === 'faces' ? { id: 3, photo_id: 1, bbox: { x: 1, y: 1, w: 5, h: 5 }, source: 'ai' } : null;
    const res = await agent.post(`/sync/${table}`).set('Authorization', SVC())
      .send({ items: ok ? [ok, item] : [item] });
    assert.equal(res.status, 400, `${table}: ${res.status} ${JSON.stringify(res.body)}`);
    assert.equal(res.body.web_id_floor, WEB_ID_FLOOR);
    assert.deepEqual(res.body.ids, [bad]);
    const n = (await pool.query(`select count(*)::int as n from ${table} where id >= $1`, [WEB_ID_FLOOR])).rows[0].n;
    assert.equal(n, 0, `${table}: nothing written`);
  }
  const face3 = (await pool.query(`select count(*)::int as n from faces where id = 3`)).rows[0].n;
  assert.equal(face3, 0, 'the valid item in a refused batch is not written either');
});

test('/sync/pull/web_origin returns only web-born rows', async () => {
  await seedDesktopPush();
  const { admin, alice } = await seedPeople();
  const aliceAgent = request.agent(app);
  const c1 = await signIn(aliceAgent, alice.id);
  const tag = await aliceAgent.post('/api/photos/1/faces').set('X-CSRF-Token', c1)
    .send({ bbox: { x: 400, y: 20, w: 60, h: 60 }, new_person: { given_name: 'Lola' } });
  const adminAgent = request.agent(app);
  const c2 = await signIn(adminAgent, admin.id);
  await adminAgent.post(`/api/admin/suggestions/${tag.body.suggestion_id}/accept`).set('X-CSRF-Token', c2).send({});

  const pull = await request(app).get('/sync/pull/web_origin').set('Authorization', SVC());
  assert.equal(pull.status, 200);
  assert.deepEqual(pull.body.faces.map((f) => Number(f.id)), [tag.body.face_id]);
  assert.equal(pull.body.people.length, 1);
  assert.equal(pull.body.people[0].given_name, 'Lola');
  assert.equal(Number(pull.body.faces[0].person_id), Number(pull.body.people[0].id));
  assert.ok(pull.body.cursor);

  const again = await request(app).get('/sync/pull/web_origin')
    .query({ since: pull.body.cursor }).set('Authorization', SVC());
  assert.equal(again.body.faces.length, 0, 'cursor advances');
});

test('a push of the desktop\'s pending copy never re-opens a suggestion accepted on the web', async () => {
  await seedDesktopPush();
  const { admin } = await seedPeople();
  const adminAgent = request.agent(app);
  const c2 = await signIn(adminAgent, admin.id);
  const acc = await adminAgent.post('/api/admin/suggestions/1/accept').set('X-CSRF-Token', c2).send({});
  assert.equal(acc.status, 200, JSON.stringify(acc.body));

  const r = await request(app).post('/sync/suggestions').set('Authorization', SVC()).send({ items: [
    { id: 1, photo_id: 1, kind: 'date', payload: { date: '1971-01-01', precision: 'year' },
      source: 'ai', status: 'pending', model: 'm2' },
  ] });
  assert.equal(r.status, 200);
  const s = (await pool.query(`select status, resolved_by, model from suggestions where id = 1`)).rows[0];
  assert.equal(s.status, 'accepted');
  assert.equal(Number(s.resolved_by), Number(admin.id));
  assert.equal(s.model, 'm2', 'other columns still update');
});

test('rescan_wanted set on the web is in /sync/pull/confirmed', async () => {
  await seedDesktopPush();
  const { admin } = await seedPeople();
  const adminAgent = request.agent(app);
  const c2 = await signIn(adminAgent, admin.id);
  const r = await adminAgent.post('/api/photos/1/rescan_wanted').set('X-CSRF-Token', c2).send({ wanted: true });
  assert.equal(r.status, 200);
  const pull = await request(app).get('/sync/pull/confirmed').set('Authorization', SVC());
  const row = pull.body.fact_audits.find((a) => a.action === 'photo.rescan_wanted');
  assert.ok(row, 'rescan audit row is pulled');
  assert.equal(Number(row.entity_id), 1);
  assert.equal(row.new_value.rescan_wanted, true);
});

test('desktop relationship / place duplicates of web-born rows are skipped, not 500', async () => {
  await seedDesktopPush();
  await request(app).post('/sync/people').set('Authorization', SVC())
    .send({ items: [{ id: 2, given_name: 'Chuck' }] });
  // Web-born copies (as an accept would make them).
  await pool.query(`insert into relationships (person_a_id, person_b_id, type, confirmed) values (1, 2, 'spouse', true)`);
  await pool.query(`insert into places (name) values ('Toronto')`);

  const rel = await request(app).post('/sync/relationships').set('Authorization', SVC())
    .send({ items: [{ id: 5, person_a_id: 1, person_b_id: 2, type: 'spouse', confirmed: false }] });
  assert.equal(rel.status, 200);
  assert.equal(rel.body.skipped, 1);
  const pl = await request(app).post('/sync/places').set('Authorization', SVC())
    .send({ items: [{ id: 5, name: 'toronto' }] });
  assert.equal(pl.status, 200);
  assert.equal(pl.body.skipped, 1);
  const rels = (await pool.query(`select id, confirmed from relationships`)).rows;
  assert.equal(rels.length, 1);
  assert.ok(Number(rels[0].id) >= WEB_ID_FLOOR);
  assert.equal(rels[0].confirmed, true);
});

test('/sync/status reports the id floor', async () => {
  const r = await request(app).get('/sync/status').set('Authorization', SVC());
  assert.equal(r.status, 200);
  assert.equal(r.body.id_floor.floor, WEB_ID_FLOOR);
  assert.equal(r.body.id_floor.ok, true);
  assert.equal(r.body.id_floor.sequences.length, 7);
});
