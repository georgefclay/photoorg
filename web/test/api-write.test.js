// Contributor + admin write endpoints.
const test = require('node:test');
const { after, before, beforeEach } = require('node:test');
const request = require('supertest');
const { pool, makeApp, insertUser, assert } = require('./helpers');

let app;
before(() => { app = makeApp(); });
after(async () => { await pool.end(); });

async function truncateAllWithGroups() {
  await pool.query(`
    truncate table photo_groups, group_members, groups,
                   audit_log, magic_links, access_requests, "session",
                   contribution_files, contributions,
                   suggestions, likes, comments, faces, photo_backs,
                   photo_masters, photos, users
      restart identity cascade
  `);
}
beforeEach(async () => { await truncateAllWithGroups(); });

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

async function seedPhoto({ inGroup } = {}) {
  const { rows } = await pool.query(
    `insert into photos (sha256, mime, source_root, source_folder, source_filename, triage_status)
     values ($1, 'image/jpeg', 'photos', '_2005-06', $2, 'keep') returning id`,
    [`sha-${Math.random().toString(36).slice(2)}`, 'f.jpg'],
  );
  const id = Number(rows[0].id);
  if (inGroup) {
    await pool.query(`insert into photo_groups (photo_id, group_id) values ($1, $2)`, [id, inGroup]);
  }
  return id;
}

async function seedGroup(name, members) {
  const g = Number((await pool.query(`insert into groups (name) values ($1) returning id`, [name])).rows[0].id);
  for (const { userId, role = 'member' } of members || []) {
    await pool.query(
      `insert into group_members (group_id, user_id, role) values ($1, $2, $3)`,
      [g, userId, role],
    );
  }
  return g;
}

test('POST /api/photos/:id/suggestions parses date free text', async () => {
  const admin = await insertUser({ email: 'admin@example.com', role: 'admin' });
  const alice = await insertUser({ email: 'alice@example.com' });
  const g = await seedGroup('Clay', [{ userId: alice.id }]);
  const photoId = await seedPhoto({ inGroup: g });

  const agent = request.agent(app);
  const csrf = await signIn(agent, alice.id);
  const res = await agent
    .post(`/api/photos/${photoId}/suggestions`)
    .set('X-CSRF-Token', csrf)
    .send({ kind: 'date', text: 'March 1962' });
  assert.equal(res.status, 201);
  assert.equal(res.body.payload.precision, 'month');
  assert.equal(res.body.payload.date, '1962-03-01');
  // Rejects garbage.
  const bad = await agent
    .post(`/api/photos/${photoId}/suggestions`)
    .set('X-CSRF-Token', csrf)
    .send({ kind: 'date', text: 'yesterday afternoon' });
  assert.equal(bad.status, 400);
  assert.match(bad.body.error, /couldn't understand/);
});

test('POST /api/photos/:id/suggestions on hidden photo 404s', async () => {
  const alice = await insertUser({ email: 'alice@example.com' });
  const photoId = await seedPhoto();
  const agent = request.agent(app);
  const csrf = await signIn(agent, alice.id);
  const res = await agent
    .post(`/api/photos/${photoId}/suggestions`)
    .set('X-CSRF-Token', csrf)
    .send({ kind: 'date', text: '1962' });
  assert.equal(res.status, 404);
});

test('POST /api/photos/:id/faces creates unassigned face + person suggestion', async () => {
  const alice = await insertUser({ email: 'alice@example.com' });
  const g = await seedGroup('Clay', [{ userId: alice.id }]);
  const photoId = await seedPhoto({ inGroup: g });
  const person = (await pool.query(`insert into people (given_name) values ('Peggy') returning id`)).rows[0].id;

  const agent = request.agent(app);
  const csrf = await signIn(agent, alice.id);
  const res = await agent
    .post(`/api/photos/${photoId}/faces`)
    .set('X-CSRF-Token', csrf)
    .send({ bbox: { x: 10, y: 10, w: 100, h: 100 }, person_id: person });
  assert.equal(res.status, 201);
  const face = (await pool.query(`select id, person_id, source from faces where id = $1`, [res.body.face_id])).rows[0];
  assert.equal(face.person_id, null, 'face is unassigned until admin accepts');
  assert.equal(face.source, 'human');
  const sug = (await pool.query(`select kind, payload from suggestions where id = $1`, [res.body.suggestion_id])).rows[0];
  assert.equal(sug.kind, 'person');
  assert.equal(sug.payload.face_id, res.body.face_id);
});

test('admin accept of a date suggestion sets fact and audit + refresh_completeness', async () => {
  const admin = await insertUser({ email: 'admin@example.com', role: 'admin' });
  const alice = await insertUser({ email: 'alice@example.com' });
  const g = await seedGroup('Clay', [{ userId: alice.id }]);
  const photoId = await seedPhoto({ inGroup: g });
  const aliceAgent = request.agent(app);
  const csrf1 = await signIn(aliceAgent, alice.id);
  const sug = await aliceAgent
    .post(`/api/photos/${photoId}/suggestions`)
    .set('X-CSRF-Token', csrf1)
    .send({ kind: 'date', text: '1962' });
  const sugId = sug.body.id;

  const adminAgent = request.agent(app);
  const csrf2 = await signIn(adminAgent, admin.id);
  const accept = await adminAgent
    .post(`/api/admin/suggestions/${sugId}/accept`)
    .set('X-CSRF-Token', csrf2)
    .send({});
  assert.equal(accept.status, 200);
  const photo = (await pool.query(
    `select capture_date, capture_date_precision, capture_date_confirmed, completeness_score
       from photos where id = $1`, [photoId],
  )).rows[0];
  assert.equal(photo.capture_date_confirmed, true);
  assert.equal(photo.capture_date_precision, 'year');
  assert.equal(photo.capture_date.toISOString().slice(0, 4), '1962');
  assert.ok(photo.completeness_score >= 40, 'completeness rises');
  const dateSet = await pool.query(
    `select count(*)::int as n from audit_log where entity_type = 'photo' and entity_id = $1 and action = 'photo.capture_date.set'`,
    [photoId],
  );
  assert.equal(dateSet.rows[0].n, 1);
});

test('409 when accepting a date over a confirmed one; force=true wins with audit', async () => {
  const admin = await insertUser({ email: 'admin@example.com', role: 'admin' });
  const alice = await insertUser({ email: 'alice@example.com' });
  const g = await seedGroup('Clay', [{ userId: alice.id }]);
  const photoId = await seedPhoto({ inGroup: g });
  // Pre-set a confirmed date.
  await pool.query(
    `update photos set capture_date = '1950-01-01', capture_date_precision = 'year', capture_date_confirmed = true where id = $1`,
    [photoId],
  );
  const aliceAgent = request.agent(app);
  const csrf1 = await signIn(aliceAgent, alice.id);
  const sug = await aliceAgent
    .post(`/api/photos/${photoId}/suggestions`)
    .set('X-CSRF-Token', csrf1)
    .send({ kind: 'date', text: '1962' });
  const sugId = sug.body.id;

  const adminAgent = request.agent(app);
  const csrf2 = await signIn(adminAgent, admin.id);

  const conflict = await adminAgent
    .post(`/api/admin/suggestions/${sugId}/accept`)
    .set('X-CSRF-Token', csrf2)
    .send({});
  assert.equal(conflict.status, 409);
  assert.match(conflict.body.error, /confirmed/);

  const force = await adminAgent
    .post(`/api/admin/suggestions/${sugId}/accept`)
    .set('X-CSRF-Token', csrf2)
    .send({ force: true });
  assert.equal(force.status, 200);
  const photo = (await pool.query(
    `select capture_date from photos where id = $1`, [photoId],
  )).rows[0];
  assert.equal(photo.capture_date.toISOString().slice(0, 4), '1962');
});

test('POST /api/photos/:id/like toggles', async () => {
  const alice = await insertUser({ email: 'alice@example.com' });
  const g = await seedGroup('Clay', [{ userId: alice.id }]);
  const photoId = await seedPhoto({ inGroup: g });
  const agent = request.agent(app);
  const csrf = await signIn(agent, alice.id);
  const r1 = await agent.post(`/api/photos/${photoId}/like`).set('X-CSRF-Token', csrf).send({});
  assert.equal(r1.status, 200);
  assert.equal(r1.body.liked, true);
  assert.equal(r1.body.count, 1);
  const r2 = await agent.post(`/api/photos/${photoId}/like`).set('X-CSRF-Token', csrf).send({});
  assert.equal(r2.body.liked, false);
  assert.equal(r2.body.count, 0);
});

test('POST /api/comments/:id/hide by moderator of one of the photo\'s groups', async () => {
  const alice = await insertUser({ email: 'alice@example.com' });
  const mod   = await insertUser({ email: 'mod@example.com' });
  const g = await seedGroup('Clay', [{ userId: alice.id }, { userId: mod.id, role: 'moderator' }]);
  const photoId = await seedPhoto({ inGroup: g });
  const aliceAgent = request.agent(app);
  const csrf1 = await signIn(aliceAgent, alice.id);
  const cRes = await aliceAgent
    .post(`/api/photos/${photoId}/comments`)
    .set('X-CSRF-Token', csrf1)
    .send({ body: 'hello world' });
  assert.equal(cRes.status, 201);
  const cid = cRes.body.id;

  const modAgent = request.agent(app);
  const csrf2 = await signIn(modAgent, mod.id);
  const hid = await modAgent.post(`/api/comments/${cid}/hide`).set('X-CSRF-Token', csrf2).send({});
  assert.equal(hid.status, 200);
  const row = (await pool.query(`select is_hidden from comments where id = $1`, [cid])).rows[0];
  assert.equal(row.is_hidden, true);
});

test('non-moderator cannot hide a comment', async () => {
  const alice = await insertUser({ email: 'alice@example.com' });
  const bob   = await insertUser({ email: 'bob@example.com' });
  const g = await seedGroup('Clay', [{ userId: alice.id }, { userId: bob.id }]);
  const photoId = await seedPhoto({ inGroup: g });
  const aliceAgent = request.agent(app);
  const csrf1 = await signIn(aliceAgent, alice.id);
  const cRes = await aliceAgent
    .post(`/api/photos/${photoId}/comments`)
    .set('X-CSRF-Token', csrf1)
    .send({ body: 'hi' });
  const bobAgent = request.agent(app);
  const csrf2 = await signIn(bobAgent, bob.id);
  const hid = await bobAgent.post(`/api/comments/${cRes.body.id}/hide`).set('X-CSRF-Token', csrf2).send({});
  assert.equal(hid.status, 403);
});

test('monthly report counts logins, comments, suggestions from audit_log', async () => {
  const admin = await insertUser({ email: 'admin@example.com', role: 'admin' });
  const alice = await insertUser({ email: 'alice@example.com' });
  const g = await seedGroup('Clay', [{ userId: alice.id }]);
  const photoId = await seedPhoto({ inGroup: g });
  const aliceAgent = request.agent(app);
  const csrf1 = await signIn(aliceAgent, alice.id);
  await aliceAgent.post(`/api/photos/${photoId}/comments`).set('X-CSRF-Token', csrf1).send({ body: 'x' });
  await aliceAgent.post(`/api/photos/${photoId}/suggestions`).set('X-CSRF-Token', csrf1).send({ kind: 'date', text: '1962' });
  await aliceAgent.post(`/api/photos/${photoId}/like`).set('X-CSRF-Token', csrf1).send({});

  const adminAgent = request.agent(app);
  await signIn(adminAgent, admin.id);
  const month = new Date().toISOString().slice(0, 7);
  const rep = await adminAgent.get(`/api/admin/report/monthly?month=${month}`);
  assert.equal(rep.status, 200);
  const perUser = rep.body.per_user.find((u) => u.user_id === Number(alice.id));
  assert.ok(perUser, 'alice appears in report');
  assert.ok(perUser.comments >= 1);
  assert.ok(perUser.suggestions_made >= 1);
  assert.ok(perUser.likes_toggled >= 1);
  assert.ok(perUser.logins >= 1);
});
