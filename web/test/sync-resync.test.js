// Phase 9 fix-up 2 — content changes always re-sync.
//
// The desktop's push is a FULL re-send of every eligible metadata row on
// every run (there is no `updated_at > synced_at` filter, and there cannot
// be one on `photos`: writing `synced_at` trips the `set_updated_at`
// trigger in the same statement). So whether a plain `update` to a pushed
// row's content reaches the VM is decided entirely here, by whether each
// `on conflict (id) do update set` names the column.
//
// It did not, once: `/sync/person_name_variants` never updated
// `person_id`, so a desktop person merge — which re-parents the variant
// onto the winner without changing its id (modes/faces/merge.py) — left
// the web pointing the nickname at the soft-deleted loser.
//
// Two layers of test, because the behavioural ones only cover the columns
// somebody remembered to list:
//   1. a round-trip per table: push a row, edit every content column,
//      push again, assert the web now holds the new values;
//   2. a structural sweep over the route source asserting that every
//      column each upsert INSERTs is also assigned in its DO UPDATE SET,
//      which is what catches the next column added to a push.
const test = require('node:test');
const { after, before, beforeEach } = require('node:test');
const request = require('supertest');
const fs = require('fs');
const path = require('path');
const os = require('os');
const { pool, makeApp, assert } = require('./helpers');

const PHOTO_DIR = fs.mkdtempSync(path.join(os.tmpdir(), 'photoarchive-resync-'));
process.env.PHOTO_DIR = PHOTO_DIR;

let app;
before(() => { app = makeApp(); });
after(async () => {
  await pool.end();
  try { fs.rmSync(PHOTO_DIR, { recursive: true, force: true }); } catch {}
});

beforeEach(async () => {
  await pool.query(`
    truncate table photo_groups, group_members, groups,
                   audit_log, magic_links, access_requests, "session",
                   contribution_files, contributions,
                   suggestions, likes, comments, faces, photo_backs,
                   album_photos, albums, photo_places, places,
                   person_name_variants, relationships, people,
                   photo_masters, photos, users
      restart identity cascade
  `);
});

const TOKEN = () => `Bearer ${process.env.SERVICE_TOKEN}`;

function post(url, body) {
  return request(app).post(url).set('Authorization', TOKEN()).send(body);
}

async function seedPhoto(id = 1) {
  const res = await post('/sync/photos', {
    photos: [{
      id, sha256: `sha${id}`, mime: 'image/jpeg', file_version: 1,
      source_root: 'scans', source_folder: 'Summer 1992 - Canaca',
      source_filename: `p${id}.jpg`, triage_status: 'keep',
    }],
  });
  assert.equal(res.status, 200);
}

async function one(sql, params) {
  const { rows } = await pool.query(sql, params);
  return rows[0];
}

// ---------------------------------------------------------------------
// 1. Round-trips. Each pushes a row, then pushes the SAME id with every
//    content column changed, and asserts the web took the new values.
// ---------------------------------------------------------------------

// The exact shape of George's report: a direct SQL correction of a typo
// in an album name (Canaca -> Canada) must land on the VM at the next push.
test('albums: a renamed album re-syncs on the next push', async () => {
  let res = await post('/sync/albums', {
    items: [{
      id: 6, name: 'Summer 1992 - Canaca', description: 'from the scan folder',
      source: 'import', is_deleted: false,
    }],
  });
  assert.equal(res.status, 200);
  assert.equal((await one('select name from albums where id = 6')).name,
    'Summer 1992 - Canaca');

  // The desktop row is corrected by hand, then pushed again unchanged
  // otherwise. Nothing else about the push is different.
  res = await post('/sync/albums', {
    items: [{
      id: 6, name: 'Summer 1992 - Canada', description: 'corrected by hand',
      source: 'import', is_deleted: true,
    }],
  });
  assert.equal(res.status, 200);
  assert.equal(res.body.upserted, 1);

  const row = await one('select name, description, is_deleted from albums where id = 6');
  assert.equal(row.name, 'Summer 1992 - Canada');
  assert.equal(row.description, 'corrected by hand');
  assert.equal(row.is_deleted, true);
});

test('suggestions: an edited payload re-syncs on the next push', async () => {
  await seedPhoto(1);
  const before = { text: 'Summer 1992 - Canaca', evidence: 'scan folder name' };
  let res = await post('/sync/suggestions', {
    items: [{
      id: 118, photo_id: 1, kind: 'description', payload: JSON.stringify(before),
      status: 'pending', source: 'import', confidence: 0.4,
    }],
  });
  assert.equal(res.status, 200);
  assert.match((await one('select payload::text from suggestions where id = 118')).payload, /Canaca/);

  const after_ = { text: 'Summer 1992 - Canada', evidence: 'scan folder name' };
  res = await post('/sync/suggestions', {
    items: [{
      id: 118, photo_id: 1, kind: 'description', payload: JSON.stringify(after_),
      status: 'pending', source: 'import', confidence: 0.9,
    }],
  });
  assert.equal(res.status, 200);

  const row = await one('select payload, confidence from suggestions where id = 118');
  assert.equal(row.payload.text, 'Summer 1992 - Canada');
  assert.equal(Number(row.confidence), 0.9);
});

// The payload must still re-sync on a row the web has already resolved —
// the status guard protects the DECISION, not the text George corrected.
test('suggestions: an edited payload re-syncs even on a web-resolved row', async () => {
  await seedPhoto(1);
  await post('/sync/suggestions', {
    items: [{
      id: 118, photo_id: 1, kind: 'description',
      payload: JSON.stringify({ text: 'Canaca' }), status: 'pending', source: 'import',
    }],
  });
  // An admin accepts it on the web.
  await pool.query(
    `update suggestions set status = 'accepted', resolved_at = now(),
            resolution_note = 'accepted on the web' where id = 118`,
  );

  // The desktop pushes its still-pending copy with corrected text.
  const res = await post('/sync/suggestions', {
    items: [{
      id: 118, photo_id: 1, kind: 'description',
      payload: JSON.stringify({ text: 'Canada' }), status: 'pending', source: 'import',
    }],
  });
  assert.equal(res.status, 200);

  const row = await one(
    'select payload, status, resolution_note from suggestions where id = 118');
  assert.equal(row.payload.text, 'Canada', 'corrected text must still arrive');
  assert.equal(row.status, 'accepted', 'the web decision must not be re-opened');
  assert.equal(row.resolution_note, 'accepted on the web');
});

// The bug this fix-up actually found.
test('person_name_variants: a merge re-parents the variant on the web too', async () => {
  let res = await post('/sync/people', {
    items: [
      { id: 1, given_name: 'Margaret', surname: 'Clay' },
      { id: 2, given_name: 'Peggy', surname: 'Clay' },
    ],
  });
  assert.equal(res.status, 200);

  res = await post('/sync/person_name_variants', {
    items: [{ id: 10, person_id: 2, variant: 'Peg', kind: 'nickname' }],
  });
  assert.equal(res.status, 200);
  assert.equal((await one('select person_id from person_name_variants where id = 10')).person_id, '2');

  // George merges person 2 into person 1 on the desktop. merge.py moves
  // the variant with an UPDATE, so the id is unchanged and the next push
  // sends the same row with a new person_id.
  res = await post('/sync/person_name_variants', {
    items: [{ id: 10, person_id: 1, variant: 'Peggy', kind: 'nickname' }],
  });
  assert.equal(res.status, 200);

  const row = await one('select person_id, variant from person_name_variants where id = 10');
  assert.equal(row.person_id, '1', 'the variant must follow the merge winner');
  assert.equal(row.variant, 'Peggy');
});

test('people: every edited field re-syncs on the next push', async () => {
  await post('/sync/people', { items: [{ id: 1, given_name: 'Bill', surname: 'Clay' }] });
  const res = await post('/sync/people', {
    items: [{
      id: 1, given_name: 'William', middle_name: 'Henry', surname: 'Clay',
      maiden_name: null, nickname: 'Bill', suffix: 'Jr',
      birth_year: 1931, death_year: 2004, notes: 'corrected', is_deleted: false,
    }],
  });
  assert.equal(res.status, 200);
  const row = await one(
    `select given_name, middle_name, nickname, suffix, birth_year, death_year, notes
       from people where id = 1`);
  assert.deepEqual(
    [row.given_name, row.middle_name, row.nickname, row.suffix,
      row.birth_year, row.death_year, row.notes],
    ['William', 'Henry', 'Bill', 'Jr', 1931, 2004, 'corrected'],
  );
});

test('places: a renamed place re-syncs, and is not blocked by its own row', async () => {
  await post('/sync/places', { items: [{ id: 3, name: 'Canaca Lake' }] });
  const res = await post('/sync/places', {
    items: [{ id: 3, name: 'Canada Lake', latitude: 43.2, longitude: -74.5, notes: 'fixed' }],
  });
  assert.equal(res.status, 200);
  assert.equal(res.body.skipped, 0, 'a rename must never be skipped');
  const row = await one('select name, notes from places where id = 3');
  assert.equal(row.name, 'Canada Lake');
  assert.equal(row.notes, 'fixed');
});

test('photo_backs: a corrected transcription re-syncs', async () => {
  await seedPhoto(1);
  await post('/sync/photo_backs', {
    items: [{ id: 5, photo_id: 1, master_path: 'D:\Scanned Photos\b5.jpg',
               sha256: 'b5', transcribed_text: 'Canaca 1992' }],
  });
  const res = await post('/sync/photo_backs', {
    items: [{
      id: 5, photo_id: 1, master_path: 'D:\Scanned Photos\b5.jpg',
      sha256: 'b5', transcribed_text: 'Canada 1992',
      transcription_confidence: 0.95, transcription_confirmed: true,
    }],
  });
  assert.equal(res.status, 200);
  const row = await one(
    'select transcribed_text, transcription_confirmed from photo_backs where id = 5');
  assert.equal(row.transcribed_text, 'Canada 1992');
  assert.equal(row.transcription_confirmed, true);
});

test('photos: an edited field re-syncs on the next push', async () => {
  await seedPhoto(1);
  const res = await post('/sync/photos', {
    photos: [{
      id: 1, sha256: 'sha1', mime: 'image/jpeg', file_version: 1,
      source_root: 'scans', source_folder: 'Summer 1992 - Canada',
      source_filename: 'p1.jpg', triage_status: 'keep',
      physical_ref_note: 'box 4', description_ai: 'a lake',
    }],
  });
  assert.equal(res.status, 200);
  const row = await one(
    'select source_folder, physical_ref_note, description_ai from photos where id = 1');
  assert.equal(row.source_folder, 'Summer 1992 - Canada');
  assert.equal(row.physical_ref_note, 'box 4');
  assert.equal(row.description_ai, 'a lake');
});

test('faces / relationships: edited fields re-sync', async () => {
  await seedPhoto(1);
  await post('/sync/people', {
    items: [{ id: 1, given_name: 'A' }, { id: 2, given_name: 'B' }],
  });
  await post('/sync/faces', {
    items: [{ id: 7, photo_id: 1, bbox: JSON.stringify({ x: 0, y: 0, w: 10, h: 10 }) }],
  });
  let res = await post('/sync/faces', {
    items: [{
      id: 7, photo_id: 1, person_id: 2,
      bbox: JSON.stringify({ x: 5, y: 5, w: 20, h: 20 }),
      source: 'human', review_status: 'unknown', review_note: 'relabelled',
    }],
  });
  assert.equal(res.status, 200);
  let row = await one('select person_id, bbox, source, review_status from faces where id = 7');
  assert.equal(row.person_id, '2');
  assert.equal(row.bbox.w, 20);
  assert.equal(row.source, 'human');
  assert.equal(row.review_status, 'unknown');

  await post('/sync/relationships', {
    items: [{ id: 4, person_a_id: 1, person_b_id: 2, type: 'sibling', confirmed: false }],
  });
  res = await post('/sync/relationships', {
    items: [{ id: 4, person_a_id: 1, person_b_id: 2, type: 'spouse', confirmed: true }],
  });
  assert.equal(res.status, 200);
  row = await one('select type, confirmed from relationships where id = 4');
  assert.equal(row.type, 'spouse');
  assert.equal(row.confirmed, true);
});

// ---------------------------------------------------------------------
// 2. Structural sweep. The round-trips above only cover columns someone
//    thought to list; this one reads the route source and holds the rule
//    for every column, including the next one added.
// ---------------------------------------------------------------------

// Columns that are creation facts and must NOT be rewritten by a later
// push. Anything else the INSERT names has to appear in DO UPDATE SET.
const CREATION_ONLY = new Set([
  'id',          // the conflict key
  'created_at',  // when the row was born
  'ingested_at', // photo_masters: when the master was first read
  'added_at',    // photo_groups: when the photo joined the group
  'created_by',  // who made it; the maker does not change
]);

// Composite-key tables: the conflict target columns are not updatable.
const CONFLICT_KEYS = new Set(['photo_id', 'place_id', 'album_id', 'group_id']);

// The Phase 15 LWW guards reach the SQL through a keepWebEdits() call
// (written without the dollar-brace here so this file's own comments do
// not trip the leftover-interpolation assert below), so the raw source
// shows a template hole where nine column assignments belong. Expand
// those with the route's own helper before parsing: a sweep reading the
// unexpanded text would both cry wolf on every guarded column and lose
// the ability to spot a real omission inside one.
function expandInterpolations(src) {
  const { keepWebEdits } = require('../routes/sync');
  return src.replace(
    /\$\{keepWebEdits\('(\w+)'\)\}/g,
    (_all, table) => keepWebEdits(table),
  );
}

function parseUpserts(src) {
  const out = [];
  const re = /insert into\s+(\w+)\s*\(([\s\S]*?)\)\s*values/gi;
  let m;
  while ((m = re.exec(src)) !== null) {
    const table = m[1];
    const cols = m[2]
      .split(',')
      .map((c) => c.replace(/--[^\n]*/g, '').trim())
      .filter(Boolean);
    // The DO UPDATE SET clause runs from `do update set` to the end of the
    // SQL template literal (or a trailing `where`/backtick).
    const rest = src.slice(re.lastIndex);
    const doIdx = rest.search(/on conflict\s*\(([^)]*)\)\s*do update set/i);
    if (doIdx === -1) continue; // not an upsert (none today, but be safe)
    const confMatch = /on conflict\s*\(([^)]*)\)\s*do update set/i.exec(rest.slice(doIdx));
    const conflictCols = confMatch[1].split(',').map((c) => c.trim());
    const setStart = doIdx + confMatch[0].length;
    const end = rest.indexOf('`', setStart);
    const setClause = rest.slice(setStart, end === -1 ? undefined : end);
    out.push({ table, cols, conflictCols, setClause });
  }
  return out;
}

test('every column a sync upsert inserts is also assigned on conflict', () => {
  const src = expandInterpolations(fs.readFileSync(
    path.join(__dirname, '..', 'routes', 'sync.js'), 'utf8'));
  // Nothing may be left unexpanded: an interpolation this sweep cannot see
  // through is a blind spot, not a pass.
  assert.ok(!/\$\{keep/.test(src),
    'an unexpanded ${keep…} interpolation would hide columns from this sweep');
  const upserts = parseUpserts(src);
  assert.ok(upserts.length >= 10,
    `expected to parse the sync upserts, found ${upserts.length}`);

  const missing = [];
  for (const u of upserts) {
    // Which identifiers the SET clause assigns, i.e. `name = ...`.
    const assigned = new Set(
      [...u.setClause.matchAll(/(?:^|,|\n)\s*(\w+)\s*=/g)].map((x) => x[1]),
    );
    for (const col of u.cols) {
      if (CREATION_ONLY.has(col)) continue;
      if (u.conflictCols.includes(col)) continue;
      if (u.conflictCols.length > 1 && CONFLICT_KEYS.has(col)) continue;
      if (!assigned.has(col)) missing.push(`${u.table}.${col}`);
    }
  }
  assert.deepEqual(missing, [],
    `these columns are pushed but never updated on an existing row, so an `
    + `edit to them would never reach the VM: ${missing.join(', ')}`);
});
