// Sync endpoints — service-token authed. Tests the critical invariants
// from the phase prompt: idempotence, 400 on private, file-version
// handshake, pull-since cursors.
const test = require('node:test');
const { after, before, beforeEach } = require('node:test');
const request = require('supertest');
const fs = require('fs');
const path = require('path');
const os = require('os');
const { pool, makeApp, assert } = require('./helpers');

// Force PHOTO_DIR to a tmp dir before app builds.
const PHOTO_DIR = fs.mkdtempSync(path.join(os.tmpdir(), 'photoarchive-test-'));
process.env.PHOTO_DIR = PHOTO_DIR;

let app;
before(() => { app = makeApp(); });
after(async () => {
  await pool.end();
  try { fs.rmSync(PHOTO_DIR, { recursive: true, force: true }); } catch {}
});

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

function bearer(agent) {
  return `Bearer ${process.env.SERVICE_TOKEN}`;
}

function tinyJpeg() {
  // Minimal JPEG (SOI + EOI won't decode, but sharp allows failOn:'none').
  // For thumb generation we send a real 1x1 pixel JPEG.
  return Buffer.from(
    'ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434341f27393d38323c2e333432ffdb0043010909090c0b0c180d0d1832211c213232323232323232323232323232323232323232323232323232323232323232323232323232323232323232323232323232323232ffc0001108000100010301220002110103110101ffc4001f0000010501010101010100000000000000000102030405060708090a0bffc400b5100002010303020403050504040000017d01020300041105122131410613516107227114328191a1082342b1c11552d1f02433627282090a161718191a25262728292a3435363738393a434445464748494a535455565758595a636465666768696a737475767778797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffc4001f0100030101010101010101010000000000000102030405060708090a0bffc400b51100020102040403040705040400010277000102031104052131061241510761711322328108144291a1b1c109233352f0156272d10a162434e125f11718191a262728292a35363738393a434445464748494a535455565758595a636465666768696a737475767778797a82838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae2e3e4e5e6e7e8e9eaf2f3f4f5f6f7f8f9faffda000c03010002110311003f00fbfe28a28a03ffd9',
    'hex',
  );
}

test('POST /sync/photos requires bearer token and rejects wrong tokens', async () => {
  const none = await request(app).post('/sync/photos').send({ photos: [] });
  assert.equal(none.status, 401);
  const wrong = await request(app).post('/sync/photos').set('Authorization', 'Bearer nope').send({ photos: [] });
  assert.equal(wrong.status, 401);
});

test('POST /sync/photos upserts and reports need_files', async () => {
  const agent = request(app);
  const res = await agent
    .post('/sync/photos')
    .set('Authorization', bearer())
    .send({
      photos: [
        {
          id: 1, sha256: 'sha1', mime: 'image/jpeg', file_version: 1,
          source_root: 'photos', source_folder: '_2005-06', source_filename: 'a.jpg',
        },
      ],
    });
  assert.equal(res.status, 200);
  assert.equal(res.body.upserted, 1);
  assert.deepEqual(res.body.need_files, [1]);
  // Second call, no change → still need file (never uploaded).
  const res2 = await agent
    .post('/sync/photos')
    .set('Authorization', bearer())
    .send({
      photos: [
        {
          id: 1, sha256: 'sha1', mime: 'image/jpeg', file_version: 1,
          source_root: 'photos', source_folder: '_2005-06', source_filename: 'a.jpg',
        },
      ],
    });
  assert.deepEqual(res2.body.need_files, [1], 'idempotent, still needs file');
});

test('POST /sync/photos rejects any is_private=true with 400', async () => {
  const res = await request(app)
    .post('/sync/photos')
    .set('Authorization', bearer())
    .send({
      photos: [
        {
          id: 1, sha256: 'x', mime: 'image/jpeg', file_version: 1,
          source_root: 'p', source_folder: 'f', source_filename: 'x.jpg',
          is_private: true,
        },
      ],
    });
  assert.equal(res.status, 400);
  assert.match(res.body.error, /private/);
});

test('PUT /sync/photos/:id/file writes file, generates thumb, records synced_file_version', async () => {
  const agent = request(app);
  await agent.post('/sync/photos').set('Authorization', bearer()).send({
    photos: [{
      id: 1, sha256: 'aabbccddeeff', mime: 'image/jpeg', file_version: 1,
      source_root: 'photos', source_folder: '_2005-06', source_filename: 'a.jpg',
    }],
  });
  const buf = tinyJpeg();
  const put = await agent.put('/sync/photos/1/file')
    .set('Authorization', bearer())
    .attach('file', buf, 'a.jpg');
  assert.equal(put.status, 200);
  assert.equal(put.body.file_version, 1);

  // Working file exists.
  const workingPath = path.join(PHOTO_DIR, 'working', put.body.basename);
  assert.ok(fs.existsSync(workingPath), 'working file written');

  // Thumb exists.
  const thumbPath = path.join(PHOTO_DIR, 'thumbs', '00000001.jpg');
  assert.ok(fs.existsSync(thumbPath), 'thumb generated');

  // Re-post the same photo record — need_files should now be empty.
  const again = await agent.post('/sync/photos').set('Authorization', bearer()).send({
    photos: [{
      id: 1, sha256: 'aabbccddeeff', mime: 'image/jpeg', file_version: 1,
      source_root: 'photos', source_folder: '_2005-06', source_filename: 'a.jpg',
    }],
  });
  assert.deepEqual(again.body.need_files, [], 'file-version handshake stops re-request');
});

test('bumping file_version re-requests the file', async () => {
  const agent = request(app);
  await agent.post('/sync/photos').set('Authorization', bearer()).send({
    photos: [{
      id: 1, sha256: 'x', mime: 'image/jpeg', file_version: 1,
      source_root: 'p', source_folder: 'f', source_filename: 'a.jpg',
    }],
  });
  await agent.put('/sync/photos/1/file').set('Authorization', bearer()).attach('file', tinyJpeg(), 'a.jpg');
  const bump = await agent.post('/sync/photos').set('Authorization', bearer()).send({
    photos: [{
      id: 1, sha256: 'x', mime: 'image/jpeg', file_version: 2,
      source_root: 'p', source_folder: 'f', source_filename: 'a.jpg',
    }],
  });
  assert.deepEqual(bump.body.need_files, [1], 'v2 needs upload');
});

test('POST /sync/photo_groups is last-writer-wins and syncs soft-deletes', async () => {
  // Set up a group and a photo directly.
  const gid = Number((await pool.query(`insert into groups (name) values ('Clay') returning id`)).rows[0].id);
  const pid = Number((await pool.query(
    `insert into photos (sha256, mime, source_root, source_folder, source_filename, triage_status)
     values ('x','image/jpeg','p','f','a.jpg','keep') returning id`,
  )).rows[0].id);

  const res = await request(app).post('/sync/photo_groups')
    .set('Authorization', bearer())
    .send({ items: [
      { photo_id: pid, group_id: gid, is_deleted: false, updated_at: new Date().toISOString() },
    ] });
  assert.equal(res.status, 200);
  assert.equal(res.body.upserted, 1);
  const row = (await pool.query(`select is_deleted from photo_groups where photo_id = $1`, [pid])).rows[0];
  assert.equal(row.is_deleted, false);

  // Now sync a soft-delete.
  const later = new Date(Date.now() + 60_000).toISOString();
  const res2 = await request(app).post('/sync/photo_groups')
    .set('Authorization', bearer())
    .send({ items: [
      { photo_id: pid, group_id: gid, is_deleted: true, deleted_at: later, updated_at: later },
    ] });
  assert.equal(res2.status, 200);
  const row2 = (await pool.query(`select is_deleted from photo_groups where photo_id = $1`, [pid])).rows[0];
  assert.equal(row2.is_deleted, true, 'soft-delete synced');
});

test('GET /sync/pull/confirmed returns fact writes since cursor + comment/like summaries', async () => {
  const admin = Number((await pool.query(
    `insert into users (email, role) values ('a@x.com','admin') returning id`,
  )).rows[0].id);
  const uid = Number((await pool.query(
    `insert into users (email) values ('u@x.com') returning id`,
  )).rows[0].id);
  const pid = Number((await pool.query(
    `insert into photos (sha256, mime, source_root, source_folder, source_filename, triage_status)
     values ('sh','image/jpeg','p','f','a.jpg','keep') returning id`,
  )).rows[0].id);
  const before = new Date(Date.now() - 1000).toISOString();
  // Simulate an accepted date suggestion + fact-set audit.
  await pool.query(
    `insert into audit_log (user_id, actor, action, entity_type, entity_id, previous_value, new_value)
     values ($1,'a@x.com','photo.capture_date.set','photo',$2,'{}'::jsonb,'{"capture_date":"1962-01-01"}'::jsonb)`,
    [admin, pid],
  );
  await pool.query(`insert into comments (photo_id, user_id, body) values ($1, $2, 'hi')`, [pid, uid]);
  await pool.query(`insert into likes (user_id, photo_id) values ($1, $2)`, [uid, pid]);

  const res = await request(app).get(`/sync/pull/confirmed?since=${encodeURIComponent(before)}`)
    .set('Authorization', bearer());
  assert.equal(res.status, 200);
  assert.ok(res.body.fact_audits.length >= 1);
  assert.ok(res.body.comments_summary.some((c) => Number(c.photo_id) === pid));
  assert.ok(res.body.likes_counts.some((l) => Number(l.photo_id) === pid && l.like_count >= 1));
});

test('GET /sync/status reports the database identity (fix-up 11 push guard)', async () => {
  const res = await request(app).get('/sync/status').set('Authorization', bearer());
  assert.equal(res.status, 200);
  assert.ok(Array.isArray(res.body.tables));
  assert.ok(res.body.db, 'db identity block present');
  const { rows } = await pool.query(
    'select current_database() as name, system_identifier::text as id from pg_control_system()',
  );
  assert.equal(res.body.db.name, rows[0].name);
  assert.equal(res.body.db.system_identifier, rows[0].id);
});
