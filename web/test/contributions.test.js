// Contributions API — end to end. Uses a real tiny JPEG so pHash + dedupe
// + storage all exercise. PHOTO_DIR is a tmp dir per suite.
const test = require('node:test');
const { after, before, beforeEach } = require('node:test');
const request = require('supertest');
const fs = require('fs');
const path = require('path');
const os = require('os');
const { pool, makeApp, insertUser, email, assert } = require('./helpers');

const PHOTO_DIR = fs.mkdtempSync(path.join(os.tmpdir(), 'photoarchive-contrib-'));
process.env.PHOTO_DIR = PHOTO_DIR;

let app;
before(() => { app = makeApp(); });
after(async () => {
  await pool.end();
  try { fs.rmSync(PHOTO_DIR, { recursive: true, force: true }); } catch {}
});

async function truncateAll() {
  await pool.query(`
    truncate table photo_groups, group_members, groups,
                   audit_log, magic_links, access_requests, "session",
                   contribution_files, contributions,
                   suggestions, likes, comments, faces, photo_backs,
                   album_photos, albums, photo_masters, photos, users
      restart identity cascade
  `);
  email.clearOutbox();
}
beforeEach(async () => { await truncateAll(); });

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

function tinyJpegBuf() {
  return Buffer.from(
    'ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434341f27393d38323c2e333432ffdb0043010909090c0b0c180d0d1832211c213232323232323232323232323232323232323232323232323232323232323232323232323232323232323232323232323232323232ffc0001108000100010301220002110103110101ffc4001f0000010501010101010100000000000000000102030405060708090a0bffc400b5100002010303020403050504040000017d01020300041105122131410613516107227114328191a1082342b1c11552d1f02433627282090a161718191a25262728292a3435363738393a434445464748494a535455565758595a636465666768696a737475767778797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffc4001f0100030101010101010101010000000000000102030405060708090a0bffc400b51100020102040403040705040400010277000102031104052131061241510761711322328108144291a1b1c109233352f0156272d10a162434e125f11718191a262728292a35363738393a434445464748494a535455565758595a636465666768696a737475767778797a82838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae2e3e4e5e6e7e8e9eaf2f3f4f5f6f7f8f9faffda000c03010002110311003f00fbfe28a28a03ffd9',
    'hex',
  );
}

test('POST /api/contributions creates a pending contribution + upload + finish', async () => {
  const alice = await insertUser({ email: 'alice@example.com' });
  const agent = request.agent(app);
  const csrf = await signIn(agent, alice.id);

  const create = await agent.post('/api/contributions').set('X-CSRF-Token', csrf).send({ note: 'test' });
  assert.equal(create.status, 201);
  const cid = create.body.id;

  // HEAD pre-check for a random sha → 404 (server does not have it).
  const head = await agent.head(`/api/contributions/${cid}/files?sha256=${'a'.repeat(64)}`);
  assert.equal(head.status, 404);

  // Upload a real tiny JPEG.
  const buf = tinyJpegBuf();
  const up = await agent.post(`/api/contributions/${cid}/files`)
    .set('X-CSRF-Token', csrf)
    .attach('file', buf, 'a.jpg');
  assert.equal(up.status, 201);
  assert.equal(up.body.mime, 'image/jpeg');
  assert.equal(up.body.duplicate, null);
  assert.ok(up.body.phash);
  assert.ok(up.body.size > 0);
  // File is on disk.
  const abs = path.join(PHOTO_DIR, 'uploads', String(cid), up.body.id + '.jpg');
  assert.ok(fs.existsSync(abs), 'file written to uploads dir');

  // HEAD pre-check for the sha we just uploaded → 204.
  const head2 = await agent.head(`/api/contributions/${cid}/files?sha256=${up.body.sha256}`);
  assert.equal(head2.status, 204);

  const finish = await agent.post(`/api/contributions/${cid}/finish`).set('X-CSRF-Token', csrf).send({});
  assert.equal(finish.status, 200);
  // Admin got an email.
  assert.ok(email.outbox.some((m) => m.to === process.env.ADMIN_EMAIL && /contribution/i.test(m.subject)));
});

test('sha256 dedupe: an existing photo\'s sha marks the upload as a duplicate', async () => {
  const alice = await insertUser({ email: 'alice@example.com' });
  const buf = tinyJpegBuf();
  // Compute the sha the same way the server will.
  const crypto = require('crypto');
  const sha = crypto.createHash('sha256').update(buf).digest('hex');
  // Seed a photo with this sha256.
  const p = Number((await pool.query(
    `insert into photos (sha256, mime, source_root, source_folder, source_filename, triage_status)
     values ($1, 'image/jpeg', 'p', 'f', 'a.jpg', 'keep') returning id`, [sha],
  )).rows[0].id);

  const agent = request.agent(app);
  const csrf = await signIn(agent, alice.id);
  const create = await agent.post('/api/contributions').set('X-CSRF-Token', csrf).send({});
  const cid = create.body.id;
  const up = await agent.post(`/api/contributions/${cid}/files`)
    .set('X-CSRF-Token', csrf)
    .attach('file', buf, 'a.jpg');
  assert.equal(up.status, 201);
  assert.deepEqual(up.body.duplicate.kind, 'sha256_exact');
  assert.equal(up.body.duplicate.photo_id, p);
  assert.equal(up.body.duplicate.distance, 0);
});

test('uploader isolation: user B does not see user A\'s pending contribution', async () => {
  const alice = await insertUser({ email: 'alice@example.com' });
  const bob   = await insertUser({ email: 'bob@example.com' });
  const aliceAgent = request.agent(app);
  const bobAgent   = request.agent(app);
  const csrfA = await signIn(aliceAgent, alice.id);
  await signIn(bobAgent, bob.id);
  const create = await aliceAgent.post('/api/contributions').set('X-CSRF-Token', csrfA).send({});
  const cid = create.body.id;
  await aliceAgent.post(`/api/contributions/${cid}/files`).set('X-CSRF-Token', csrfA).attach('file', tinyJpegBuf(), 'a.jpg');
  const bobMine = await bobAgent.get('/api/contributions/mine');
  assert.equal(bobMine.status, 200);
  assert.equal(bobMine.body.items.length, 0, 'bob does not see alice\'s contribution');
});

test('admin approves a file → pull sees it; rejected file stays on disk', async () => {
  const admin = await insertUser({ email: 'admin@example.com', role: 'admin' });
  const alice = await insertUser({ email: 'alice@example.com' });
  const aliceAgent = request.agent(app);
  const csrfA = await signIn(aliceAgent, alice.id);
  const create = await aliceAgent.post('/api/contributions').set('X-CSRF-Token', csrfA).send({});
  const cid = create.body.id;
  const up1 = await aliceAgent.post(`/api/contributions/${cid}/files`).set('X-CSRF-Token', csrfA)
    .attach('file', tinyJpegBuf(), 'a.jpg');
  const up2 = await aliceAgent.post(`/api/contributions/${cid}/files`).set('X-CSRF-Token', csrfA)
    .attach('file', Buffer.concat([tinyJpegBuf(), Buffer.from([0])]), 'b.jpg'); // different sha
  await aliceAgent.post(`/api/contributions/${cid}/finish`).set('X-CSRF-Token', csrfA).send({});

  const adminAgent = request.agent(app);
  const csrfAd = await signIn(adminAgent, admin.id);
  const app1 = await adminAgent.post(`/api/admin/contributions/${cid}/files/${up1.body.id}/approve`)
    .set('X-CSRF-Token', csrfAd).send({});
  assert.equal(app1.status, 200);
  const rej = await adminAgent.post(`/api/admin/contributions/${cid}/files/${up2.body.id}/reject`)
    .set('X-CSRF-Token', csrfAd).send({});
  assert.equal(rej.status, 200);

  // Contribution status rolls up to 'partial' or 'approved' (one approved, one rejected → partial).
  const cRow = (await pool.query(`select status from contributions where id = $1`, [cid])).rows[0];
  assert.ok(['partial', 'approved'].includes(cRow.status));

  // Sync pull sees the approved file only.
  const pull = await request(app).get('/sync/pull/contributions?status=approved&pulled=false')
    .set('Authorization', `Bearer ${process.env.SERVICE_TOKEN}`);
  assert.equal(pull.status, 200);
  const item = pull.body.items.find((i) => i.id === cid);
  assert.ok(item, 'contribution appears in pull');
  assert.equal(item.files.length, 1, 'only the approved file');

  // Rejected file's bytes are still on disk.
  const rejStored = (await pool.query(`select stored_path from contribution_files where id = $1`, [up2.body.id])).rows[0].stored_path;
  assert.ok(fs.existsSync(path.join(PHOTO_DIR, rejStored)), 'rejected file remains on disk');
});

test('/api/contributions/:id/files/:file_id streams file bytes via sync pull', async () => {
  const admin = await insertUser({ email: 'admin@example.com', role: 'admin' });
  const alice = await insertUser({ email: 'alice@example.com' });
  const aliceAgent = request.agent(app);
  const csrfA = await signIn(aliceAgent, alice.id);
  const create = await aliceAgent.post('/api/contributions').set('X-CSRF-Token', csrfA).send({});
  const cid = create.body.id;
  const up = await aliceAgent.post(`/api/contributions/${cid}/files`).set('X-CSRF-Token', csrfA)
    .attach('file', tinyJpegBuf(), 'a.jpg');
  await aliceAgent.post(`/api/contributions/${cid}/finish`).set('X-CSRF-Token', csrfA).send({});
  const adminAgent = request.agent(app);
  const csrfAd = await signIn(adminAgent, admin.id);
  await adminAgent.post(`/api/admin/contributions/${cid}/files/${up.body.id}/approve`)
    .set('X-CSRF-Token', csrfAd).send({});
  const fbytes = await request(app)
    .get(`/sync/pull/contributions/${cid}/files/${up.body.id}`)
    .set('Authorization', `Bearer ${process.env.SERVICE_TOKEN}`);
  assert.equal(fbytes.status, 200);
  assert.ok(fbytes.body.length > 0);
});
