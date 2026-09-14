// Groups: CRUD, membership, moderator scope, bulk assign.
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
                   album_photos, albums, photo_masters, photos, users
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
async function mkPhoto(scanBatch = null) {
  const sfx = Math.random().toString(36).slice(2);
  const { rows } = await pool.query(
    `insert into photos (sha256, mime, source_root, source_folder, source_filename, scan_batch, triage_status)
     values ($1, 'image/jpeg', 'photos', '_2005-06', $2, $3, 'keep') returning id`,
    [`sha-${sfx}`, `f-${sfx}.jpg`, scanBatch],
  );
  return Number(rows[0].id);
}

test('admin CRUD: create group, rename, add member, delete', async () => {
  const admin = await insertUser({ email: 'admin@example.com', role: 'admin' });
  const alice = await insertUser({ email: 'alice@example.com' });
  const agent = request.agent(app);
  const csrf = await signIn(agent, admin.id);

  const create = await agent.post('/api/admin/groups').set('X-CSRF-Token', csrf)
    .send({ name: 'Clay Family', description: 'the Clays' });
  assert.equal(create.status, 201);
  const gid = create.body.id;

  const dup = await agent.post('/api/admin/groups').set('X-CSRF-Token', csrf)
    .send({ name: 'Clay Family' });
  assert.equal(dup.status, 409, 'unique name');

  const rename = await agent.patch(`/api/admin/groups/${gid}`).set('X-CSRF-Token', csrf)
    .send({ name: 'Clays' });
  assert.equal(rename.status, 200);

  const addMem = await agent.post(`/api/admin/groups/${gid}/members`).set('X-CSRF-Token', csrf)
    .send({ user_id: alice.id, role: 'member' });
  assert.equal(addMem.status, 200);
  const memCount = await pool.query(`select count(*)::int as n from group_members where group_id = $1 and is_deleted = false`, [gid]);
  assert.equal(memCount.rows[0].n, 1);

  const del = await agent.post(`/api/admin/groups/${gid}/delete`).set('X-CSRF-Token', csrf).send({});
  assert.equal(del.status, 200);
  const g = await pool.query(`select is_deleted from groups where id = $1`, [gid]);
  assert.equal(g.rows[0].is_deleted, true);
});

test('moderator can remove a photo from their group only', async () => {
  const admin = await insertUser({ email: 'admin@example.com', role: 'admin' });
  const alice = await insertUser({ email: 'alice@example.com' });
  const clay = Number((await pool.query(`insert into groups (name) values ('Clay') returning id`)).rows[0].id);
  const boots = Number((await pool.query(`insert into groups (name) values ('Boots') returning id`)).rows[0].id);
  await pool.query(`insert into group_members (group_id, user_id, role) values ($1, $2, 'moderator')`, [clay, alice.id]);
  const p = await mkPhoto();
  await pool.query(`insert into photo_groups (photo_id, group_id) values ($1, $2)`, [p, clay]);
  await pool.query(`insert into photo_groups (photo_id, group_id) values ($1, $2)`, [p, boots]);

  const agent = request.agent(app);
  const csrf = await signIn(agent, alice.id);
  const ok = await agent.post(`/api/groups/${clay}/photos/${p}/remove`).set('X-CSRF-Token', csrf).send({});
  assert.equal(ok.status, 200);
  assert.equal(ok.body.unfiled, false, 'still in boots');

  const denied = await agent.post(`/api/groups/${boots}/photos/${p}/remove`).set('X-CSRF-Token', csrf).send({});
  assert.equal(denied.status, 403, 'not a moderator of boots');
});

test('admin bulk-assign by scan_batch adds live photo_groups rows', async () => {
  const admin = await insertUser({ email: 'admin@example.com', role: 'admin' });
  const clay = Number((await pool.query(`insert into groups (name) values ('Clay') returning id`)).rows[0].id);
  const p1 = await mkPhoto('Batch 00012');
  const p2 = await mkPhoto('Batch 00012');
  const p3 = await mkPhoto('Batch 00099');

  const agent = request.agent(app);
  const csrf = await signIn(agent, admin.id);
  const res = await agent.post('/api/admin/photos/bulk-assign-groups').set('X-CSRF-Token', csrf)
    .send({ scan_batch: 'Batch 00012', add: [clay] });
  assert.equal(res.status, 200);
  assert.equal(res.body.photos, 2);
  assert.ok(res.body.added >= 2);

  const counts = await pool.query(
    `select photo_id from photo_groups where group_id = $1 and is_deleted = false order by photo_id`, [clay],
  );
  const ids = counts.rows.map((r) => Number(r.photo_id));
  assert.deepEqual(ids, [p1, p2]);
  assert.ok(!ids.includes(p3));
});

test('bulk-assign remove sets is_deleted; is_deleted rows do not count', async () => {
  const admin = await insertUser({ email: 'admin@example.com', role: 'admin' });
  const clay = Number((await pool.query(`insert into groups (name) values ('Clay') returning id`)).rows[0].id);
  const p = await mkPhoto('B');
  await pool.query(`insert into photo_groups (photo_id, group_id) values ($1, $2)`, [p, clay]);

  const agent = request.agent(app);
  const csrf = await signIn(agent, admin.id);
  const res = await agent.post('/api/admin/photos/bulk-assign-groups').set('X-CSRF-Token', csrf)
    .send({ ids: [p], remove: [clay] });
  assert.equal(res.status, 200);
  assert.equal(res.body.removed, 1);
  const row = (await pool.query(`select is_deleted from photo_groups where photo_id = $1 and group_id = $2`, [p, clay])).rows[0];
  assert.equal(row.is_deleted, true);
});
