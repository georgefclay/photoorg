// Phase 15 — fixing a mistake must never need SQL.
//
// The web half of the rule. Three things are being held here, and the
// third is the one that is easy to get wrong:
//
//   1. an admin can correct text that is already on the site — a pending
//      suggestion's wording, a person's name, a comment body;
//   2. a bulk find & replace previews, applies under one audited batch,
//      and undoes from the audit log, skipping anything edited since;
//   3. **a desktop push does not clobber a web edit, and a desktop edit
//      is not blocked by a stale web one.** That cannot be decided by
//      `updated_at`: every /sync/* upsert sets it, so on the web it means
//      "when a push last touched this row". Hence
//      `edited_on_web_at` / `edited_on_desktop_at`, written only by a
//      human edit in a UI, with ties to the web.
const test = require('node:test');
const { after, before, beforeEach } = require('node:test');
const {
  pool, makeApp, assert, request, resetDb, seedWorld, agentFor, insertPhoto,
} = require('./page-helpers');

let app;
before(() => { app = makeApp(); });
after(async () => { await pool.end(); });
beforeEach(resetDb);

const TOKEN = () => `Bearer ${process.env.SERVICE_TOKEN}`;
function push(url, body) {
  return request(app).post(url).set('Authorization', TOKEN()).send(body);
}
function pull(url) {
  return request(app).get(url).set('Authorization', TOKEN());
}
async function one(sql, params) {
  const { rows } = await pool.query(sql, params);
  return rows[0];
}

// ---------------------------------------------------------------------
// 0. The replacement itself. Pure, no database — and the one place the
//    two tiers could silently disagree about what a correction means, so
//    the cases here mirror the desktop's `test_replace_text_*` exactly.
// ---------------------------------------------------------------------

test('a replacement is literal text, never a regex template', () => {
  const { replaceText } = require('../services/corrections');
  // `$1` and `$&` are replacement-pattern syntax in String.replace, and a
  // backslash is in Python's re.sub. Both tiers must store them verbatim.
  assert.equal(replaceText('a-b', '-', '$1', true), 'a$1b');
  assert.equal(replaceText('a-b', '-', '$1', false), 'a$1b');
  assert.equal(replaceText('a-b', '-', '$&', false), 'a$&b');
  assert.equal(replaceText('a-b', '-', '\\1', false), 'a\\1b');
  // A needle full of metacharacters is a needle, not a pattern.
  assert.equal(replaceText('a.b.c', '.', '_', true), 'a_b_c');
  assert.equal(replaceText('a.b.c', '.', '_', false), 'a_b_c');
  assert.equal(replaceText('aXb', 'x', '+', false), 'a+b');
  assert.equal(replaceText('aXb', 'x', '+', true), 'aXb');
  // Nothing to do cases.
  assert.equal(replaceText('abc', '', 'X', true), 'abc');
  assert.equal(replaceText(null, 'a', 'X', true), null);
});

// ---------------------------------------------------------------------
// 1. Inline edit of one pending suggestion
// ---------------------------------------------------------------------

test('an admin can correct a pending suggestion and the rest of the payload survives', async () => {
  const w = await seedWorld();
  const a = await agentFor(app, w.admin);
  const photo = w.photos.clay1;
  const id = Number((await one(
    `insert into suggestions (photo_id, kind, payload, source, status)
     values ($1, 'description', $2::jsonb, 'ai', 'pending') returning id`,
    [photo, JSON.stringify({
      text: 'A lake in Canaca.', tags: ['lake'], prompt_version: 'describe.v1',
    })],
  )).id);

  const res = await a.patch(`/api/admin/suggestions/${id}`)
    .set('X-CSRF-Token', a.csrf)
    .send({ text: 'A lake in Canada.' });
  assert.equal(res.status, 200);
  assert.equal(res.body.changed, true);

  const row = await one('select payload, edited_on_web_at from suggestions where id = $1', [id]);
  assert.equal(row.payload.text, 'A lake in Canada.');
  assert.deepEqual(row.payload.tags, ['lake'], 'jsonb_set must leave the rest alone');
  assert.equal(row.payload.prompt_version, 'describe.v1');
  // Stamped, so the laptop's next push cannot hand the old text back.
  assert.ok(row.edited_on_web_at);

  const audit = await one(
    `select actor, previous_value, new_value from audit_log
      where action = 'suggestion.edit' and entity_id = $1`, [id]);
  assert.equal(audit.actor, w.admin.email);
  assert.equal(audit.previous_value.value, 'A lake in Canaca.');
  assert.equal(audit.new_value.value, 'A lake in Canada.');
});

test('a resolved suggestion cannot be re-worded', async () => {
  // Its text is what the decision was made on; rewriting it would
  // falsify the decision.
  const w = await seedWorld();
  const a = await agentFor(app, w.admin);
  const id = Number((await one(
    `insert into suggestions (photo_id, kind, payload, source, status, resolved_at)
     values ($1, 'description', '{"text":"Canaca"}', 'ai', 'accepted', now()) returning id`,
    [w.photos.clay1],
  )).id);

  const res = await a.patch(`/api/admin/suggestions/${id}`)
    .set('X-CSRF-Token', a.csrf).send({ text: 'Canada' });
  assert.equal(res.status, 409);
  assert.equal((await one('select payload from suggestions where id = $1', [id])).payload.text, 'Canaca');
});

test('a contributor cannot edit a suggestion', async () => {
  const w = await seedWorld();
  const a = await agentFor(app, w.alice);
  const res = await a.patch(`/api/admin/suggestions/${w.suggestion}`)
    .set('X-CSRF-Token', a.csrf).send({ text: 'nope' });
  assert.equal(res.status, 403);
});

// ---------------------------------------------------------------------
// 2. Bulk find & replace: preview, apply, undo
// ---------------------------------------------------------------------

async function seedTypos(photo, n = 3) {
  const ids = [];
  for (let i = 0; i < n; i += 1) {
    ids.push(Number((await one(
      `insert into suggestions (photo_id, kind, payload, source, status)
       values ($1, 'description', $2::jsonb, 'ai', 'pending') returning id`,
      [photo, JSON.stringify({ text: `Photo ${i} from Canaca.` })],
    )).id));
  }
  return ids;
}

test('find & replace previews the exact replacement, then applies and audits it', async () => {
  const w = await seedWorld();
  const a = await agentFor(app, w.admin);
  const ids = await seedTypos(w.photos.clay1, 3);
  // A date suggestion's evidence is text too.
  const dateId = Number((await one(
    `insert into suggestions (photo_id, kind, payload, source, status)
     values ($1, 'date', '{"date":"1992-07-01","precision":"month","evidence":"Canaca trip"}', 'ai', 'pending')
     returning id`, [w.photos.clay1],
  )).id);

  const prev = await a.get('/api/admin/corrections/search?q=Canaca&replace=Canada');
  assert.equal(prev.status, 200);
  assert.equal(prev.body.total, 4);
  const desc = prev.body.groups.find((g) => g.kind === 'description');
  const dates = prev.body.groups.find((g) => g.kind === 'date');
  assert.equal(desc.count, 3);
  assert.equal(dates.count, 1);
  assert.equal(desc.rows[0].new_value, 'Photo 0 from Canada.');
  assert.equal(dates.rows[0].new_value, 'Canada trip');

  const rows = [...desc.rows, ...dates.rows]
    .map((r) => ({ id: r.id, kind: r.kind, value: r.value }));
  const res = await a.post('/api/admin/corrections/apply')
    .set('X-CSRF-Token', a.csrf)
    .send({ q: 'Canaca', replace: 'Canada', rows });
  assert.equal(res.status, 200);
  assert.equal(res.body.changed, 4);
  assert.deepEqual(res.body.skipped, []);

  for (const id of ids) {
    assert.match((await one('select payload->>\'text\' as t from suggestions where id = $1', [id])).t, /Canada/);
  }
  assert.equal(
    (await one('select payload->>\'evidence\' as e from suggestions where id = $1', [dateId])).e,
    'Canada trip');

  // One audit row per changed row, carrying previous and new, plus a
  // batch summary.
  const { rows: audits } = await pool.query(
    `select previous_value, new_value from audit_log
      where action = 'correction.replace' and new_value->>'batch_id' = $1`,
    [res.body.batch_id]);
  assert.equal(audits.length, 4);
  for (const x of audits) {
    assert.match(x.previous_value.value, /Canaca/);
    assert.match(x.new_value.value, /Canada/);
  }
  const summary = await one(
    `select new_value from audit_log where action = 'correction.batch'
      and new_value->>'batch_id' = $1`, [res.body.batch_id]);
  assert.equal(summary.new_value.changed, 4);
});

test('every row a bulk replace changes is stamped, so the next push keeps it', async () => {
  const w = await seedWorld();
  const a = await agentFor(app, w.admin);
  const ids = await seedTypos(w.photos.clay1, 2);
  const prev = await a.get('/api/admin/corrections/search?q=Canaca&replace=Canada');
  const rows = prev.body.groups[0].rows.map((r) => ({ id: r.id, kind: r.kind, value: r.value }));
  await a.post('/api/admin/corrections/apply').set('X-CSRF-Token', a.csrf)
    .send({ q: 'Canaca', replace: 'Canada', rows });

  for (const id of ids) {
    assert.ok((await one('select edited_on_web_at from suggestions where id = $1', [id])).edited_on_web_at);
  }
});

test('undo restores a batch, and skips a row edited since', async () => {
  const w = await seedWorld();
  const a = await agentFor(app, w.admin);
  const ids = await seedTypos(w.photos.clay1, 3);
  const prev = await a.get('/api/admin/corrections/search?q=Canaca&replace=Canada');
  const rows = prev.body.groups[0].rows.map((r) => ({ id: r.id, kind: r.kind, value: r.value }));
  const applied = await a.post('/api/admin/corrections/apply').set('X-CSRF-Token', a.csrf)
    .send({ q: 'Canaca', replace: 'Canada', rows });

  // Somebody re-words one of them afterwards. Undo must leave that alone
  // and say so, never overwrite newer work.
  await pool.query(
    `update suggestions set payload = jsonb_set(payload, '{text}', '"Rewritten by hand"')
      where id = $1`, [ids[1]]);

  const undone = await a.post('/api/admin/corrections/undo')
    .set('X-CSRF-Token', a.csrf).send({ batch_id: applied.body.batch_id });
  assert.equal(undone.status, 200);
  assert.equal(undone.body.restored, 2);
  assert.equal(undone.body.skipped.length, 1);
  assert.match(undone.body.skipped[0], /changed since the correction/);

  assert.match((await one('select payload->>\'text\' as t from suggestions where id = $1', [ids[0]])).t, /Canaca/);
  assert.equal((await one('select payload->>\'text\' as t from suggestions where id = $1', [ids[1]])).t, 'Rewritten by hand');
  assert.match((await one('select payload->>\'text\' as t from suggestions where id = $1', [ids[2]])).t, /Canaca/);

  // The batch list marks it undone, so it is not offered twice.
  const batches = await a.get('/api/admin/corrections/batches');
  const b = batches.body.items.find((x) => x.batch_id === applied.body.batch_id);
  assert.equal(b.undone, true);
});

test('apply skips a row that changed between the preview and the apply', async () => {
  const w = await seedWorld();
  const a = await agentFor(app, w.admin);
  const ids = await seedTypos(w.photos.clay1, 1);
  const prev = await a.get('/api/admin/corrections/search?q=Canaca&replace=Canada');
  const rows = prev.body.groups[0].rows.map((r) => ({ id: r.id, kind: r.kind, value: r.value }));

  await pool.query(
    `update suggestions set payload = jsonb_set(payload, '{text}', '"Moved on"') where id = $1`,
    [ids[0]]);

  const res = await a.post('/api/admin/corrections/apply').set('X-CSRF-Token', a.csrf)
    .send({ q: 'Canaca', replace: 'Canada', rows });
  assert.equal(res.body.changed, 0);
  assert.match(res.body.skipped[0], /changed since the search/);
  assert.equal((await one('select payload->>\'text\' as t from suggestions where id = $1', [ids[0]])).t, 'Moved on');
});

test('a bulk replace never touches a resolved suggestion', async () => {
  const w = await seedWorld();
  const a = await agentFor(app, w.admin);
  const resolved = Number((await one(
    `insert into suggestions (photo_id, kind, payload, source, status, resolved_at)
     values ($1, 'description', '{"text":"Canaca"}', 'ai', 'rejected', now()) returning id`,
    [w.photos.clay1],
  )).id);
  const prev = await a.get('/api/admin/corrections/search?q=Canaca&replace=Canada');
  const found = (prev.body.groups[0] || { rows: [] }).rows.map((r) => r.id);
  assert.ok(!found.includes(resolved));
});

test('a contributor cannot reach the corrections endpoints or the page', async () => {
  const w = await seedWorld();
  const a = await agentFor(app, w.alice);
  assert.equal((await a.get('/api/admin/corrections/search?q=x')).status, 403);
  assert.equal((await a.post('/api/admin/corrections/apply').set('X-CSRF-Token', a.csrf)
    .send({ q: 'x', rows: [{ id: 1, kind: 'description', value: 'x' }] })).status, 403);
  // Moderators do not get it either: hiding is moderation, rewriting
  // someone's words is an admin act.
  const b = await agentFor(app, w.bob);
  assert.equal((await b.get('/admin/corrections')).status, 403);
});

// ---------------------------------------------------------------------
// 3. The cross-tier last-writer-wins
// ---------------------------------------------------------------------

test('a push does not clobber a web edit of the same suggestion', async () => {
  const w = await seedWorld();
  const a = await agentFor(app, w.admin);

  // The laptop pushes its AI description.
  await push('/sync/suggestions', {
    items: [{
      id: 500, photo_id: w.photos.clay1, kind: 'description',
      payload: JSON.stringify({ text: 'A lake in Canaca.', tags: ['lake'] }),
      status: 'pending', source: 'ai',
    }],
  });
  // An admin corrects it on the web.
  await a.patch('/api/admin/suggestions/500').set('X-CSRF-Token', a.csrf)
    .send({ text: 'A lake in Canada.' });

  // The laptop pushes again, still carrying the old text and no human
  // edit of its own. The web's wording must survive.
  const res = await push('/sync/suggestions', {
    items: [{
      id: 500, photo_id: w.photos.clay1, kind: 'description',
      payload: JSON.stringify({ text: 'A lake in Canaca.', tags: ['lake'] }),
      status: 'pending', source: 'ai', confidence: 0.8,
    }],
  });
  assert.equal(res.status, 200);
  const row = await one('select payload, confidence from suggestions where id = 500');
  assert.equal(row.payload.text, 'A lake in Canada.', 'the web edit must survive the push');
  // Everything NOT guarded still travels, so the row is not frozen.
  assert.equal(Number(row.confidence), 0.8);
});

test('a later desktop edit beats the earlier web edit', async () => {
  const w = await seedWorld();
  const a = await agentFor(app, w.admin);
  await push('/sync/suggestions', {
    items: [{
      id: 501, photo_id: w.photos.clay1, kind: 'description',
      payload: JSON.stringify({ text: 'Canaca' }), status: 'pending', source: 'ai',
    }],
  });
  await a.patch('/api/admin/suggestions/501').set('X-CSRF-Token', a.csrf)
    .send({ text: 'Web wording' });

  // George then corrects the same row on the laptop; the push carries a
  // NEWER human-edit timestamp.
  const res = await push('/sync/suggestions', {
    items: [{
      id: 501, photo_id: w.photos.clay1, kind: 'description',
      payload: JSON.stringify({ text: 'Desktop wording' }), status: 'pending', source: 'ai',
      edited_on_desktop_at: new Date(Date.now() + 60_000).toISOString(),
    }],
  });
  assert.equal(res.status, 200);
  assert.equal((await one('select payload from suggestions where id = 501')).payload.text,
    'Desktop wording');
});

test('a push with no human edit at all still carries content through', async () => {
  // The guard must not freeze rows nobody has edited on the web — that
  // would silently undo Phase 9 fix-up 2.
  const w = await seedWorld();
  await push('/sync/albums', {
    items: [{ id: 700, name: 'Summer 1992 - Canaca', source: 'import' }],
  });
  await push('/sync/albums', {
    items: [{ id: 700, name: 'Summer 1992 - Canada', source: 'import' }],
  });
  assert.equal((await one('select name from albums where id = 700')).name,
    'Summer 1992 - Canada');
});

test('people: an admin web edit survives the push, and the laptop can pull it', async () => {
  const w = await seedWorld();
  const a = await agentFor(app, w.admin);
  await push('/sync/people', {
    items: [{ id: 300, given_name: 'Ann', surname: 'Canaca' }],
  });

  const patched = await a.patch('/api/people/300').set('X-CSRF-Token', a.csrf)
    .send({ surname: 'Canada' });
  assert.equal(patched.status, 200);
  assert.equal(patched.body.changed, true);

  // The laptop re-sends the old surname.
  await push('/sync/people', {
    items: [{ id: 300, given_name: 'Ann', surname: 'Canaca' }],
  });
  const row = await one('select surname, display_name, edited_on_web_at from people where id = 300');
  assert.equal(row.surname, 'Canada');
  assert.match(row.display_name, /Canada/, 'the generated display_name follows');

  // And /sync/pull/web_edits offers it to the laptop.
  const pulled = await pull('/sync/pull/web_edits');
  assert.equal(pulled.status, 200);
  const person = pulled.body.people.find((p) => Number(p.id) === 300);
  assert.ok(person, 'the web edit must be offered to the laptop');
  assert.equal(person.surname, 'Canada');
  assert.ok(person.edited_on_web_at);
});

test('an is_deleted push still lands on a person whose name the web edited', async () => {
  // The guard covers the WORDING. A soft-delete on the laptop is a
  // decision and stays desktop-authoritative.
  const w = await seedWorld();
  const a = await agentFor(app, w.admin);
  await push('/sync/people', { items: [{ id: 301, given_name: 'Ann', surname: 'Canaca' }] });
  await a.patch('/api/people/301').set('X-CSRF-Token', a.csrf).send({ surname: 'Canada' });
  await push('/sync/people', {
    items: [{ id: 301, given_name: 'Ann', surname: 'Canaca', is_deleted: true }],
  });
  const row = await one('select surname, is_deleted from people where id = 301');
  assert.equal(row.surname, 'Canada');
  assert.equal(row.is_deleted, true);
});

test('a person merge still re-parents a variant the web renamed', async () => {
  // person_id is deliberately NOT guarded: a merge is a desktop decision
  // about who owns the name, not a wording. This is the Phase 9 fix-up 2
  // bug; Phase 15's guards must not reintroduce it.
  const w = await seedWorld();
  const a = await agentFor(app, w.admin);
  await push('/sync/people', {
    items: [{ id: 310, given_name: 'Margaret', surname: 'Clay' },
            { id: 311, given_name: 'Peggy', surname: 'Clay' }],
  });
  await push('/sync/person_name_variants', {
    items: [{ id: 320, person_id: 311, variant: 'Peg', kind: 'nickname' }],
  });
  await a.patch('/api/people/311/variants/320').set('X-CSRF-Token', a.csrf)
    .send({ variant: 'Peggie' });

  await push('/sync/person_name_variants', {
    items: [{ id: 320, person_id: 310, variant: 'Peg', kind: 'nickname' }],
  });
  const row = await one('select person_id, variant from person_name_variants where id = 320');
  assert.equal(Number(row.person_id), 310, 'the merge must still re-parent it');
  assert.equal(row.variant, 'Peggie', 'the web wording must survive');
});

test('pull/web_edits pages past rows that share one timestamp', async () => {
  // A bulk web replace stamps every row inside one transaction, so they
  // share `edited_on_web_at` to the microsecond. A timestamp-only cursor
  // could never get past the tie; the composite `(edited_on_web_at, id)`
  // always advances.
  const w = await seedWorld();
  const stamp = new Date().toISOString();
  for (let id = 800; id < 806; id += 1) {
    await pool.query(
      `insert into albums (id, name, source, edited_on_web_at)
       values ($1, $2, 'import', $3::timestamptz)`,
      [id, `Album ${id}`, stamp]);
  }
  const first = await pull(`/sync/pull/web_edits?cursors=${encodeURIComponent(JSON.stringify({}))}`);
  assert.equal(first.status, 200);
  assert.equal(first.body.albums.length, 6);
  const cursor = first.body.cursors.albums;
  assert.match(cursor, /\|805$/, 'the cursor must carry the last id, not just the time');

  const second = await pull(
    `/sync/pull/web_edits?cursors=${encodeURIComponent(JSON.stringify({ albums: cursor }))}`);
  assert.equal(second.body.albums.length, 0, 'the second page must be past the tie');
});

test('pull/web_edits never offers a web-born row', async () => {
  // Those are web-authoritative outright and travel whole through
  // /sync/pull/web_origin.
  await seedWorld();
  const { WEB_ID_FLOOR } = require('../services/id-floor');
  await pool.query(
    `insert into people (id, given_name, edited_on_web_at) values ($1, 'WebBorn', now())`,
    [WEB_ID_FLOOR + 5]);
  const res = await pull('/sync/pull/web_edits');
  assert.ok(!res.body.people.some((p) => Number(p.id) >= WEB_ID_FLOOR));
});

test('pull/web_edits refuses a malformed cursor rather than guessing', async () => {
  await seedWorld();
  assert.equal((await pull('/sync/pull/web_edits?cursors=not-json')).status, 400);
  assert.equal((await pull(
    `/sync/pull/web_edits?cursors=${encodeURIComponent(JSON.stringify({ albums: 'nonsense|1' }))}`,
  )).status, 400);
});

// ---------------------------------------------------------------------
// 4. place_aliases — the route that did not exist
// ---------------------------------------------------------------------

test('place aliases sync, and a removal travels as an empty set', async () => {
  await seedWorld();
  await push('/sync/places', { items: [{ id: 400, name: 'Banff' }] });

  let res = await push('/sync/place_aliases', {
    items: [{
      place_id: 400,
      aliases: [{ alias: 'Banff Springs', kind: 'alias' }, { alias: 'Banff AB', kind: 'alias' }],
    }],
  });
  assert.equal(res.status, 200);
  assert.equal(res.body.aliases, 2);
  let { rows } = await pool.query(
    'select alias from place_aliases where place_id = 400 order by alias');
  assert.deepEqual(rows.map((r) => r.alias), ['Banff AB', 'Banff Springs']);

  // A correction on the laptop: one alias re-spelled, one removed.
  res = await push('/sync/place_aliases', {
    items: [{ place_id: 400, aliases: [{ alias: 'Banff Springs Hotel', kind: 'alias' }] }],
  });
  assert.equal(res.status, 200);
  assert.equal(res.body.removed, 2);
  ({ rows } = await pool.query('select alias from place_aliases where place_id = 400'));
  assert.deepEqual(rows.map((r) => r.alias), ['Banff Springs Hotel']);

  // The last alias removed: the empty set has to clear the table, which
  // is the state a soft-delete column would otherwise have carried.
  res = await push('/sync/place_aliases', { items: [{ place_id: 400, aliases: [] }] });
  assert.equal(res.status, 200);
  ({ rows } = await pool.query('select alias from place_aliases where place_id = 400'));
  assert.equal(rows.length, 0);
});

test('place aliases for an unknown or web-origin place are ignored, not an error', async () => {
  await seedWorld();
  const { WEB_ID_FLOOR } = require('../services/id-floor');
  const res = await push('/sync/place_aliases', {
    items: [
      { place_id: 999_999, aliases: [{ alias: 'Nowhere' }] },
      { place_id: WEB_ID_FLOOR + 1, aliases: [{ alias: 'Web place' }] },
    ],
  });
  assert.equal(res.status, 200);
  assert.equal(res.body.upserted, 0);
});

test('place aliases need a service token', async () => {
  await seedWorld();
  assert.equal((await request(app).post('/sync/place_aliases').send({ items: [] })).status, 401);
});

// ---------------------------------------------------------------------
// 5. album_photos / photo_places soft-delete
// ---------------------------------------------------------------------

test('a desktop removal from an album reaches the web and hides the photo there', async () => {
  // Both join tables were insert-or-update-only and the push only
  // upserts, so a removal never arrived and the site kept showing the
  // photo in the album.
  const w = await seedWorld();
  const a = await agentFor(app, w.admin);

  let page = await a.get(`/albums/${w.album}`);
  assert.equal(page.status, 200);
  assert.ok(page.text.includes(`/photos/${w.photos.clay1}`));

  const res = await push('/sync/album_photos', {
    items: [{
      album_id: w.album, photo_id: w.photos.clay1, position: 2,
      is_deleted: true, deleted_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    }],
  });
  assert.equal(res.status, 200);
  assert.equal((await one(
    'select is_deleted from album_photos where album_id = $1 and photo_id = $2',
    [w.album, w.photos.clay1])).is_deleted, true);

  page = await a.get(`/albums/${w.album}`);
  assert.ok(!page.text.includes(`/photos/${w.photos.clay1}`),
    'the removed photo must be gone from the album page');
  // The photo itself is untouched — this removed a membership, not a photo.
  assert.equal((await a.get(`/photos/${w.photos.clay1}`)).status, 200);
});

test('album_photos is last-writer-wins by updated_at', async () => {
  const w = await seedWorld();
  const older = new Date(Date.now() - 3600_000).toISOString();
  await pool.query(
    `update album_photos set is_deleted = true, updated_at = now()
      where album_id = $1 and photo_id = $2`, [w.album, w.photos.clay1]);

  // A stale push (older timestamp) must not resurrect the membership.
  await push('/sync/album_photos', {
    items: [{
      album_id: w.album, photo_id: w.photos.clay1, position: 2,
      is_deleted: false, updated_at: older,
    }],
  });
  assert.equal((await one(
    'select is_deleted from album_photos where album_id = $1 and photo_id = $2',
    [w.album, w.photos.clay1])).is_deleted, true);

  // A fresh one does.
  await push('/sync/album_photos', {
    items: [{
      album_id: w.album, photo_id: w.photos.clay1, position: 2,
      is_deleted: false, updated_at: new Date(Date.now() + 1000).toISOString(),
    }],
  });
  assert.equal((await one(
    'select is_deleted from album_photos where album_id = $1 and photo_id = $2',
    [w.album, w.photos.clay1])).is_deleted, false);
});

test('a removed place no longer shows on the photo page or in search', async () => {
  const w = await seedWorld();
  const a = await agentFor(app, w.admin);
  let page = await a.get(`/photos/${w.photos.clay1}`);
  assert.ok(page.text.includes('Toronto'));

  await push('/sync/photo_places', {
    items: [{
      photo_id: w.photos.clay1, place_id: w.place, confirmed: true,
      is_deleted: true, deleted_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    }],
  });
  await pool.query('select refresh_photo_search($1)', [w.photos.clay1]);

  page = await a.get(`/photos/${w.photos.clay1}`);
  assert.ok(!page.text.includes('Toronto'),
    'a removed place must not still be listed on the photo');
  const hits = await a.get('/api/search?q=Toronto');
  assert.equal(hits.status, 200);
  assert.ok(!(hits.body.items || []).some((i) => i.id === w.photos.clay1));
});

test('re-accepting a place suggestion brings a removed membership back', async () => {
  // Otherwise an accepted suggestion would point at a soft-deleted
  // membership that nothing displays.
  const w = await seedWorld();
  const a = await agentFor(app, w.admin);
  await pool.query(
    `update photo_places set is_deleted = true, deleted_at = now(), updated_at = now()
      where photo_id = $1 and place_id = $2`, [w.photos.clay1, w.place]);
  const sug = Number((await one(
    `insert into suggestions (photo_id, user_id, kind, payload, source, status)
     values ($1, $2, 'place', $3::jsonb, 'human', 'pending') returning id`,
    [w.photos.clay1, w.alice.id, JSON.stringify({ place_id: w.place })],
  )).id);

  const res = await a.post(`/api/admin/suggestions/${sug}/accept`)
    .set('X-CSRF-Token', a.csrf).send({});
  assert.equal(res.status, 200);
  assert.equal((await one(
    'select is_deleted from photo_places where photo_id = $1 and place_id = $2',
    [w.photos.clay1, w.place])).is_deleted, false);
});

// ---------------------------------------------------------------------
// 6. Comments — the day-one exception, closed
// ---------------------------------------------------------------------

async function comment(photoId, userId, body = 'As I remember it.') {
  return Number((await one(
    `insert into comments (photo_id, user_id, body) values ($1, $2, $3) returning id`,
    [photoId, userId, body],
  )).id);
}

test('the author may edit their own comment, and it says it was edited', async () => {
  const w = await seedWorld();
  const a = await agentFor(app, w.alice);
  const id = await comment(w.photos.clay1, w.alice.id, 'Taken in Canaca.');

  const res = await a.patch(`/api/comments/${id}`)
    .set('X-CSRF-Token', a.csrf).send({ body: 'Taken in Canada.' });
  assert.equal(res.status, 200);
  const row = await one('select body, edited_at from comments where id = $1', [id]);
  assert.equal(row.body, 'Taken in Canada.');
  assert.ok(row.edited_at, 'edited_at must be set so the page can say so');

  const audit = await one(
    `select previous_value, new_value from audit_log
      where action = 'comment.edit' and entity_id = $1`, [id]);
  assert.equal(audit.previous_value.body, 'Taken in Canaca.');
  assert.equal(audit.new_value.body, 'Taken in Canada.');

  // The page renders the marker.
  const page = await a.get(`/photos/${w.photos.clay1}`);
  assert.ok(page.text.includes('(edited)'));
});

test('an admin may edit anyone\'s comment; a moderator may not', async () => {
  const w = await seedWorld();
  const id = await comment(w.photos.clay1, w.alice.id);

  // Bob is a MODERATOR of the Clay group. Hiding is moderation;
  // rewriting another person's words is an admin act (answer 8a).
  const bob = await agentFor(app, w.bob);
  assert.equal((await bob.patch(`/api/comments/${id}`)
    .set('X-CSRF-Token', bob.csrf).send({ body: 'Rewritten' })).status, 403);
  // He can still hide it.
  assert.equal((await bob.post(`/api/comments/${id}/hide`)
    .set('X-CSRF-Token', bob.csrf).send({})).status, 200);

  const admin = await agentFor(app, w.admin);
  const res = await admin.patch(`/api/comments/${id}`)
    .set('X-CSRF-Token', admin.csrf).send({ body: 'Corrected by the admin' });
  assert.equal(res.status, 200);
  assert.equal((await one('select body from comments where id = $1', [id])).body,
    'Corrected by the admin');
  const audit = await one(
    `select new_value from audit_log where action = 'comment.edit' and entity_id = $1`, [id]);
  assert.equal(audit.new_value.as_admin, true);
});

test('a non-member editing a comment gets 404, never 403', async () => {
  // We never confirm that a photo they cannot open exists.
  const w = await seedWorld();
  const id = await comment(w.photos.clay1, w.alice.id);
  const carol = await agentFor(app, w.carol);   // Boots group only
  assert.equal((await carol.patch(`/api/comments/${id}`)
    .set('X-CSRF-Token', carol.csrf).send({ body: 'nope' })).status, 404);
});

test('a comment edit rejects empty and over-long bodies', async () => {
  const w = await seedWorld();
  const a = await agentFor(app, w.alice);
  const id = await comment(w.photos.clay1, w.alice.id);
  assert.equal((await a.patch(`/api/comments/${id}`)
    .set('X-CSRF-Token', a.csrf).send({ body: '   ' })).status, 400);
  assert.equal((await a.patch(`/api/comments/${id}`)
    .set('X-CSRF-Token', a.csrf).send({ body: 'x'.repeat(4001) })).status, 400);
});

// ---------------------------------------------------------------------
// 7. The admin people editor
// ---------------------------------------------------------------------

test('a person PATCH only touches the fields it carries', async () => {
  const w = await seedWorld();
  const a = await agentFor(app, w.admin);
  const id = w.people.peggy.id;

  const res = await a.patch(`/api/people/${id}`)
    .set('X-CSRF-Token', a.csrf).send({ surname: 'Clayton' });
  assert.equal(res.status, 200);
  const row = await one(
    'select given_name, surname, nickname, birth_year from people where id = $1', [id]);
  assert.equal(row.surname, 'Clayton');
  assert.equal(row.given_name, 'Margaret', 'an absent field must be left alone');
  assert.equal(row.nickname, 'Peggy');
  assert.equal(row.birth_year, 1931);
});

test('a person PATCH refuses to leave someone with no name at all', async () => {
  const w = await seedWorld();
  const a = await agentFor(app, w.admin);
  const res = await a.patch(`/api/people/${w.people.peggy.id}`)
    .set('X-CSRF-Token', a.csrf)
    .send({ given_name: '', surname: '', nickname: '' });
  assert.equal(res.status, 400);
});

test('only admins may edit a person or add a variant', async () => {
  const w = await seedWorld();
  const a = await agentFor(app, w.alice);
  assert.equal((await a.patch(`/api/people/${w.people.peggy.id}`)
    .set('X-CSRF-Token', a.csrf).send({ surname: 'Nope' })).status, 403);
  assert.equal((await a.post(`/api/people/${w.people.peggy.id}/variants`)
    .set('X-CSRF-Token', a.csrf).send({ variant: 'Nope' })).status, 403);
});

test('adding a variant makes the person findable by it, and a duplicate is 409', async () => {
  const w = await seedWorld();
  const a = await agentFor(app, w.admin);
  const id = w.people.peggy.id;

  const res = await a.post(`/api/people/${id}/variants`)
    .set('X-CSRF-Token', a.csrf).send({ variant: 'Maggie', kind: 'nickname' });
  assert.equal(res.status, 201);
  assert.equal((await a.post(`/api/people/${id}/variants`)
    .set('X-CSRF-Token', a.csrf).send({ variant: 'maggie' })).status, 409);
  assert.equal((await a.post(`/api/people/${id}/variants`)
    .set('X-CSRF-Token', a.csrf).send({ variant: 'X', kind: 'not-a-kind' })).status, 400);

  // The Phase 11 trigger rebuilds the tokens, so search finds her by it.
  const hits = await a.get('/api/search?q=Maggie');
  assert.equal(hits.status, 200);
  assert.ok(JSON.stringify(hits.body).includes('Margaret'));
});

test('a person edit is audited with previous and new', async () => {
  const w = await seedWorld();
  const a = await agentFor(app, w.admin);
  await a.patch(`/api/people/${w.people.peggy.id}`)
    .set('X-CSRF-Token', a.csrf).send({ surname: 'Clayton' });
  const audit = await one(
    `select actor, previous_value, new_value from audit_log
      where action = 'person.update' and entity_id = $1`, [w.people.peggy.id]);
  assert.equal(audit.actor, w.admin.email);
  assert.equal(audit.previous_value.surname, 'Clay');
  assert.equal(audit.new_value.surname, 'Clayton');
});

test('the admin corrections page renders for an admin', async () => {
  const w = await seedWorld();
  const a = await agentFor(app, w.admin);
  const res = await a.get('/admin/corrections');
  assert.equal(res.status, 200);
  assert.ok(res.text.includes('Corrections'));
  // The scope is on screen, so nobody looks here for an album name.
  assert.ok(/desktop app/i.test(res.text));
});
