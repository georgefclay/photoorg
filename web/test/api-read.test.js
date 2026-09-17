// /api/photos, /api/people, /api/albums — read paths respect visibility
// and produce the expected JSON shapes.
const test = require('node:test');
const { after, before, beforeEach } = require('node:test');
const request = require('supertest');
const {
  pool, makeApp, insertUser, assert,
} = require('./helpers');

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
  // Prime the CSRF token so JSON POSTs can carry X-CSRF-Token.
  const csrfRes = await agent.get('/api/csrf');
  return csrfRes.body.csrfToken;
}

async function seed() {
  const admin = await insertUser({ email: 'admin@example.com', role: 'admin' });
  const alice = await insertUser({ email: 'alice@example.com' });
  const bob   = await insertUser({ email: 'bob@example.com' });
  const clay = (await pool.query(`insert into groups (name) values ('Clay') returning id`)).rows[0].id;
  const boots = (await pool.query(`insert into groups (name) values ('Boots') returning id`)).rows[0].id;
  await pool.query(`insert into group_members (group_id, user_id) values ($1, $2)`, [clay, alice.id]);
  await pool.query(`insert into group_members (group_id, user_id) values ($1, $2)`, [boots, bob.id]);

  async function mkPhoto(sfx) {
    const { rows } = await pool.query(
      `insert into photos (sha256, mime, source_root, source_folder, source_filename, triage_status)
       values ($1, 'image/jpeg', 'photos', '_2005-06', $2, 'keep') returning id`,
      [`sha-${sfx}`, `f-${sfx}.jpg`],
    );
    return Number(rows[0].id);
  }
  const clayPhoto  = await mkPhoto('clay');
  const bootsPhoto = await mkPhoto('boots');
  const unfiled    = await mkPhoto('unfiled');
  const priv       = await mkPhoto('priv');
  await pool.query(`insert into photo_groups (photo_id, group_id) values ($1, $2)`, [clayPhoto, clay]);
  await pool.query(`insert into photo_groups (photo_id, group_id) values ($1, $2)`, [bootsPhoto, boots]);
  await pool.query(`update photos set is_private = true where id = $1`, [priv]);
  // Add priv to clay group to prove is_private overrides.
  await pool.query(`insert into photo_groups (photo_id, group_id) values ($1, $2)`, [priv, clay]);

  return { admin, alice, bob, clay, boots, clayPhoto, bootsPhoto, unfiled, priv };
}

test('GET /api/photos as admin returns all non-private non-deleted (including unfiled)', async () => {
  const { admin, clayPhoto, bootsPhoto, unfiled } = await seed();
  const agent = request.agent(app);
  await signIn(agent, admin.id);
  const res = await agent.get('/api/photos');
  assert.equal(res.status, 200);
  const ids = res.body.items.map((i) => i.id).sort((a, b) => a - b);
  assert.deepEqual(ids, [clayPhoto, bootsPhoto, unfiled].sort((a, b) => a - b),
    'admin includes unfiled, excludes private');
});

test('GET /api/photos as contributor only returns own-group photos', async () => {
  const { alice, clayPhoto } = await seed();
  const agent = request.agent(app);
  await signIn(agent, alice.id);
  const res = await agent.get('/api/photos');
  assert.equal(res.status, 200);
  assert.deepEqual(res.body.items.map((i) => i.id), [clayPhoto]);
});

test('GET /api/photos/:id 404s for non-members', async () => {
  const { alice, bootsPhoto } = await seed();
  const agent = request.agent(app);
  await signIn(agent, alice.id);
  const res = await agent.get(`/api/photos/${bootsPhoto}`);
  assert.equal(res.status, 404);
});

test('GET /api/photos/:id succeeds for a member and returns expected shape', async () => {
  const { alice, clayPhoto } = await seed();
  const agent = request.agent(app);
  await signIn(agent, alice.id);
  const res = await agent.get(`/api/photos/${clayPhoto}`);
  assert.equal(res.status, 200);
  assert.equal(res.body.id, clayPhoto);
  assert.equal(res.body.thumb_url, `/media/thumbs/${clayPhoto}`);
  assert.ok(Array.isArray(res.body.faces));
  assert.ok(Array.isArray(res.body.comments));
  assert.ok(Array.isArray(res.body.suggestions_pending));
  assert.ok(res.body.likes);
});

test('GET /api/albums lists albums with visibility-scoped counts', async () => {
  const { admin, alice, clay, clayPhoto, bootsPhoto } = await seed();
  await pool.query(`insert into albums (name, source) values ('Wedding', 'manual')`);
  const albumId = (await pool.query(`select id from albums where name = 'Wedding'`)).rows[0].id;
  await pool.query(`insert into album_photos (album_id, photo_id) values ($1, $2)`, [albumId, clayPhoto]);
  await pool.query(`insert into album_photos (album_id, photo_id) values ($1, $2)`, [albumId, bootsPhoto]);

  const adminAgent = request.agent(app); await signIn(adminAgent, admin.id);
  const aliceAgent = request.agent(app); await signIn(aliceAgent, alice.id);

  const adminRes = await adminAgent.get('/api/albums');
  const aliceRes = await aliceAgent.get('/api/albums');
  const findWedding = (b) => b.items.find((a) => a.name === 'Wedding');
  assert.equal(findWedding(adminRes.body).photo_count, 2, 'admin sees both');
  assert.equal(findWedding(aliceRes.body).photo_count, 1, 'alice sees only her group photo');
});

test('POST /api/people: admin creates a person with an audit row; contributors get 403', async () => {
  const { admin, alice } = await seed();
  const aliceAgent = request.agent(app);
  const aliceCsrf = await signIn(aliceAgent, alice.id);
  const denied = await aliceAgent.post('/api/people')
    .set('X-CSRF-Token', aliceCsrf)
    .send({ given_name: 'John', surname: 'Doe' });
  assert.equal(denied.status, 403, 'contributors name new people inside a person suggestion (answer A2)');
  const agent = request.agent(app);
  const csrf = await signIn(agent, admin.id);
  const res = await agent.post('/api/people')
    .set('X-CSRF-Token', csrf)
    .send({ given_name: 'John', surname: 'Doe' });
  assert.equal(res.status, 201);
  assert.match(res.body.display_name, /John Doe/);
  const audit = await pool.query(`select action, user_id from audit_log where entity_type = 'person' and entity_id = $1`, [res.body.id]);
  assert.equal(audit.rows[0].action, 'person.create');
  assert.equal(Number(audit.rows[0].user_id), Number(admin.id));
});

test('GET /api/people/autocomplete finds prefix and nickname variants', async () => {
  const { alice } = await seed();
  const p = (await pool.query(
    `insert into people (given_name, surname, nickname) values ('Margaret','Clay','Peggy') returning id`,
  )).rows[0].id;
  await pool.query(
    `insert into person_name_variants (person_id, variant, kind) values ($1,'Meg','nickname')`, [p],
  );
  const agent = request.agent(app);
  await signIn(agent, alice.id);
  const res1 = await agent.get('/api/people/autocomplete?q=Marg');
  assert.equal(res1.status, 200);
  assert.ok(res1.body.items.length >= 1, 'prefix hit');
  assert.match(res1.body.items[0].display_name, /Margaret/);
  const res2 = await agent.get('/api/people/autocomplete?q=Meg');
  assert.ok(res2.body.items.length >= 1, 'variant hit');
});
