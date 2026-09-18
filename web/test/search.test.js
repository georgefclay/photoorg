// Search: the resolver (nicknames, maiden names, misspellings), tolerant
// dates, full text, visibility and the SQL triggers (Phase 11).
const test = require('node:test');
const { after, before, beforeEach } = require('node:test');
const {
  pool, makeApp, assert, request, resetDb, seedWorld, agentFor,
  insertPerson, insertPhoto, addToGroup, insertFace,
} = require('./page-helpers');
const { search, resolvePeopleIds } = require('../services/search');
const { parseFilters } = require('../services/photos');

let app;
before(() => { app = makeApp(); });
after(async () => { await pool.end(); });
beforeEach(resetDb);

const ADMIN = { id: 1, role: 'admin', email: 'admin@example.com' };

async function ids(user, q, opts = {}) {
  const r = await search(pool, user, {
    q, filters: parseFilters(opts.query || {}), limit: 50, ...opts,
  });
  return r.photos.items.map((i) => i.id);
}

async function tokensOf(personId) {
  const { rows } = await pool.query(
    `select token, kind from person_search where person_id = $1 order by kind, token`, [personId],
  );
  return rows.map((r) => `${r.kind}:${r.token}`);
}

// ---- the name index -----------------------------------------------------

test('nicknames resolve both ways, variants and maiden names too', async () => {
  const w = await seedWorld();
  // Seeded: Margaret "Peggy" Clay, with Margaret→Peggy/Peg/Meg in the dictionary.
  const t = await tokensOf(w.people.peggy.id);
  assert.ok(t.includes('exact:margaret'), t.join(' '));
  assert.ok(t.includes('exact:peggy'));
  assert.ok(t.includes('nickname:peg'));
  assert.ok(t.includes('nickname:meg'));

  // Someone recorded under her nickname still answers to the full name.
  const peg = await insertPerson({ given_name: 'Peggy', surname: 'Nolan' });
  const pegTokens = await tokensOf(peg.id);
  assert.ok(pegTokens.includes('nickname:margaret'), pegTokens.join(' '));

  // Maiden name and married name both find the same person.
  const kate = await insertPerson({
    given_name: 'Katherine', surname: 'Young', maiden_name: 'Schmidt', nickname: 'Kate',
  });
  const kateTokens = await tokensOf(kate.id);
  assert.ok(kateTokens.includes('exact:schmidt'));
  assert.ok(kateTokens.includes('exact:young'));

  assert.deepEqual(await resolvePeopleIds(pool, 'Peggy'), await resolvePeopleIds(pool, 'peggy'));
  assert.ok((await resolvePeopleIds(pool, 'Peg')).includes(w.people.peggy.id));
  assert.ok((await resolvePeopleIds(pool, 'Margaret')).includes(peg.id));
  assert.ok((await resolvePeopleIds(pool, 'Schmidt')).includes(kate.id));
  assert.ok((await resolvePeopleIds(pool, 'Young')).includes(kate.id));
  assert.ok((await resolvePeopleIds(pool, 'Cathy')).includes(kate.id), 'nickname of Katherine');

  // A hand-curated variant.
  await pool.query(
    `insert into person_name_variants (person_id, variant, kind) values ($1, 'Kitty', 'nickname')`,
    [kate.id],
  );
  assert.ok((await resolvePeopleIds(pool, 'Kitty')).includes(kate.id), 'variant, added after the fact');
});

test('a misspelling finds the right person; a different name does not', async () => {
  await seedWorld();
  const schmidt = await insertPerson({ given_name: 'Anna', surname: 'Schmidt' });
  const smith = await insertPerson({ given_name: 'Ada', surname: 'Smith' });
  const hit = await resolvePeopleIds(pool, 'Schmitt');
  assert.ok(hit.includes(schmidt.id), 'Schmitt → Schmidt');
  assert.ok(!hit.includes(smith.id), 'Schmitt must NOT reach Smith');

  const kathryn = await insertPerson({ given_name: 'Kathryn', surname: 'Bell' });
  assert.ok((await resolvePeopleIds(pool, 'Katherine')).includes(kathryn.id));

  // A phonetic near-miss with low trigram similarity is excluded: "Xu"
  // and "Shaw" share no trigrams even when the metaphone agrees.
  const { rows } = await pool.query(
    `select similarity('schmidt', 'schmitt') as ok, similarity('smith', 'schmitt') as bad`,
  );
  assert.ok(Number(rows[0].ok) >= 0.3);
  assert.ok(Number(rows[0].bad) < 0.3);
});

test('a nickname search ranks the person above a plain word match', async () => {
  const w = await seedWorld();
  // clay1 has Peggy's face; clay2 only mentions "peg" in a comment.
  await pool.query(
    `insert into comments (photo_id, user_id, body) values ($1, $2, 'a peg on the line')`,
    [w.photos.clay2, w.alice.id],
  );
  const order = await ids(ADMIN, 'Peg');
  assert.deepEqual(order.slice(0, 2), [w.photos.clay1, w.photos.clay2],
    'the tagged person outranks the word in a comment');
});

// ---- dates ---------------------------------------------------------------

test('dates are tolerant of precision in both directions', async () => {
  // A neutral folder name: the default fixture folder is "_1962-03", and
  // a folder name is itself searchable (weight D) — which would make this
  // test about the folder, not the date.
  const plain = { source_folder: 'album' };
  const decade = await insertPhoto({ ...plain, capture_date: '1960-01-01', capture_date_precision: 'decade' });
  const year = await insertPhoto({
    ...plain, capture_date: '1962-01-01', capture_date_precision: 'year', capture_date_confirmed: true,
  });
  const month = await insertPhoto({
    ...plain, capture_date: '1962-03-01', capture_date_precision: 'month', capture_date_confirmed: true,
  });
  const other = await insertPhoto({
    ...plain, capture_date: '1975-01-01', capture_date_precision: 'year', capture_date_confirmed: true,
  });

  const in1962 = await ids(ADMIN, '1962');
  assert.ok(in1962.includes(decade), 'a decade-precision photo answers a year inside it');
  assert.ok(in1962.includes(year));
  assert.ok(in1962.includes(month));
  assert.ok(!in1962.includes(other));

  const in60s = await ids(ADMIN, '1960s');
  assert.ok(in60s.includes(year), 'a year-precision photo answers its decade');
  assert.ok(in60s.includes(decade));
  assert.ok(!in60s.includes(other));

  // A month search still finds the year- and decade-precision photos —
  // their spans contain March 1962 — but the tighter answer comes first.
  const march = await ids(ADMIN, 'March 1962');
  assert.equal(march[0], month);
  assert.ok(march.includes(year) && march.includes(decade));
  assert.ok(!march.includes(other));
  assert.ok((await ids(ADMIN, 'before 1970')).includes(decade));
  assert.ok(!(await ids(ADMIN, 'before 1970')).includes(other));
  assert.ok((await ids(ADMIN, 'after 1970')).includes(other));
  const range = await ids(ADMIN, '1961-1963');
  assert.ok(range.includes(year) && range.includes(month) && !range.includes(other));
});

test('a pending date suggestion counts, ranked lower and labelled estimated', async () => {
  const confirmed = await insertPhoto({
    source_folder: 'album', capture_date: '1958-01-01',
    capture_date_precision: 'year', capture_date_confirmed: true,
  });
  const guessed = await insertPhoto({ source_folder: 'album' });
  await pool.query(
    `insert into suggestions (photo_id, kind, payload, source, status)
     values ($1, 'date', '{"date":"1958-01-01","precision":"decade","evidence":"handwritten"}', 'ai', 'pending')`,
    [guessed],
  );
  const r = await search(pool, ADMIN, { q: '1958', filters: parseFilters({}), limit: 50 });
  const order = r.photos.items.map((i) => i.id);
  assert.deepEqual(order, [confirmed, guessed], 'the fact outranks the estimate');
  const why = r.photos.items[1].why.map((w) => w.text).join(' ');
  assert.match(why, /estimated/i);
  assert.match(r.photos.items[0].why.map((w) => w.text).join(' '), /Dated 1958/);
});

// ---- full text -----------------------------------------------------------

test('transcriptions, comments and descriptions all match, in weight order', async () => {
  const w = await seedWorld();
  // clay1: description_ai "Easter picnic at the lake" + back "Peggy and Chuck, Easter 1962".
  const commented = w.photos.clay2;
  await pool.query(
    `insert into comments (photo_id, user_id, body) values ($1, $2, 'Grandma made a picnic that day')`,
    [commented, w.alice.id],
  );
  const pending = await insertPhoto({});
  await addToGroup(pending, w.clay);
  await pool.query(
    `insert into suggestions (photo_id, kind, payload, source, status)
     values ($1, 'description', '{"text":"a picnic on the grass"}', 'ai', 'pending')`,
    [pending],
  );

  const order = await ids(ADMIN, 'picnic');
  assert.equal(order[0], w.photos.clay1, 'the accepted description ranks first');
  assert.ok(order.includes(commented));
  assert.ok(order.includes(pending), 'a pending description is searchable');
  assert.ok(order.indexOf(pending) > 0, 'but ranks below the accepted one');

  const r = await search(pool, ADMIN, { q: 'Easter', filters: parseFilters({}), limit: 10 });
  const first = r.photos.items.find((i) => i.id === w.photos.clay1);
  assert.ok(first, 'the back transcription matches');
  assert.match(first.why.map((x) => x.text).join(' | '), /Easter/);

  // Folder names and the scan locator are searchable, at the lowest weight.
  assert.ok((await ids(ADMIN, 'Batch 00012')).includes(w.photos.clay1));
});

test('two words are AND-ed; a stop word never empties the result', async () => {
  const w = await seedWorld();
  assert.ok((await ids(ADMIN, 'Easter Peggy')).includes(w.photos.clay1));
  assert.deepEqual(await ids(ADMIN, 'Easter zzzznotaword'), []);
  const r = await search(pool, ADMIN, { q: 'the Easter', filters: parseFilters({}), limit: 10 });
  assert.ok(r.photos.items.some((i) => i.id === w.photos.clay1), '"the" is ignored, not fatal');
  assert.ok(r.ignored.includes('the'));
});

// ---- visibility ----------------------------------------------------------

test('search never leaks another group photo — results, counts, autocomplete', async () => {
  const w = await seedWorld();
  await pool.query(`update photos set description_ai = 'a picnic by the river' where id = $1`,
    [w.photos.boots1]);
  const bootsPerson = await insertPerson({ given_name: 'Olive', surname: 'Boots' });
  await insertFace(w.photos.boots1, { personId: bootsPerson.id });

  const alice = { id: w.alice.id, role: 'contributor', email: w.alice.email };
  const carol = { id: w.carol.id, role: 'contributor', email: w.carol.email };

  const aliceHits = await search(pool, alice, { q: 'picnic', filters: parseFilters({}), limit: 50, withCount: true });
  assert.ok(!aliceHits.photos.items.some((i) => i.id === w.photos.boots1));
  assert.equal(aliceHits.count, 1, 'the count is the visible count');

  const carolHits = await search(pool, carol, { q: 'picnic', filters: parseFilters({}), limit: 50, withCount: true });
  assert.deepEqual(carolHits.photos.items.map((i) => i.id), [w.photos.boots1]);
  assert.equal(carolHits.count, 1);

  // The private photo is invisible to everyone, including the admin.
  await pool.query(`update photos set description_ai = 'a private picnic' where id = $1`, [w.photos.priv]);
  const adminHits = await ids(ADMIN, 'picnic');
  assert.ok(!adminHits.includes(w.photos.priv));

  // Autocomplete: Alice is not offered a person she can only see in Boots.
  const a = await agentFor(app, w.alice);
  const ac = await a.get('/api/search/autocomplete?q=Olive');
  assert.equal(ac.status, 200);
  assert.ok(!(ac.body.people || []).some((p) => p.id === bootsPerson.id));
  const c = await agentFor(app, w.carol);
  const ac2 = await c.get('/api/search/autocomplete?q=Olive');
  assert.ok((ac2.body.people || []).some((p) => p.id === bootsPerson.id));
});

// ---- triggers ------------------------------------------------------------

async function tsvOf(photoId) {
  const { rows } = await pool.query(`select tsv::text, names, updated_at from photo_search where photo_id = $1`, [photoId]);
  return rows[0] || null;
}

test('the triggers keep photo_search current, for that photo only', async () => {
  const w = await seedWorld();
  const before = await tsvOf(w.photos.clay1);
  const untouched = await tsvOf(w.photos.clay2);
  assert.ok(before, 'ingest built a row');
  assert.match(before.tsv, /picnic/);

  // A comment.
  await pool.query(`insert into comments (photo_id, user_id, body) values ($1, $2, 'that is the Tabernacle')`,
    [w.photos.clay1, w.alice.id]);
  let now = await tsvOf(w.photos.clay1);
  assert.match(now.tsv, /tabernacl/);
  assert.deepEqual(await tsvOf(w.photos.clay2), untouched, 'no other photo was touched');

  // Hiding it takes it back out.
  await pool.query(`update comments set is_hidden = true where photo_id = $1`, [w.photos.clay1]);
  now = await tsvOf(w.photos.clay1);
  assert.ok(!/tabernacl/.test(now.tsv));

  // Accepting a description.
  await pool.query(`update photos set description_ai = 'children on a porch' where id = $1`, [w.photos.clay2]);
  assert.match((await tsvOf(w.photos.clay2)).tsv, /porch/);

  // Assigning a face.
  const nan = await insertPerson({ given_name: 'Nancy', surname: 'Quill' });
  await insertFace(w.photos.clay2, { personId: nan.id });
  const after = await tsvOf(w.photos.clay2);
  assert.match(after.tsv, /quill/);
  assert.match(after.names, /Nancy Quill/);

  // Renaming a person rewrites every photo they are in.
  await pool.query(`update people set surname = 'Featherstone' where id = $1`, [nan.id]);
  assert.match((await tsvOf(w.photos.clay2)).names, /Featherstone/);
  assert.ok((await resolvePeopleIds(pool, 'Featherstone')).includes(nan.id));
  assert.equal((await resolvePeopleIds(pool, 'Quill')).length, 0, 'the old name is gone');

  // "Not a face" takes the name out again.
  await pool.query(`update faces set is_deleted = true where person_id = $1`, [nan.id]);
  assert.ok(!/featherstone/i.test((await tsvOf(w.photos.clay2)).tsv));

  // A photo delete cascades.
  const doomed = await insertPhoto({});
  assert.ok(await tsvOf(doomed));
  await pool.query(`delete from photos where id = $1`, [doomed]);
  assert.equal(await tsvOf(doomed), null);
});

test('the deferred sweep produces exactly the same rows', async () => {
  const w = await seedWorld();
  const direct = await tsvOf(w.photos.clay1);
  const client = await pool.connect();
  try {
    await client.query('begin');
    await client.query(`set local photoarchive.search_defer = on`);
    await client.query(`update photos set description_ai = 'a deferred description' where id = $1`,
      [w.photos.clay1]);
    await client.query('commit');
  } finally { client.release(); }
  assert.deepEqual((await tsvOf(w.photos.clay1)).tsv, direct.tsv, 'nothing was rewritten yet');
  const { rows } = await pool.query(`select count(*)::int as n from photo_search_dirty`);
  assert.equal(rows[0].n, 1);
  await pool.query(`select sweep_search()`);
  assert.match((await tsvOf(w.photos.clay1)).tsv, /defer/);
  assert.equal((await pool.query(`select count(*)::int as n from photo_search_dirty`)).rows[0].n, 0);
});

// ---- pages and API -------------------------------------------------------

test('/search and /api/search: nickname, why text, sorts, and no 4xx', async () => {
  const w = await seedWorld();
  const a = await agentFor(app, w.alice);

  let r = await a.get('/search?q=Peggy');
  assert.equal(r.status, 200);
  assert.match(r.text, new RegExp(`/photos/${w.photos.clay1}\\?from=`));
  assert.match(r.text, /Margaret/, 'the person chip explains the nickname');

  // Empty search, a search with nothing found, and a junk filter are all 200.
  for (const path of ['/search', '/search?q=', '/search?q=zzzznothing',
    '/search?sort=bogus&completeness_below=nonsense&album_id=abc',
    '/search?q=%22unclosed', '/search?cursor=nonsense&q=peggy']) {
    const res = await a.get(path);
    assert.equal(res.status, 200, path);
  }

  r = await a.get('/api/search?q=Easter&count=1');
  assert.equal(r.status, 200);
  assert.equal(r.body.photos.length, 1);
  assert.equal(r.body.count, 1);
  assert.ok(r.body.photos[0].why.length >= 1);
  assert.match(r.body.photos[0].why[0].text, /Easter/);

  // Every sort answers, and relevance is the default.
  r = await a.get('/api/search?q=Peggy');
  assert.equal(r.body.sort, 'relevance');
  for (const sort of ['recent', 'liked', 'incomplete', 'oldest', 'newest']) {
    const res = await a.get(`/api/search?q=Peggy&sort=${sort}`);
    assert.equal(res.status, 200, sort);
    assert.equal(res.body.sort, sort);
  }

  // A filter with no words still lists photos.
  r = await a.get('/api/search?has_no_date=1&count=1');
  assert.equal(r.status, 200);
  assert.ok(r.body.count >= 1);

  // Anonymous is redirected, not 404'd.
  const anon = await request(app).get('/search?q=peggy');
  assert.equal(anon.status, 302);
});

test('paging a search keeps the ranking and never repeats a photo', async () => {
  const w = await seedWorld();
  for (let i = 0; i < 9; i++) {
    const id = await insertPhoto({ description_ai: 'a picnic in the park' });
    await addToGroup(id, w.clay);
  }
  const a = await agentFor(app, w.alice);
  const seen = [];
  let url = '/api/search?q=picnic&limit=4';
  for (let page = 0; page < 5; page++) {
    const r = await a.get(url);
    assert.equal(r.status, 200);
    seen.push(...r.body.photos.map((p) => p.id));
    if (!r.body.next) break;
    url = `/api/search?q=picnic&limit=4&cursor=${encodeURIComponent(r.body.next)}`;
  }
  assert.equal(new Set(seen).size, seen.length, 'no photo appears twice');
  assert.equal(seen.length, 10, 'clay1 plus the nine new ones');
});

test('the People page finds Margaret when you type Peggy', async () => {
  const w = await seedWorld();
  const a = await agentFor(app, w.alice);
  const r = await a.get('/people?q=Peg');
  assert.equal(r.status, 200);
  assert.match(r.text, new RegExp(`/people/${w.people.peggy.id}"`));
});
