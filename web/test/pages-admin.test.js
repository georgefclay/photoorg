// Admin area pages (/admin/*): access rules, empty renders, the suggestions
// queue, the 409 → force flow, unfiled count preview, rescan grouping,
// moderator scoping for contributions and groups.
const test = require('node:test');
const { after, before, beforeEach } = require('node:test');
const {
  pool, makeApp, assert, request, resetDb, seedWorld, agentFor, insertUser, insertPhoto, addToGroup,
} = require('./page-helpers');

let app;
before(() => { app = makeApp(); });
after(async () => { await pool.end(); });
beforeEach(resetDb);

const ADMIN_ONLY = [
  '/admin/suggestions', '/admin/suggestions?source=ai&kind=date', '/admin/disputes',
  '/admin/unfiled', '/admin/unfiled?decade=1960', '/admin/rescan', '/admin/report', '/admin/report?month=2026-01',
  '/admin/audit', '/admin/audit?action=auth.*', '/admin/access', '/admin/users',
];
const MODERATOR_OK = ['/admin', '/admin/contributions', '/admin/contributions?status=all', '/admin/groups'];

async function insertContribution(user, groupIds, files) {
  const cid = Number((await pool.query(
    `insert into contributions (user_id, note, group_ids, finished_at) values ($1, 'From the attic', $2, now()) returning id`,
    [user.id, groupIds],
  )).rows[0].id);
  const ids = [];
  for (const f of files) {
    ids.push(Number((await pool.query(
      `insert into contribution_files (contribution_id, original_filename, stored_path, sha256, size, mime,
                                       duplicate_of_photo_id, duplicate_distance)
       values ($1, $2, 'uploads/' || $3 || '.jpg', $3, 1234, 'image/jpeg', $4, $5) returning id`,
      [cid, f.name, f.sha, f.dup || null, f.dist != null ? f.dist : null],
    )).rows[0].id));
  }
  return { cid, ids };
}

test('anonymous is redirected to /login from every admin page', async () => {
  for (const path of [...MODERATOR_OK, ...ADMIN_ONLY, '/admin/groups/1']) {
    const r = await request(app).get(path);
    assert.equal(r.status, 302, path);
    assert.equal(r.headers.location, '/login', path);
  }
});

test('admin gets 200 everywhere; contributor 403; moderator only the scoped pages', async () => {
  const w = await seedWorld();
  const admin = await agentFor(app, w.admin);
  const alice = await agentFor(app, w.alice);
  const bob = await agentFor(app, w.bob);

  for (const path of [...MODERATOR_OK, ...ADMIN_ONLY, `/admin/groups/${w.clay}`, `/admin/groups/${w.boots}`]) {
    const r = await admin.get(path);
    assert.equal(r.status, 200, `admin ${path}`);
    assert.match(r.text, /<main/, `layout wraps ${path}`);
  }
  for (const path of [...MODERATOR_OK, ...ADMIN_ONLY, `/admin/groups/${w.clay}`]) {
    const r = await alice.get(path);
    assert.equal(r.status, 403, `contributor ${path}`);
  }
  for (const path of [...MODERATOR_OK, `/admin/groups/${w.clay}`]) {
    assert.equal((await bob.get(path)).status, 200, `moderator ${path}`);
  }
  for (const path of ADMIN_ONLY) {
    assert.equal((await bob.get(path)).status, 403, `moderator admin-only ${path}`);
  }
  assert.equal((await bob.get(`/admin/groups/${w.boots}`)).status, 404, 'moderator of another group');
  assert.equal((await admin.get('/admin/groups/999999')).status, 404, 'missing group');
});

test('every page renders with an empty database (only an admin)', async () => {
  const me = await insertUser({ email: 'solo@example.com', role: 'admin', displayName: 'Solo' });
  const admin = await agentFor(app, me);
  const expectations = {
    '/admin': /To review/,
    '/admin/suggestions': /All caught up/,
    '/admin/disputes': /No disputes/,
    '/admin/contributions': /Nothing to review/,
    '/admin/groups': /No groups yet/,
    '/admin/unfiled': /Everything is filed/,
    '/admin/rescan': /Nothing to rescan/,
    '/admin/report': /A quiet month|Sign-ins/,
    '/admin/audit': /Audit log/,
    '/admin/access': /No pending requests/,
    '/admin/users': /solo@example\.com/,
    '/admin/suggestions?cursor=5': /No more suggestions/,
  };
  for (const [path, re] of Object.entries(expectations)) {
    const r = await admin.get(path);
    assert.equal(r.status, 200, path);
    assert.match(r.text, re, path);
  }
});

test('dashboard: admin counts; moderator sees only contributions and their groups', async () => {
  const w = await seedWorld();
  await insertContribution(w.alice, [w.clay], [{ name: 'a.jpg', sha: 'c1' }, { name: 'b.jpg', sha: 'c2' }]);
  await insertContribution(w.carol, [w.boots], [{ name: 'c.jpg', sha: 'c3' }]);
  await pool.query(`update photos set rescan_wanted = true where id = $1`, [w.photos.clay1]);

  const admin = await agentFor(app, w.admin);
  const a = await admin.get('/admin');
  assert.match(a.text, /href="\/admin\/suggestions"/);
  assert.match(a.text, /href="\/admin\/unfiled"/);
  assert.match(a.text, /<span class="stat-n">3<\/span>\s*<span class="stat-label">Uploads to review/);
  const counts = await admin.get('/api/admin/counts');
  assert.equal(counts.status, 200);
  assert.equal(counts.body.suggestions.total, 1);
  assert.equal(counts.body.unfiled, 1);
  assert.equal(counts.body.rescan, 1);
  assert.equal(counts.body.contributions.files, 3);

  const bob = await agentFor(app, w.bob);
  const b = await bob.get('/admin');
  assert.match(b.text, /<span class="stat-n">2<\/span>\s*<span class="stat-label">Uploads to review/);
  assert.match(b.text, /Clay Family/);
  assert.doesNotMatch(b.text, /Boots Family/);
  assert.doesNotMatch(b.text, /href="\/admin\/suggestions"/);
  assert.doesNotMatch(b.text, /href="\/admin\/unfiled"/);
  const bg = await bob.get('/admin/groups');
  assert.match(bg.text, new RegExp(`href="/admin/groups/${w.clay}"`));
  assert.doesNotMatch(bg.text, new RegExp(`href="/admin/groups/${w.boots}"`));
});

test('suggestions page shows alice’s pending date suggestion with evidence and actions', async () => {
  const w = await seedWorld();
  await pool.query(
    `insert into suggestions (photo_id, kind, payload, confidence, source, model, status)
     values ($1, 'description', '{"text":"two children on a porch"}', 0.8, 'ai', 'qwen2.5-vl', 'pending')`,
    [w.photos.clay2],
  );
  const admin = await agentFor(app, w.admin);
  const r = await admin.get('/admin/suggestions');
  assert.equal(r.status, 200);
  assert.match(r.text, new RegExp(`data-suggestion="${w.suggestion}"`));
  assert.match(r.text, /April 1962/, 'proposed date label');
  assert.match(r.text, /March 1962 \(confirmed\)/, 'current confirmed value');
  assert.match(r.text, /Alice/);
  assert.match(r.text, new RegExp(`/media/thumbs/${w.photos.clay1}`));
  assert.match(r.text, /data-act="accept"/);
  assert.match(r.text, /data-act="reject"/);
  assert.doesNotMatch(r.text, /two children on a porch/, 'human tab by default');

  const ai = await admin.get('/admin/suggestions?source=ai');
  assert.match(ai.text, /two children on a porch/);
  assert.match(ai.text, /qwen2\.5-vl/);
  assert.match(ai.text, /80% sure/);
  assert.doesNotMatch(ai.text, new RegExp(`data-suggestion="${w.suggestion}"`));

  const other = await admin.get('/admin/suggestions?source=all&kind=place');
  assert.match(other.text, /All caught up/);
  const bogus = await admin.get('/admin/suggestions?kind=nonsense&source=zzz');
  assert.equal(bogus.status, 200, 'unknown kind/source fall back');
});

test('suggestions queue paginates with a keyset cursor', async () => {
  const w = await seedWorld();
  for (let i = 0; i < 45; i++) {
    await pool.query(
      `insert into suggestions (photo_id, kind, payload, source, status) values ($1, 'classification', '{"label":"document"}', 'ai', 'pending')`,
      [w.photos.clay2],
    );
  }
  const admin = await agentFor(app, w.admin);
  const p1 = await admin.get('/admin/suggestions?source=ai');
  const ids1 = [...p1.text.matchAll(/data-suggestion="(\d+)"/g)].map((m) => Number(m[1]));
  assert.equal(ids1.length, 40);
  const more = /href="(\/admin\/suggestions\?[^"]*cursor=\d+[^"]*)"/.exec(p1.text);
  assert.ok(more, 'Older link');
  const p2 = await admin.get(more[1].replace(/&amp;/g, '&'));
  const ids2 = [...p2.text.matchAll(/data-suggestion="(\d+)"/g)].map((m) => Number(m[1]));
  assert.equal(ids2.length, 5);
  assert.ok(Math.min(...ids1) > Math.max(...ids2), 'strictly older');
});

test('accept → 409 with current/proposed → force replaces (API the page uses)', async () => {
  const w = await seedWorld();
  const admin = await agentFor(app, w.admin);
  const conflict = await admin.post(`/api/admin/suggestions/${w.suggestion}/accept`).set('X-CSRF-Token', admin.csrf).send({});
  assert.equal(conflict.status, 409);
  assert.ok(conflict.body.current && conflict.body.proposed);
  assert.equal(conflict.body.proposed.capture_date, '1962-04-01');
  const forced = await admin.post(`/api/admin/suggestions/${w.suggestion}/accept`).set('X-CSRF-Token', admin.csrf).send({ force: true });
  assert.equal(forced.status, 200);
  const { rows } = await pool.query(`select to_char(capture_date, 'YYYY-MM-DD') as d from photos where id = $1`, [w.photos.clay1]);
  assert.equal(rows[0].d, '1962-04-01');
  const page = await admin.get('/admin/suggestions');
  assert.doesNotMatch(page.text, new RegExp(`data-suggestion="${w.suggestion}"`), 'resolved leaves the queue');
});

test('disputes page lists disputed faces with keep/unassign/reassign', async () => {
  const w = await seedWorld();
  await pool.query(`update faces set is_disputed = true, disputed_by = $1, dispute_note = 'That is Aunt Jo' where id = $2`,
    [w.alice.id, w.faces.peggy]);
  const admin = await agentFor(app, w.admin);
  const r = await admin.get('/admin/disputes');
  assert.match(r.text, new RegExp(`data-dispute="${w.faces.peggy}"`));
  assert.match(r.text, /That is Aunt Jo/);
  assert.match(r.text, new RegExp(`/media/faces/${w.faces.peggy}`));
  assert.match(r.text, /data-act="reassign"/);
});

test('contributions: dup badges, admin batch buttons, moderator scoped without batch actions', async () => {
  const w = await seedWorld();
  const exactSha = (await pool.query(`select sha256 from photos where id = $1`, [w.photos.clay1])).rows[0].sha256;
  const clayC = await insertContribution(w.alice, [w.clay], [
    { name: 'exact.jpg', sha: exactSha, dup: w.photos.clay1, dist: 0 },
    { name: 'near.jpg', sha: 'near-sha', dup: w.photos.clay2, dist: 6 },
    { name: 'new.jpg', sha: 'new-sha' },
  ]);
  const bootsC = await insertContribution(w.carol, [w.boots], [{ name: 'boots.jpg', sha: 'boots-sha' }]);

  const admin = await agentFor(app, w.admin);
  const a = await admin.get('/admin/contributions');
  assert.match(a.text, new RegExp(`data-contribution="${clayC.cid}"`));
  assert.match(a.text, new RegExp(`data-contribution="${bootsC.cid}"`));
  assert.match(a.text, new RegExp(`Exact copy of #${w.photos.clay1}`));
  assert.match(a.text, new RegExp(`Looks like #${w.photos.clay2} \\(distance 6\\)`));
  assert.match(a.text, /data-act="approve-all"/);
  assert.match(a.text, /From the attic/);

  const bob = await agentFor(app, w.bob);
  const b = await bob.get('/admin/contributions');
  assert.match(b.text, new RegExp(`data-contribution="${clayC.cid}"`));
  assert.doesNotMatch(b.text, new RegExp(`data-contribution="${bootsC.cid}"`));
  assert.doesNotMatch(b.text, /data-act="approve-all"/);

  // Moderator API calls reach the contributions router (not the admin gate).
  const list = await bob.get('/api/admin/contributions');
  assert.equal(list.status, 200);
  assert.deepEqual(list.body.items.map((c) => c.id), [clayC.cid]);
  const ok = await bob.post(`/api/admin/contributions/${clayC.cid}/files/${clayC.ids[2]}/approve`).set('X-CSRF-Token', bob.csrf).send({});
  assert.equal(ok.status, 200);
  const no = await bob.post(`/api/admin/contributions/${bootsC.cid}/files/${bootsC.ids[0]}/approve`).set('X-CSRF-Token', bob.csrf).send({});
  assert.equal(no.status, 403);
  const batch = await bob.post(`/api/admin/contributions/${clayC.cid}/approve-all`).set('X-CSRF-Token', bob.csrf).send({});
  assert.equal(batch.status, 403);
  assert.equal((await bob.get('/api/admin/suggestions')).status, 403, 'rest of /api/admin stays admin-only');
  assert.equal((await (await agentFor(app, w.alice)).get('/api/admin/contributions')).status, 200, 'contributor list is empty, not an error');

  const approved = await admin.get('/admin/contributions?status=all');
  assert.match(approved.text, /Clay Family/);
});

test('groups: detail page shows members and role controls for admin only; user lookup is moderator-scoped', async () => {
  const w = await seedWorld();
  const admin = await agentFor(app, w.admin);
  const a = await admin.get(`/admin/groups/${w.clay}`);
  assert.match(a.text, /alice@example\.com/);
  assert.match(a.text, /data-role-select/);
  assert.match(a.text, /data-act="delete-group"/);

  const bob = await agentFor(app, w.bob);
  const b = await bob.get(`/admin/groups/${w.clay}`);
  assert.match(b.text, /alice@example\.com/);
  assert.doesNotMatch(b.text, /data-role-select/);
  assert.doesNotMatch(b.text, /data-act="delete-group"/);
  assert.match(b.text, /data-act="remove-member"/);

  const look = await bob.get(`/api/groups/${w.clay}/user-lookup?q=car`);
  assert.equal(look.status, 200);
  assert.deepEqual(look.body.items.map((u) => u.email), ['carol@example.com']);
  assert.equal(look.body.items[0].is_member, false);
  const short = await bob.get(`/api/groups/${w.clay}/user-lookup?q=c`);
  assert.deepEqual(short.body.items, []);
  assert.equal((await bob.get(`/api/groups/${w.boots}/user-lookup?q=car`)).status, 403);
  assert.equal((await (await agentFor(app, w.alice)).get(`/api/groups/${w.clay}/user-lookup?q=car`)).status, 403);
});

test('unfiled: page lists unfiled photos; preview counts match; apply files them', async () => {
  const w = await seedWorld();
  const extra = [];
  for (let i = 0; i < 3; i++) extra.push(await insertPhoto({ scan_batch: 'Batch 00077', capture_date: '1965-06-01', capture_date_precision: 'year' }));
  const filedSameBatch = await insertPhoto({ scan_batch: 'Batch 00077' });
  await addToGroup(filedSameBatch, w.boots);

  const admin = await agentFor(app, w.admin);
  const page = await admin.get('/admin/unfiled');
  assert.match(page.text, /4 unfiled photos/, 'seed unfiled + 3 new; private and filed excluded');
  const tiles = [...page.text.matchAll(/class="tile" href="\/photos\/(\d+)"/g)].map((m) => Number(m[1]));
  assert.deepEqual(tiles.sort((x, y) => x - y), [w.photos.unfiled, ...extra].sort((x, y) => x - y));
  const filtered = await admin.get('/admin/unfiled?scan_batch=Batch%2000077');
  assert.match(filtered.text, /3 unfiled photos/);
  assert.match(filtered.text, /Batch 00077/, 'batch offered in the datalist');

  const post = (url, body) => admin.post(url).set('X-CSRF-Token', admin.csrf).send(body);
  let p = await post('/api/admin/photos/bulk-assign-groups/preview', { scan_batch: 'Batch 00077', only_unfiled: true, add: [w.clay] });
  assert.equal(p.status, 200);
  assert.equal(p.body.photos, 3);
  assert.equal(p.body.new_rows, 3);
  assert.equal(p.body.over_cap, false);
  p = await post('/api/admin/photos/bulk-assign-groups/preview', { scan_batch: 'Batch 00077' });
  assert.equal(p.body.photos, 4, 'without only_unfiled the filed photo counts too (existing API semantics)');
  p = await post('/api/admin/photos/bulk-assign-groups/preview', { decade: 1960, only_unfiled: true });
  assert.equal(p.body.photos, 3);
  p = await post('/api/admin/photos/bulk-assign-groups/preview', { ids: [extra[0], extra[1]], add: [w.clay] });
  assert.equal(p.body.photos, 2);

  const applied = await post('/api/admin/photos/bulk-assign-groups', { scan_batch: 'Batch 00077', only_unfiled: true, add: [w.clay] });
  assert.equal(applied.status, 200);
  assert.equal(applied.body.photos, 3);
  const live = await pool.query(
    `select count(*)::int as n from photo_groups where group_id = $1 and photo_id = any($2::bigint[]) and is_deleted = false`,
    [w.clay, extra],
  );
  assert.equal(live.rows[0].n, 3);
  const stillFiled = await pool.query(`select count(*)::int as n from photo_groups where photo_id = $1 and group_id = $2`, [filedSameBatch, w.clay]);
  assert.equal(stillFiled.rows[0].n, 0, 'already-filed photo untouched');
  const after1 = await admin.get('/admin/unfiled');
  assert.match(after1.text, /1 unfiled photo\b/);

  const bob = await agentFor(app, w.bob);
  const forbidden = await bob.post('/api/admin/photos/bulk-assign-groups/preview').set('X-CSRF-Token', bob.csrf).send({});
  assert.equal(forbidden.status, 403);
});

test('rescan page groups by batch in sequence order', async () => {
  const w = await seedWorld();
  const a2 = await insertPhoto({ scan_batch: 'Batch 00003', scan_sequence: 2, source_filename: 'scan0002.tif', rescan_wanted: true });
  const a1 = await insertPhoto({ scan_batch: 'Batch 00003', scan_sequence: 1, source_filename: 'scan0001.tif', rescan_wanted: true, physical_ref_note: 'torn corner' });
  const b9 = await insertPhoto({ scan_batch: 'Batch 00009', scan_sequence: 9, source_filename: 'scan0009.tif', rescan_wanted: true });
  await insertPhoto({ scan_batch: 'Batch 00009', scan_sequence: 10, source_filename: 'nope.tif', rescan_wanted: false });
  const admin = await agentFor(app, w.admin);
  const r = await admin.get('/admin/rescan');
  assert.equal(r.status, 200);
  const t = r.text;
  const i3 = t.indexOf('<h2>Batch 00003');
  const i9 = t.indexOf('<h2>Batch 00009');
  assert.ok(i3 > 0 && i9 > i3, 'batches in order');
  assert.ok(t.indexOf('scan0001.tif') > i3 && t.indexOf('scan0001.tif') < t.indexOf('scan0002.tif'), 'sequence order');
  assert.ok(t.indexOf('scan0009.tif') > i9);
  assert.match(t, /torn corner/);
  assert.doesNotMatch(t, /nope\.tif/);
  assert.match(t, /3 prints to rescan, in 2 envelopes/);
  assert.doesNotMatch(t, /\/media\/thumbs\//, 'thumbnails off by default');
  const withThumbs = await admin.get('/admin/rescan?thumbs=1');
  assert.match(withThumbs.text, new RegExp(`/media/thumbs/${a1}`));
  void a2; void b9;
  const legacy = await admin.get('/admin/rescan-list');
  assert.equal(legacy.status, 301);
});

test('report month picker and audit filters with keyset Older', async () => {
  const w = await seedWorld();
  const admin = await agentFor(app, w.admin);
  const now = (await pool.query(`select to_char(now(), 'YYYY-MM') as m`)).rows[0].m;
  const rep = await admin.get('/admin/report');
  assert.match(rep.text, new RegExp(`value="${now}"`));
  assert.match(rep.text, /Admin/, 'admin signed in this month → listed');
  const bad = await admin.get('/admin/report?month=2026-13');
  assert.match(bad.text, new RegExp(`value="${now}"`), 'bad month falls back');

  for (let i = 0; i < 55; i++) {
    await pool.query(
      `insert into audit_log (actor, action, entity_type, entity_id, new_value) values ('desktop', 'triage.decision', 'photo', $1, '{"x":1}')`,
      [w.photos.clay1],
    );
  }
  const p1 = await admin.get('/admin/audit?actor=desktop');
  const rows1 = (p1.text.match(/class="audit-row"/g) || []).length;
  assert.equal(rows1, 50);
  const older = /href="(\/admin\/audit\?[^"]*cursor=\d+)"/.exec(p1.text);
  assert.ok(older);
  const p2 = await admin.get(older[1].replace(/&amp;/g, '&'));
  assert.equal((p2.text.match(/class="audit-row"/g) || []).length, 5);
  const byAction = await admin.get('/admin/audit?action=auth.*');
  assert.doesNotMatch(byAction.text, /triage\.decision/);
  assert.match(byAction.text, /auth\.login/);
  const api = await admin.get('/api/admin/audit?actor=desktop&limit=5');
  assert.equal(api.body.items.length, 5);
  assert.ok(api.body.items.every((x) => x.actor === 'desktop'));
  assert.match(p1.text, new RegExp(`href="/photos/${w.photos.clay1}"`), 'entity link');
});

test('access and users keep their form actions and CSRF fields', async () => {
  const w = await seedWorld();
  await pool.query(
    `insert into access_requests (email, display_name, message, status, token, token_expires_at)
     values ('newbie@example.com', 'Newbie', 'Hi, it is me', 'pending', 'tok-abc', now() + interval '72 hours')`,
  );
  const admin = await agentFor(app, w.admin);
  const acc = await admin.get('/admin/access');
  assert.match(acc.text, /newbie@example\.com/);
  assert.match(acc.text, /action="\/admin\/access\/\d+\/approve"/);
  assert.match(acc.text, /action="\/admin\/access\/\d+\/deny"/);
  const users = await admin.get('/admin/users');
  assert.match(users.text, new RegExp(`action="/admin/users/${w.alice.id}/suspend"`));
  assert.match(users.text, new RegExp(`action="/admin/users/${w.alice.id}/promote"`));
  assert.match(users.text, /name="_csrf"\s+value="[a-f0-9]+"/);
});

test('admin static assets are served', async () => {
  for (const p of ['/css/admin.css', '/js/admin.js', '/js/admin-groups.js', '/js/admin-unfiled.js']) {
    assert.equal((await request(app).get(p)).status, 200, p);
  }
});
