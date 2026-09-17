// Visibility helpers: a photo is visible to a user when they share at
// least one live group with it, or the user is an admin. is_private and
// is_deleted always exclude.
const test = require('node:test');
const { after, before, beforeEach } = require('node:test');
const request = require('supertest');
const {
  pool, makeApp, truncateAll, insertUser, assert,
} = require('./helpers');
const { assertPhotoVisible } = require('../middleware/visibility');

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

beforeEach(async () => {
  await truncateAllWithGroups();
});

async function insertPhoto({ isPrivate = false, isDeleted = false, sha = null } = {}) {
  const s = sha || `sha-${Math.random().toString(36).slice(2, 10)}`;
  const { rows } = await pool.query(
    `insert into photos (sha256, mime, source_root, source_folder, source_filename,
                         is_private, is_deleted, triage_status)
     values ($1, 'image/jpeg', 'photos', '_2005-06', $2, $3, $4, 'keep')
     returning id`,
    [s, `${s}.jpg`, isPrivate, isDeleted],
  );
  return rows[0].id;
}

async function insertGroup(name) {
  const { rows } = await pool.query(
    `insert into groups (name) values ($1) returning id`, [name],
  );
  return rows[0].id;
}

async function addMember(groupId, userId, role = 'member') {
  await pool.query(
    `insert into group_members (group_id, user_id, role) values ($1, $2, $3)`,
    [groupId, userId, role],
  );
}

async function addPhotoToGroup(photoId, groupId) {
  await pool.query(
    `insert into photo_groups (photo_id, group_id) values ($1, $2)`,
    [photoId, groupId],
  );
}

test('admin sees all non-private, non-deleted photos including unfiled', async () => {
  const admin = await insertUser({ email: 'admin@example.com', role: 'admin' });
  const p1 = await insertPhoto();
  const p2 = await insertPhoto();
  const priv = await insertPhoto({ isPrivate: true });
  const del  = await insertPhoto({ isDeleted: true });

  assert.equal(await assertPhotoVisible(pool, admin, p1), true);
  assert.equal(await assertPhotoVisible(pool, admin, p2), true);
  assert.equal(await assertPhotoVisible(pool, admin, priv), false, 'private excluded even for admin');
  assert.equal(await assertPhotoVisible(pool, admin, del), false, 'deleted excluded even for admin');
});

test('contributor sees only photos in a group they share; unfiled is admin-only', async () => {
  const alice = await insertUser({ email: 'alice@example.com' });
  const bob   = await insertUser({ email: 'bob@example.com' });
  const clayFamily = await insertGroup('Clay Family');
  const bootsFamily = await insertGroup('Boots Family');
  await addMember(clayFamily, alice.id);
  await addMember(bootsFamily, bob.id);

  const clayPhoto = await insertPhoto();
  const bootsPhoto = await insertPhoto();
  const unfiled = await insertPhoto();
  await addPhotoToGroup(clayPhoto, clayFamily);
  await addPhotoToGroup(bootsPhoto, bootsFamily);

  assert.equal(await assertPhotoVisible(pool, alice, clayPhoto), true);
  assert.equal(await assertPhotoVisible(pool, alice, bootsPhoto), false);
  assert.equal(await assertPhotoVisible(pool, alice, unfiled), false, 'unfiled hidden from non-admin');
  assert.equal(await assertPhotoVisible(pool, bob, clayPhoto), false);
  assert.equal(await assertPhotoVisible(pool, bob, bootsPhoto), true);
});

test('is_private wins over group membership', async () => {
  const alice = await insertUser({ email: 'alice@example.com' });
  const clay = await insertGroup('Clay Family');
  await addMember(clay, alice.id);

  const privInGroup = await insertPhoto({ isPrivate: true });
  await addPhotoToGroup(privInGroup, clay);
  assert.equal(await assertPhotoVisible(pool, alice, privInGroup), false);
});

test('soft-deleted photo_groups row hides the photo', async () => {
  const alice = await insertUser({ email: 'alice@example.com' });
  const clay = await insertGroup('Clay Family');
  await addMember(clay, alice.id);
  const p = await insertPhoto();
  await addPhotoToGroup(p, clay);
  assert.equal(await assertPhotoVisible(pool, alice, p), true);
  await pool.query(
    `update photo_groups set is_deleted = true, deleted_at = now()
     where photo_id = $1 and group_id = $2`,
    [p, clay],
  );
  assert.equal(await assertPhotoVisible(pool, alice, p), false, 'soft-deleted pg row hides photo');
});

test('soft-deleted group_members row hides the photo from that user', async () => {
  const alice = await insertUser({ email: 'alice@example.com' });
  const clay = await insertGroup('Clay Family');
  await addMember(clay, alice.id);
  const p = await insertPhoto();
  await addPhotoToGroup(p, clay);
  assert.equal(await assertPhotoVisible(pool, alice, p), true);
  await pool.query(
    `update group_members set is_deleted = true, deleted_at = now()
     where group_id = $1 and user_id = $2`,
    [clay, alice.id],
  );
  assert.equal(await assertPhotoVisible(pool, alice, p), false);
});

test('GET /media/thumbs/:id 404s for a non-admin on an unfiled photo', async () => {
  // Unfiled = has zero live photo_groups rows. Admin can see, contributor
  // cannot. The response must be 404 so we don't leak the photo's
  // existence to someone who is not an admin.
  const admin = await insertUser({ email: 'admin@example.com', role: 'admin' });
  const alice = await insertUser({ email: 'alice@example.com' });
  const p = await insertPhoto(); // no photo_groups row → unfiled

  const adminAgent = request.agent(app);
  const aliceAgent = request.agent(app);

  await pool.query(
    `insert into magic_links (user_id, token_hash, expires_at)
     values ($1, encode(sha256($2::bytea), 'hex'), now() + interval '15 minutes')`,
    [admin.id, 'admin-token'],
  );
  await pool.query(
    `insert into magic_links (user_id, token_hash, expires_at)
     values ($1, encode(sha256($2::bytea), 'hex'), now() + interval '15 minutes')`,
    [alice.id, 'alice-token'],
  );
  await adminAgent.post('/a/admin-token');
  await aliceAgent.post('/a/alice-token');

  // Alice must get 404 for an unfiled photo — never 403 (don't confirm
  // the photo exists to a non-admin). Admin also gets 404 here because
  // the file itself doesn't exist on disk, but her visibility check
  // *would* have passed. That's fine — the point of this test is that
  // Alice never reaches the fs read.
  const aliceRes = await aliceAgent.get(`/media/thumbs/${p}`);
  assert.equal(aliceRes.status, 404, 'unfiled photo: non-admin gets 404');
  // And the visibility helper directly confirms the reasoning.
  assert.equal(await assertPhotoVisible(pool, alice, p), false,
    'unfiled photo: contributor visibility is false');
  assert.equal(await assertPhotoVisible(pool, admin, p), true,
    'unfiled photo: admin visibility is true');
});

test('GET /media/thumbs/:id 404s for non-members and 200s for members', async () => {
  const alice = await insertUser({ email: 'alice@example.com' });
  const clay = await insertGroup('Clay Family');
  await addMember(clay, alice.id);
  const p = await insertPhoto();
  await addPhotoToGroup(p, clay);

  // Bob is a non-member.
  const bob = await insertUser({ email: 'bob@example.com' });

  const aliceAgent = request.agent(app);
  const bobAgent = request.agent(app);

  // Fake sign-in via direct session — the media route only reads req.user.
  // We do it via magic-link like the real flow.
  await pool.query(
    `insert into magic_links (user_id, token_hash, expires_at)
     values ($1, encode(sha256($2::bytea), 'hex'), now() + interval '15 minutes')`,
    [alice.id, 'alice-token'],
  );
  await pool.query(
    `insert into magic_links (user_id, token_hash, expires_at)
     values ($1, encode(sha256($2::bytea), 'hex'), now() + interval '15 minutes')`,
    [bob.id, 'bob-token'],
  );
  await aliceAgent.post('/a/alice-token');
  await bobAgent.post('/a/bob-token');

  // File does not exist on disk. Alice may see the photo, so she gets the
  // 200 "no image yet" placeholder (a 404 storm from metadata-only photos
  // trips fail2ban). Bob fails visibility first → 404, never confirming
  // the photo exists. Neither returns 500.
  const aliceRes = await aliceAgent.get(`/media/thumbs/${p}`);
  const bobRes   = await bobAgent.get(`/media/thumbs/${p}`);
  assert.equal(aliceRes.status, 200);
  assert.equal(aliceRes.headers['x-media-placeholder'], '1');
  assert.equal(bobRes.status, 404);
});
