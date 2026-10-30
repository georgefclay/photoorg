// Phase 13 fix-up 1 — Clear confirmed date on the web admin photo page.
//
// The value goes, not just the flag (Browse's sort and the year/decade
// filters read capture_date alone); the old value comes back as a pending
// date suggestion; one photo.capture_date.unconfirm audit row carries
// previous/new; and /sync/pull/confirmed hands that row to the desktop.
const test = require('node:test');
const { after, before, beforeEach } = require('node:test');
const {
  pool, makeApp, assert, request, resetDb, seedWorld, agentFor,
} = require('./page-helpers');

let app;
before(() => { app = makeApp(); });
after(async () => { await pool.end(); });
beforeEach(resetDb);

const SVC = () => `Bearer ${process.env.SERVICE_TOKEN}`;
const url = (id) => `/api/admin/photos/${id}/capture_date/clear`;

test('admin sees Clear confirmed date on a confirmed photo; a member does not', async () => {
  const w = await seedWorld();
  const admin = await agentFor(app, w.admin);
  const a = await admin.get(`/photos/${w.photos.clay1}`);
  assert.equal(a.status, 200);
  assert.match(a.text, new RegExp(`data-clear-date[^>]*data-url="${url(w.photos.clay1)}"`));
  const alice = await agentFor(app, w.alice);
  const m = await alice.get(`/photos/${w.photos.clay1}`);
  assert.equal(m.status, 200);
  assert.doesNotMatch(m.text, /Clear confirmed date/);
  // An unconfirmed date offers nothing to clear.
  const g = await admin.get(`/photos/${w.photos.clay2}`);
  assert.doesNotMatch(g.text, /Clear confirmed date/);
});

test('clearing removes the value, keeps it as a suggestion, audits, and is pulled', async () => {
  const w = await seedWorld();
  const id = w.photos.clay1;
  const admin = await agentFor(app, w.admin);
  const r = await admin.post(url(id)).set('X-CSRF-Token', admin.csrf).send({});
  assert.equal(r.status, 200);
  assert.equal(r.body.cleared, true);
  assert.deepEqual(r.body.previous, { capture_date: '1962-03-01', precision: 'month' });

  const p = (await pool.query(
    `select capture_date, capture_date_precision, capture_date_confirmed from photos where id = $1`, [id],
  )).rows[0];
  assert.deepEqual(p, { capture_date: null, capture_date_precision: 'unknown', capture_date_confirmed: false });

  const s = (await pool.query(
    `select id, source, status, payload, user_id from suggestions
      where photo_id = $1 and kind = 'date' and payload->>'reason' = 'cleared'`, [id],
  )).rows;
  assert.equal(s.length, 1);
  assert.equal(Number(s[0].id), r.body.suggestion_id);
  assert.equal(s[0].status, 'pending');
  assert.equal(s[0].source, 'import', 'no web accept ever set this date');
  assert.equal(s[0].payload.date, '1962-03-01');
  assert.equal(s[0].payload.precision, 'month');
  assert.equal(s[0].payload.reason, 'cleared');

  const audit = (await pool.query(
    `select actor, previous_value, new_value from audit_log where action = 'photo.capture_date.unconfirm'`,
  )).rows;
  assert.equal(audit.length, 1);
  assert.equal(audit[0].actor, w.admin.email);
  assert.equal(audit[0].previous_value.capture_date, '1962-03-01');
  assert.equal(audit[0].new_value.confirmed, false);

  const pull = await request(app).get('/sync/pull/confirmed').set('Authorization', SVC());
  const row = pull.body.fact_audits.find((x) => x.action === 'photo.capture_date.unconfirm');
  assert.ok(row, 'the clear reaches the desktop');
  assert.equal(Number(row.entity_id), id);

  // Re-acceptable in one click.
  const acc = await admin.post(`/api/admin/suggestions/${s[0].id}/accept`).set('X-CSRF-Token', admin.csrf).send({});
  assert.equal(acc.status, 200);
  const back = (await pool.query(`select capture_date_confirmed from photos where id = $1`, [id])).rows[0];
  assert.equal(back.capture_date_confirmed, true);

  // Clearing a date a web accept set marks the returned suggestion human.
  const again = await admin.post(url(id)).set('X-CSRF-Token', admin.csrf).send({});
  assert.equal(again.status, 200);
  assert.equal(again.body.cleared, true);
  const src = (await pool.query(`select source from suggestions where id = $1`, [again.body.suggestion_id])).rows[0];
  assert.equal(src.source, 'human');
});

test('nothing to clear is a 200 no-op; a contributor is refused; a missing photo is 404', async () => {
  const w = await seedWorld();
  const admin = await agentFor(app, w.admin);
  const r = await admin.post(url(w.photos.clay2)).set('X-CSRF-Token', admin.csrf).send({});
  assert.equal(r.status, 200);
  assert.equal(r.body.cleared, false);
  assert.equal((await pool.query(`select count(*)::int as n from audit_log where action = 'photo.capture_date.unconfirm'`)).rows[0].n, 0);

  const alice = await agentFor(app, w.alice);
  const c = await alice.post(url(w.photos.clay1)).set('X-CSRF-Token', alice.csrf).send({});
  assert.equal(c.status, 403);
  assert.equal((await pool.query(`select capture_date_confirmed from photos where id = $1`, [w.photos.clay1])).rows[0].capture_date_confirmed, true);

  const missing = await admin.post(url(999999)).set('X-CSRF-Token', admin.csrf).send({});
  assert.equal(missing.status, 404);
});
