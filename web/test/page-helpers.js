// Shared fixtures for Phase 10 page tests.
//
//   const { pool, makeApp, assert, resetDb, seedWorld, signIn, agentFor } = require('./page-helpers');
//   beforeEach(resetDb);
//   const w = await seedWorld();
//   const a = await agentFor(app, w.alice);   // supertest agent, signed in; a.csrf is the token
//
// Run a file in isolation (own schema, safe to run next to other runs):
//   TEST_DB_SCHEMA=t_mine npm run test:setup
//   TEST_DB_SCHEMA=t_mine node --test --test-concurrency=1 test/pages-browse.test.js
const request = require('supertest');
const helpers = require('./helpers');

const { pool, insertUser } = helpers;

const ALL_TABLES = `
  photo_search, person_search, photo_search_dirty, person_search_dirty, place_aliases,
  photo_groups, group_members, groups,
  audit_log, magic_links, access_requests, "session",
  contribution_files, contributions,
  nickname_dictionary, suggestions, likes, comments, faces, photo_backs,
  album_photos, albums, photo_places, places, relationships,
  person_name_variants, people, photo_masters, photos, users`;

async function resetDb() {
  await pool.query(`truncate table ${ALL_TABLES} restart identity cascade`);
  helpers.email.clearOutbox();
}

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

async function agentFor(app, user) {
  const agent = request.agent(app);
  agent.csrf = await signIn(agent, user.id);
  return agent;
}

let shaSeq = 0;
async function insertPhoto(fields = {}) {
  shaSeq += 1;
  const f = {
    sha256: `sha-${shaSeq}-${Math.random().toString(36).slice(2, 8)}`,
    mime: 'image/jpeg', source_root: 'photos', source_folder: '_1962-03', source_filename: `f${shaSeq}.jpg`,
    triage_status: 'keep', width: 1200, height: 800, working_path: `0000000${shaSeq}_abcdef12.jpg`,
    synced_file_version: 1, // the file was pushed (pass null for metadata-only)
    ...fields,
  };
  const cols = Object.keys(f);
  const { rows } = await pool.query(
    `insert into photos (${cols.join(', ')}) values (${cols.map((_, i) => `$${i + 1}`).join(', ')}) returning id`,
    cols.map((c) => f[c]),
  );
  return Number(rows[0].id);
}

async function insertGroup(name, members = []) {
  const id = Number((await pool.query(`insert into groups (name) values ($1) returning id`, [name])).rows[0].id);
  for (const { user, role = 'member' } of members) {
    await pool.query(`insert into group_members (group_id, user_id, role) values ($1, $2, $3)`, [id, user.id, role]);
  }
  return id;
}

async function addToGroup(photoId, groupId) {
  await pool.query(`insert into photo_groups (photo_id, group_id) values ($1, $2)`, [photoId, groupId]);
}

async function insertPerson(fields) {
  const cols = Object.keys(fields);
  const { rows } = await pool.query(
    `insert into people (${cols.join(', ')}) values (${cols.map((_, i) => `$${i + 1}`).join(', ')}) returning id, display_name`,
    cols.map((c) => fields[c]),
  );
  return { id: Number(rows[0].id), display_name: rows[0].display_name };
}

async function insertFace(photoId, { personId = null, bbox = { x: 100, y: 100, w: 120, h: 120 }, review = 'pending', source = 'ai' } = {}) {
  const { rows } = await pool.query(
    `insert into faces (photo_id, person_id, bbox, source, review_status) values ($1, $2, $3::jsonb, $4, $5) returning id`,
    [photoId, personId, bbox, source, review],
  );
  return Number(rows[0].id);
}

// A small world covering visibility, groups, moderation and every
// "needs attention" kind:
//   admin; alice (Clay member); bob (Clay moderator); carol (Boots member); olive (no groups)
//   clay1: dated 1962 confirmed, Peggy tagged, one unknown face, one untagged face, a back, a pending suggestion
//   clay2: undated; boots1: Boots only; unfiled: no group; priv: private (never visible)
async function seedWorld() {
  const admin = await insertUser({ email: 'admin@example.com', role: 'admin', displayName: 'Admin' });
  const alice = await insertUser({ email: 'alice@example.com', displayName: 'Alice' });
  const bob = await insertUser({ email: 'bob@example.com', displayName: 'Bob' });
  const carol = await insertUser({ email: 'carol@example.com', displayName: 'Carol' });
  const olive = await insertUser({ email: 'olive@example.com', displayName: 'Olive' });
  const clay = await insertGroup('Clay Family', [{ user: alice }, { user: bob, role: 'moderator' }]);
  const boots = await insertGroup('Boots Family', [{ user: carol }]);

  const clay1 = await insertPhoto({
    capture_date: '1962-03-01', capture_date_precision: 'month', capture_date_confirmed: true,
    scan_batch: 'Batch 00012', scan_sequence: 17, is_scan: true,
  });
  const clay2 = await insertPhoto({ capture_date: '1970-01-01', capture_date_precision: 'decade' });
  const boots1 = await insertPhoto({});
  const unfiled = await insertPhoto({});
  const priv = await insertPhoto({ is_private: true, triage_status: 'private' });
  await addToGroup(clay1, clay);
  await addToGroup(clay2, clay);
  await addToGroup(boots1, boots);
  await addToGroup(priv, clay);

  // A slice of the real nickname seed, inserted BEFORE the people so the
  // Phase 11 trigger builds their nickname tokens.
  await pool.query(
    `insert into nickname_dictionary (canonical, variant) values
       ('Margaret', 'Peggy'), ('Margaret', 'Peg'), ('Margaret', 'Meg'),
       ('Charles', 'Chuck'), ('Charles', 'Charlie'),
       ('Katherine', 'Kate'), ('Katherine', 'Cathy'), ('Katherine', 'Kathy')
     on conflict do nothing`,
  );
  const peggy = await insertPerson({ given_name: 'Margaret', surname: 'Clay', nickname: 'Peggy', birth_year: 1931 });
  const chuck = await insertPerson({ given_name: 'Charles', surname: 'Clay', nickname: 'Chuck' });
  const faces = {
    peggy: await insertFace(clay1, { personId: peggy.id, bbox: { x: 100, y: 100, w: 150, h: 150 } }),
    unknown: await insertFace(clay1, { review: 'unknown', bbox: { x: 400, y: 120, w: 140, h: 140 } }),
    untagged: await insertFace(clay1, { bbox: { x: 700, y: 150, w: 120, h: 120 } }),
    bootsUnknown: await insertFace(boots1, { review: 'unknown' }),
  };
  await pool.query(
    `insert into photo_backs (photo_id, master_path, sha256, working_path, transcribed_text, transcription_confidence)
     values ($1, 'D:/Scanned Photos/Batch 00012/0018.jpg', 'back-sha', 'b1.jpg', 'Peggy and Chuck, Easter 1962', 0.9)`, [clay1],
  );
  const album = Number((await pool.query(
    `insert into albums (name, source) values ('Summer 1992 — Canada', 'import') returning id`,
  )).rows[0].id);
  await pool.query(`insert into album_photos (album_id, photo_id, position) values ($1, $2, 1), ($1, $3, 2)`, [album, clay2, clay1]);
  const place = Number((await pool.query(`insert into places (name) values ('Toronto') returning id`)).rows[0].id);
  await pool.query(`insert into photo_places (photo_id, place_id, confirmed) values ($1, $2, true)`, [clay1, place]);
  await pool.query(`insert into relationships (person_a_id, person_b_id, type, confirmed) values ($1, $2, 'spouse', true)`, [peggy.id, chuck.id]);
  const suggestion = Number((await pool.query(
    `insert into suggestions (photo_id, user_id, kind, payload, source, status)
     values ($1, $2, 'date', '{"date":"1962-04-01","precision":"month","evidence":"April 1962"}', 'human', 'pending')
     returning id`, [clay1, alice.id],
  )).rows[0].id);
  // Real text, so the Phase 11 triggers build a real search row for it.
  await pool.query(`update photos set description_ai = 'Easter picnic at the lake' where id = $1`, [clay1]);
  await pool.query(`select refresh_completeness(id) from photos`);

  return {
    admin, alice, bob, carol, olive, clay, boots,
    photos: { clay1, clay2, boots1, unfiled, priv },
    people: { peggy, chuck }, faces, album, place, suggestion,
  };
}

module.exports = {
  ...helpers,
  request,
  resetDb,
  signIn,
  agentFor,
  insertPhoto,
  insertGroup,
  addToGroup,
  insertPerson,
  insertFace,
  seedWorld,
};
