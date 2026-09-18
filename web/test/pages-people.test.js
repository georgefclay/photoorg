// People, person, albums, album and search pages (Phase 10).
const test = require('node:test');
const { after, before, beforeEach } = require('node:test');
const {
  pool, makeApp, assert, request, resetDb, seedWorld, agentFor, insertPerson, insertPhoto, addToGroup, insertFace,
} = require('./page-helpers');

let app;
before(() => { app = makeApp(); });
after(async () => { await pool.end(); });
beforeEach(resetDb);

function tileIds(html) {
  return [...html.matchAll(/href="\/photos\/(\d+)\?from=/g)].map((m) => Number(m[1]));
}
function personLinks(html) {
  return [...html.matchAll(/class="list-link person-row[^"]*" href="\/people\/(\d+)"/g)].map((m) => Number(m[1]));
}
const decode = (s) => s.replace(/&amp;/g, '&').replace(/&#34;/g, '"');

test('anonymous visitors are sent to /login', async () => {
  for (const path of ['/people', '/people/1', '/albums', '/albums/1', '/search', '/search?q=Peggy']) {
    const r = await request(app).get(path);
    assert.equal(r.status, 302, path);
    assert.equal(r.headers.location, '/login', path);
  }
  const post = await request(app).post('/people/1/relationships').type('form').send({ relation: 'spouse' });
  assert.notEqual(post.status, 200);
  assert.equal((await pool.query(`select count(*)::int as n from suggestions`)).rows[0].n, 0);
});

test('every page renders with empty data (no people, albums or photos)', async () => {
  const { insertUser } = require('./page-helpers');
  const u = await insertUser({ email: 'lonely@example.com' });
  const a = await agentFor(app, u);
  let r = await a.get('/people');
  assert.equal(r.status, 200);
  assert.match(r.text, /No people yet/);
  r = await a.get('/people?q=zzz');
  assert.equal(r.status, 200);
  assert.match(r.text, /No one called/);
  r = await a.get('/albums');
  assert.equal(r.status, 200);
  assert.match(r.text, /No albums yet/);
  r = await a.get('/search');
  assert.equal(r.status, 200);
  assert.match(r.text, /Search by name \(a nickname or a maiden name works\)/);
  r = await a.get('/search?q=nothing');
  assert.equal(r.status, 200);
  assert.match(r.text, /No photos matched/);
  assert.equal((await a.get('/people/999')).status, 404);
  assert.equal((await a.get('/albums/999')).status, 404);
  for (const p of ['/css/people.css', '/js/people.js', '/js/search.js']) {
    assert.equal((await request(app).get(p)).status, 200, p);
  }
});

test('/people lists everyone site-wide, with counts that respect visibility', async () => {
  const w = await seedWorld();
  const alice = await agentFor(app, w.alice);
  let r = await alice.get('/people');
  assert.equal(r.status, 200);
  assert.match(r.text, /aria-current="page"[^>]*>People|href="\/people" aria-current="page"/);
  // Chuck Clay sorts before Margaret Clay (display names).
  const ids = personLinks(r.text);
  assert.deepEqual([...ids].sort(), [w.people.peggy.id, w.people.chuck.id].sort());
  assert.match(r.text, /born 1931 · 1 photo, 1962/);
  assert.match(r.text, /No photos you can see yet/, 'Chuck has no tagged photos');

  const carol = await agentFor(app, w.carol);
  r = await carol.get('/people');
  assert.equal(r.status, 200);
  assert.equal(personLinks(r.text).length, 2, 'people are site-wide');
  assert.doesNotMatch(r.text, /1 photo, 1962/, 'but Carol cannot see Peggy\'s photo');

  r = await alice.get('/people?q=pegg');
  assert.deepEqual(personLinks(r.text), [w.people.peggy.id]);
  assert.match(r.text, /value="pegg"/);
});

test('/people "More people" pages with a keyset cursor', async () => {
  const w = await seedWorld();
  await pool.query(
    `insert into people (given_name, surname) select 'Test', 'Person ' || lpad(g::text, 3, '0') from generate_series(1, 101) g`,
  );
  const admin = await agentFor(app, w.admin);
  const r = await admin.get('/people');
  const first = personLinks(r.text);
  assert.equal(first.length, 100);
  const m = /data-people-more href="([^"]+)"/.exec(r.text);
  assert.ok(m && m[1].includes('cursor='), 'more link carries cursor');
  const r2 = await admin.get(decode(m[1]));
  assert.equal(r2.status, 200);
  const second = personLinks(r2.text);
  assert.equal(second.length, 3);
  assert.equal(new Set([...first, ...second]).size, 103);
  assert.match(r2.text, /Back to the start/);
});

test('/people/:id shows names, family, and only visible photos', async () => {
  const w = await seedWorld();
  await pool.query(`insert into person_name_variants (person_id, variant, kind) values ($1, 'Maggie', 'nickname')`, [w.people.peggy.id])
    .catch(() => pool.query(`insert into person_name_variants (person_id, variant) values ($1, 'Maggie')`, [w.people.peggy.id]));
  const alice = await agentFor(app, w.alice);
  let r = await alice.get(`/people/${w.people.peggy.id}`);
  assert.equal(r.status, 200);
  assert.match(r.text, /Margaret/);
  assert.match(r.text, /<dt>Nickname<\/dt><dd>Peggy<\/dd>/);
  assert.match(r.text, /<dt>Surname<\/dt><dd>Clay<\/dd>/);
  assert.match(r.text, /Also known as<\/dt><dd>Maggie/);
  assert.match(r.text, /<dt>Born<\/dt><dd>1931<\/dd>/);
  assert.match(r.text, /<h3 class="rel-title">Spouse<\/h3>\s*<ul class="chips rel-list" data-rel="spouses">\s*<li><a class="chip" href="\/people\/(\d+)">Charles/);
  assert.match(r.text, new RegExp(`data-rel="spouses">\\s*<li><a class="chip" href="/people/${w.people.chuck.id}"`));
  assert.deepEqual(tileIds(r.text), [w.photos.clay1]);
  assert.match(r.text, new RegExp(`from=${encodeURIComponent(`p.${w.people.peggy.id}`)}`));
  assert.match(r.text, /Suggest a relationship/);

  const carol = await agentFor(app, w.carol);
  r = await carol.get(`/people/${w.people.peggy.id}`);
  assert.equal(r.status, 200);
  assert.deepEqual(tileIds(r.text), []);
  assert.match(r.text, /No photos of Margaret Clay you can see yet|No photos of .* you can see yet/);

  // Chuck's page shows Peggy as spouse too.
  r = await alice.get(`/people/${w.people.chuck.id}`);
  assert.match(r.text, new RegExp(`data-rel="spouses">\\s*<li><a class="chip" href="/people/${w.people.peggy.id}"`));

  // Deleted / missing people are 404.
  await pool.query(`update people set is_deleted = true where id = $1`, [w.people.chuck.id]);
  assert.equal((await alice.get(`/people/${w.people.chuck.id}`)).status, 404);
  assert.equal((await alice.get('/people/987654')).status, 404);
});

test('person page: derived siblings are marked; photo grid respects the header scope', async () => {
  const w = await seedWorld();
  const kid1 = await insertPerson({ given_name: 'Ann', surname: 'Clay' });
  const kid2 = await insertPerson({ given_name: 'Bill', surname: 'Clay' });
  await pool.query(
    `insert into relationships (person_a_id, person_b_id, type, confirmed) values ($1, $2, 'parent', true), ($1, $3, 'parent', true)`,
    [w.people.peggy.id, kid1.id, kid2.id],
  );
  const admin = await agentFor(app, w.admin);
  let r = await admin.get(`/people/${kid1.id}`);
  assert.match(r.text, /data-rel="parents">\s*<li><a class="chip" href="\/people\/\d+">Margaret/);
  assert.match(r.text, /Bill Clay <span class="rel-note">\(same parents\)<\/span>/);
  r = await admin.get(`/people/${w.people.peggy.id}`);
  assert.match(r.text, /Children/);

  // Peggy also appears on a Boots photo; admin scope switches the grid.
  const b2 = await insertPhoto({});
  await addToGroup(b2, w.boots);
  await insertFace(b2, { personId: w.people.peggy.id });
  r = await admin.get(`/people/${w.people.peggy.id}`);
  assert.deepEqual(tileIds(r.text).sort(), [w.photos.clay1, b2].sort());
  assert.match(r.text, /2 photos/);
  await admin.post('/scope').type('form').send({ _csrf: admin.csrf, scope: String(w.boots), return_to: '/' });
  r = await admin.get(`/people/${w.people.peggy.id}`);
  assert.deepEqual(tileIds(r.text), [b2]);
  assert.match(r.text, /1 photo in Boots Family/);
  r = await admin.get('/people');
  assert.match(r.text, /Photo counts are for <strong>Boots Family<\/strong>/);
});

test('relationship suggestions: API and no-JS form create pending suggestions shown on the page', async () => {
  const w = await seedWorld();
  const kid = await insertPerson({ given_name: 'Ann', surname: 'Clay' });
  const alice = await agentFor(app, w.alice);

  // JSON (what people.js sends): "Ann is Peggy's child" → parent row a=Peggy, b=Ann.
  const api = await alice.post('/api/relationships').set('X-CSRF-Token', alice.csrf)
    .send({ person_a_id: w.people.peggy.id, person_b_id: kid.id, type: 'parent' });
  assert.equal(api.status, 201);
  const dup = await alice.post('/api/relationships').set('X-CSRF-Token', alice.csrf)
    .send({ person_a_id: w.people.peggy.id, person_b_id: kid.id, type: 'parent' });
  assert.equal(dup.status, 200);
  assert.equal(dup.body.duplicate, true);
  const known = await alice.post('/api/relationships').set('X-CSRF-Token', alice.csrf)
    .send({ person_a_id: w.people.chuck.id, person_b_id: w.people.peggy.id, type: 'spouse' });
  assert.equal(known.status, 409, 'spouse already recorded (either order)');

  let r = await alice.get(`/people/${w.people.peggy.id}`);
  assert.match(r.text, /Suggested, waiting for review/);
  assert.match(r.text, new RegExp(`<a href="/people/${kid.id}">Ann Clay</a> — their child`));
  r = await alice.get(`/people/${kid.id}`);
  assert.match(r.text, new RegExp(`<a href="/people/${w.people.peggy.id}">Margaret[^<]*</a> — their parent`));

  // No-JS form with a typed name: on Ann's page, "Charles Clay … is their parent"
  // → parent row a=Chuck (the other person), b=Ann.
  const noCsrf = await alice.post(`/people/${kid.id}/relationships`).type('form')
    .send({ other_name: 'Chuck', relation: 'parent' });
  assert.equal(noCsrf.status, 403);
  const form = await alice.post(`/people/${kid.id}/relationships`).type('form')
    .send({ _csrf: alice.csrf, other_name: 'Charles Clay', other_id: '', relation: 'parent' });
  assert.equal(form.status, 303);
  assert.equal(form.headers.location, `/people/${kid.id}?rel=sent#suggest`);
  const { rows } = await pool.query(
    `select payload, status, source, user_id from suggestions where kind = 'relationship' order by id`,
  );
  assert.equal(rows.length, 2);
  assert.deepEqual(rows[1].payload, { person_a_id: w.people.chuck.id, person_b_id: kid.id, type: 'parent' });
  assert.equal(rows[1].status, 'pending');
  assert.equal(rows[1].source, 'human');
  assert.equal(Number(rows[1].user_id), Number(w.alice.id));
  r = await alice.get(`/people/${kid.id}?rel=sent`);
  assert.match(r.text, /Thank you! Your suggestion is waiting for review/);
  assert.match(r.text, /Charles[^<]*Clay<\/a> — their parent/);

  // Unknown name → friendly 422, nothing filed; missing relation → 422.
  r = await alice.post(`/people/${kid.id}/relationships`).type('form')
    .send({ _csrf: alice.csrf, other_name: 'Nobody Atall', relation: 'sibling' });
  assert.equal(r.status, 422);
  assert.match(r.text, /couldn(&#39;|')t find anyone called/);
  r = await alice.post(`/people/${kid.id}/relationships`).type('form')
    .send({ _csrf: alice.csrf, other_name: 'Chuck', relation: 'cousin' });
  assert.equal(r.status, 422);
  // Self-link refused.
  r = await alice.post(`/people/${kid.id}/relationships`).type('form')
    .send({ _csrf: alice.csrf, other_id: String(kid.id), relation: 'sibling' });
  assert.equal(r.status, 422);
  assert.equal((await pool.query(`select count(*)::int as n from suggestions where kind = 'relationship'`)).rows[0].n, 2);
  // Contributors cannot create people from here.
  const create = await alice.post('/api/people').set('X-CSRF-Token', alice.csrf).send({ given_name: 'New' });
  assert.equal(create.status, 403);
});

test('/albums and /albums/:id: counts and grids respect visibility and scope', async () => {
  const w = await seedWorld();
  const boots2 = await insertPhoto({});
  await addToGroup(boots2, w.boots);
  await pool.query(`insert into album_photos (album_id, photo_id, position) values ($1, $2, 3)`, [w.album, boots2]);
  await pool.query(`insert into albums (name, source) values ('Empty album', 'import')`);

  const count = (html) => {
    const m = /Summer 1992 — Canada<\/span>\s*<span class="person-sub">([^<]+)</.exec(html);
    return m && m[1];
  };
  const admin = await agentFor(app, w.admin);
  const alice = await agentFor(app, w.alice);
  const carol = await agentFor(app, w.carol);
  const olive = await agentFor(app, w.olive);

  let r = await admin.get('/albums');
  assert.equal(r.status, 200);
  assert.equal(count(r.text), '3 photos');
  assert.match(r.text, /Empty album<\/span>\s*<span class="person-sub">No photos you can see yet/);
  assert.match(r.text, /album-row no-photos/);
  assert.match(r.text, /<img src="\/media\/thumbs\/\d+"/);
  assert.equal(count((await alice.get('/albums')).text), '2 photos');
  assert.equal(count((await carol.get('/albums')).text), '1 photo');
  r = await olive.get('/albums');
  assert.match(r.text, /Summer 1992 — Canada<\/span>\s*<span class="person-sub">No photos you can see yet/, 'site-wide list');

  // Album page: album order (clay2 pos 1, clay1 pos 2, boots2 pos 3).
  r = await admin.get(`/albums/${w.album}`);
  assert.equal(r.status, 200);
  assert.deepEqual(tileIds(r.text), [w.photos.clay2, w.photos.clay1, boots2]);
  assert.match(r.text, new RegExp(`from=a\\.${w.album}`));
  assert.doesNotMatch(r.text, /Add to album|Rename/i, 'read-only');
  assert.deepEqual(tileIds((await alice.get(`/albums/${w.album}`)).text), [w.photos.clay2, w.photos.clay1]);
  r = await olive.get(`/albums/${w.album}`);
  assert.equal(r.status, 200);
  assert.deepEqual(tileIds(r.text), []);
  assert.match(r.text, /No photos you can see in this album yet/);

  // Scope.
  await admin.post('/scope').type('form').send({ _csrf: admin.csrf, scope: String(w.boots), return_to: '/albums' });
  r = await admin.get('/albums');
  assert.equal(count(r.text), '1 photo');
  r = await admin.get(`/albums/${w.album}`);
  assert.deepEqual(tileIds(r.text), [boots2]);
  assert.match(r.text, /1 photo in Boots Family/);
  await admin.post('/scope').type('form').send({ _csrf: admin.csrf, scope: 'unfiled', return_to: '/albums' });
  r = await admin.get(`/albums/${w.album}`);
  assert.match(r.text, /Nothing from Unfiled in this album/);
});

test('/search: people and photos by name, words, years and filters', async () => {
  const w = await seedWorld();
  const alice = await agentFor(app, w.alice);

  let r = await alice.get('/search');
  assert.equal(r.status, 200);
  assert.match(r.text, /What are you looking for\?/);
  assert.deepEqual(tileIds(r.text), []);

  r = await alice.get('/search?q=Peggy');
  assert.equal(r.status, 200);
  assert.match(r.text, /id="site-search"[^>]*value="Peggy"/, 'header search box keeps the text');
  assert.match(r.text, new RegExp(`href="/people/${w.people.peggy.id}"`), 'matching person listed');
  assert.deepEqual(tileIds(r.text), [w.photos.clay1]);
  assert.match(r.text, /from=b\.recent/);
  assert.match(r.text, /1 photo found/);

  // Words: the seeded description ("Easter picnic at the lake").
  r = await alice.get('/search?q=picnic');
  assert.deepEqual(tileIds(r.text), [w.photos.clay1]);
  // Back transcription, indexed by the Phase 11 triggers.
  r = await alice.get('/search?q=Easter');
  assert.deepEqual(tileIds(r.text), [w.photos.clay1]);

  // Carol can't see clay1.
  const carol = await agentFor(app, w.carol);
  r = await carol.get('/search?q=Peggy');
  assert.deepEqual(tileIds(r.text), []);
  assert.match(r.text, /No photos matched/);

  // Year range (swapped bounds are tolerated).
  r = await alice.get('/search?year_from=1960&year_to=1965');
  assert.deepEqual(tileIds(r.text), [w.photos.clay1]);
  r = await alice.get('/search?year_from=1975&year_to=1965');
  assert.deepEqual(tileIds(r.text), [w.photos.clay2]);
  assert.match(r.text, /name="year_from"[^>]*value="1965"/);
  r = await alice.get('/search?year_from=1990');
  assert.deepEqual(tileIds(r.text), []);

  // Person / place / album / checkboxes.
  r = await alice.get(`/search?person_id=${w.people.peggy.id}`);
  assert.deepEqual(tileIds(r.text), [w.photos.clay1]);
  assert.match(r.text, /name="person" value="Margaret[^"]*"/);
  r = await alice.get('/search?person=Peggy'); // typed name, no JS
  assert.deepEqual(tileIds(r.text), [w.photos.clay1]);
  // No-JS form sends the text next to the hidden id: emptied text drops the filter.
  r = await alice.get(`/search?person=&person_id=${w.people.peggy.id}`);
  assert.match(r.text, /What are you looking for\?/);
  r = await alice.get(`/search?person=${encodeURIComponent(w.people.peggy.display_name)}&person_id=${w.people.peggy.id}`);
  assert.deepEqual(tileIds(r.text), [w.photos.clay1]);
  r = await alice.get('/search?person=Nobody');
  assert.match(r.text, /couldn(&#39;|')t find anyone called/);
  r = await alice.get(`/search?place_id=${w.place}`);
  assert.deepEqual(tileIds(r.text), [w.photos.clay1]);
  assert.match(r.text, /name="place" value="Toronto"/);
  r = await alice.get('/search?place=toronto');
  assert.deepEqual(tileIds(r.text), [w.photos.clay1]);
  r = await alice.get(`/search?album_id=${w.album}`);
  assert.deepEqual(tileIds(r.text).sort(), [w.photos.clay1, w.photos.clay2].sort());
  assert.match(r.text, new RegExp(`<option value="${w.album}" selected>`));
  r = await alice.get('/search?has_no_date=1');
  assert.deepEqual(tileIds(r.text), [w.photos.clay2]);
  r = await alice.get('/search?has_untagged_faces=1');
  assert.deepEqual(tileIds(r.text), [w.photos.clay1]);

  // The infinite-scroll endpoint in the grid returns the same photos.
  r = await alice.get('/search?q=Peggy');
  const m = /data-api="([^"]+)"/.exec(r.text);
  assert.ok(m);
  const apiRes = await alice.get(decode(m[1]));
  assert.equal(apiRes.status, 200);
  assert.deepEqual(apiRes.body.photos.map((p) => p.id), [w.photos.clay1]);
});

test('/search pages with a cursor (no-JS "More photos")', async () => {
  const w = await seedWorld();
  for (let i = 0; i < 61; i++) {
    await addToGroup(await insertPhoto({ capture_date: '1980-06-01', capture_date_precision: 'year' }), w.clay);
  }
  const alice = await agentFor(app, w.alice);
  const r = await alice.get('/search?year_from=1980&year_to=1980');
  assert.equal(tileIds(r.text).length, 60);
  assert.match(r.text, /61 photos found/);
  const m = /data-grid-more href="([^"]+)"/.exec(r.text);
  assert.ok(m, 'more link present');
  const page2 = await alice.get(decode(m[1]));
  assert.equal(page2.status, 200);
  assert.equal(tileIds(page2.text).length, 1);
  assert.match(page2.text, /Back to the start/);
});
