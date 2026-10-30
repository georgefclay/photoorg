// Phase 12 — downloads: selection through visibility, the zip's tree and
// contents, the two paths, the background job in a child process, the
// disk guard, the sweep, and 200-not-4xx pages.
const { test, beforeEach, after } = require('node:test');
const fs = require('fs');
const path = require('path');
const {
  pool, makeApp, assert, resetDb, seedWorld, agentFor, insertPhoto, addToGroup, request,
  email, testDbUrl,
} = require('./page-helpers');
const dl = require('../services/downloads');
const pp = require('../services/people-pages');

const app = makeApp();
const PHOTO_DIR = process.env.PHOTO_DIR;
const MAIL_SINK = fs.mkdtempSync(path.join(require('os').tmpdir(), 'photoarchive-test-mail-'));
const HOLDS = fs.mkdtempSync(path.join(require('os').tmpdir(), 'photoarchive-test-hold-'));
dl.setWorkerEnv({ DOWNLOAD_WORKER_DATABASE_URL: testDbUrl, MAIL_SINK_DIR: MAIL_SINK, POSTMARK_API_KEY: '' });
// The background test holds its worker; the hold file is named per job.
const realKick = dl.kick;
dl.kick = async (pool, opts) => {
  const next = (await pool.query(`select id from download_jobs where status = 'queued' order by id limit 1`)).rows[0];
  if (next) dl.setWorkerEnv({ DOWNLOAD_WORKER_DATABASE_URL: testDbUrl, MAIL_SINK_DIR: MAIL_SINK, POSTMARK_API_KEY: '',
    DOWNLOAD_WORKER_HOLD_FILE: path.join(HOLDS, `hold-${next.id}`) });
  return realKick(pool, opts);
};

after(async () => { await pool.end(); });

// ---- a minimal reader for a `store` zip ----------------------------------
function readZip(buf) {
  let i = buf.length - 22;
  while (i >= 0 && buf.readUInt32LE(i) !== 0x06054b50) i -= 1;
  assert.ok(i >= 0, 'end of central directory');
  const count = buf.readUInt16LE(i + 10);
  let off = buf.readUInt32LE(i + 16);
  const out = new Map();
  for (let n = 0; n < count; n += 1) {
    assert.equal(buf.readUInt32LE(off), 0x02014b50);
    const method = buf.readUInt16LE(off + 10);
    const size = buf.readUInt32LE(off + 20);
    const nameLen = buf.readUInt16LE(off + 28);
    const extraLen = buf.readUInt16LE(off + 30);
    const commentLen = buf.readUInt16LE(off + 32);
    const local = buf.readUInt32LE(off + 42);
    const name = buf.toString('utf8', off + 46, off + 46 + nameLen);
    const start = local + 30 + buf.readUInt16LE(local + 26) + buf.readUInt16LE(local + 28);
    out.set(name, { method, data: buf.subarray(start, start + size) });
    off += 46 + nameLen + extraLen + commentLen;
  }
  return out;
}
const binary = (res, cb) => {
  const chunks = [];
  res.on('data', (c) => chunks.push(c));
  res.on('end', () => cb(null, Buffer.concat(chunks)));
};

async function writeWorkingFiles() {
  const dir = path.join(PHOTO_DIR, 'working');
  fs.mkdirSync(dir, { recursive: true });
  const { rows } = await pool.query(
    `select id, working_path from photos where synced_file_version is not null and working_path is not null`,
  );
  for (const r of rows) fs.writeFileSync(path.join(dir, path.basename(r.working_path)), `image-bytes-${r.id}`);
  // A back image on disk too: it must never reach a zip.
  fs.mkdirSync(path.join(PHOTO_DIR, 'backs'), { recursive: true });
  fs.writeFileSync(path.join(PHOTO_DIR, 'backs', 'b1.jpg'), 'back-bytes');
}

function rmDownloads() { fs.rmSync(path.join(PHOTO_DIR, 'downloads'), { recursive: true, force: true }); }

let w;
beforeEach(async () => {
  await resetDb();
  rmDownloads();
  delete process.env.DOWNLOAD_SYNC_MAX_PHOTOS;
  w = await seedWorld();
  await writeWorkingFiles();
});

async function zipFor(agent, qs) {
  const r = await agent.get(`/download/zip?${qs}`).buffer(true).parse(binary);
  assert.equal(r.status, 200);
  assert.equal(r.headers['content-type'], 'application/zip');
  return readZip(r.body);
}

// ---- selection ------------------------------------------------------------

test('a selection never includes a photo the user cannot open, and counts like the page', async () => {
  const sel = dl.parseSelection({ kind: 'everything' });
  const alice = await dl.gather(pool, { ...w.alice, role: 'contributor' }, sel);
  assert.deepEqual(alice.rows.map((r) => r.id).sort(), [w.photos.clay1, w.photos.clay2].sort());
  const carol = await dl.gather(pool, { ...w.carol, role: 'contributor' }, sel);
  assert.deepEqual(carol.rows.map((r) => r.id), [w.photos.boots1]);
  const olive = await dl.gather(pool, { ...w.olive, role: 'contributor' }, sel);
  assert.equal(olive.rows.length, 0);
  const admin = await dl.gather(pool, { ...w.admin, role: 'admin' }, sel);
  assert.ok(!admin.rows.some((r) => r.id === w.photos.priv), 'private never');

  // Album: the download's count is the album page's count, per user.
  for (const u of [w.alice, w.carol, w.admin]) {
    const user = { ...u, role: u === w.admin ? 'admin' : 'contributor' };
    const g = await dl.gather(pool, user, dl.parseSelection({ kind: 'album', id: w.album }));
    const n = await pp.countPhotos(pool, user, { filters: { album_id: w.album }, scope: { kind: 'all' } });
    assert.equal(g.rows.length, n);
  }
});

test('a contributor cannot widen the scope to a group they are not in', async () => {
  const sel = dl.parseSelection({ kind: 'browse', scope: String(w.boots) });
  const g = await dl.gather(pool, { ...w.alice, role: 'contributor' }, sel);
  assert.deepEqual(g.rows.map((r) => r.id).sort(), [w.photos.clay1, w.photos.clay2].sort());
});

test('search selection matches the search page', async () => {
  const a = await agentFor(app, w.alice);
  const page = await a.get('/search?q=easter');
  assert.equal(page.status, 200);
  assert.match(page.text, /1 photo found/);
  assert.match(page.text, /href="\/download\?q=easter&amp;kind=search"/);
  const g = await dl.gather(pool, { ...w.alice, role: 'contributor' }, dl.parseSelection({ kind: 'search', q: 'easter' }));
  assert.deepEqual(g.rows.map((r) => r.id), [w.photos.clay1]);
});

// ---- the zip --------------------------------------------------------------

test('immediate zip: tree rules, README, manifest, no backs, People tree on request', async () => {
  // A year-only photo, an exact one, and an unconfirmed one in the album too.
  const yearOnly = await insertPhoto({ capture_date: '1975-01-01', capture_date_precision: 'year', capture_date_confirmed: true });
  const exact = await insertPhoto({ capture_date: '1980-06-15', capture_date_precision: 'exact', capture_date_confirmed: true });
  const pending = await insertPhoto({ capture_date: '1981-01-01', capture_date_precision: 'exact', capture_date_confirmed: false });
  for (const id of [yearOnly, exact, pending]) {
    await addToGroup(id, w.clay);
    await pool.query(`insert into album_photos (album_id, photo_id, position) values ($1, $2, 9)`, [w.album, id]);
  }
  await writeWorkingFiles();
  const a = await agentFor(app, w.alice);

  const conf = await a.get(`/download?kind=album&id=${w.album}`);
  assert.equal(conf.status, 200);
  assert.match(conf.text, /It downloads now/);

  const zip = await zipFor(a, `kind=album&id=${w.album}&people_tree=1`);
  const pad = (n) => String(n).padStart(8, '0');
  const names = [...zip.keys()];
  assert.ok(zip.has(`1962/03/1962-03_${pad(w.photos.clay1)}.jpg`), names.join('\n'));
  assert.ok(zip.has(`1975/1975_${pad(yearOnly)}.jpg`), 'year → top of the year folder');
  assert.ok(zip.has(`1980/06/1980-06-15_${pad(exact)}.jpg`));
  assert.ok(zip.has(`Unsorted/undated_${pad(pending)}.jpg`), 'unconfirmed → Unsorted');
  assert.ok(zip.has(`Unsorted/undated_${pad(w.photos.clay2)}.jpg`), 'unconfirmed decade → Unsorted');
  assert.ok(zip.has(`People/Peggy Clay/1962-03_${pad(w.photos.clay1)}.jpg`), 'People tree');
  assert.equal(zip.get(`People/Peggy Clay/1962-03_${pad(w.photos.clay1)}.jpg`).data.toString(), `image-bytes-${w.photos.clay1}`);
  assert.ok(!names.some((n) => /back/i.test(n)), 'no backs');
  for (const e of zip.values()) assert.equal(e.method, 0, 'store method');
  assert.ok(!zip.has('MISSING.txt'), 'a contributor has nothing missing');

  const readme = zip.get('README.txt').data.toString();
  assert.match(readme, /Requested by: Alice <alice@example.com>/);
  assert.match(readme, /Selection: {4}Album “Summer 1992 — Canada”/);
  assert.match(readme, /Photos in this zip: {8}5/);
  assert.match(readme, /no month is invented/);

  const manifest = zip.get('manifest.csv').data.toString().trim().split('\r\n');
  assert.equal(manifest[0], 'photo_id,path,master_sha256,scan_locator,capture_date,date_precision,people,albums,places,in_zip');
  const clay1Line = manifest.find((l) => l.startsWith(`${w.photos.clay1},`));
  assert.match(clay1Line, /1962\/03\/1962-03_\d+\.jpg ; People\/Peggy Clay\//);
  assert.match(clay1Line, /Batch 00012 #017/);
  assert.match(clay1Line, /,1962-03-01,month,Peggy Clay,Summer 1992 — Canada,Toronto,yes$/);
  const pendingLine = manifest.find((l) => l.startsWith(`${pending},`));
  assert.match(pendingLine, /,,,,Summer 1992 — Canada,,yes$/, 'an unconfirmed date is blank');

  // Never touched the disk.
  assert.ok(!fs.existsSync(path.join(PHOTO_DIR, 'downloads')), 'immediate path wrote nothing');
  const aud = (await pool.query(`select action from audit_log where action like 'download.%' order by id`)).rows.map((r) => r.action);
  assert.deepEqual(aud, ['download.request', 'download.fetched']);
});

test('a removed album membership or tag is not in the manifest', async () => {
  await pool.query(`update photo_places set is_deleted = true where photo_id = $1`, [w.photos.clay1]);
  const a = await agentFor(app, w.alice);
  const zip = await zipFor(a, `kind=person&id=${w.people.peggy.id}`);
  const line = zip.get('manifest.csv').data.toString().split('\r\n').find((l) => l.startsWith(`${w.photos.clay1},`));
  assert.match(line, /,Peggy Clay,Summer 1992 — Canada,,yes$/);
});

test('a visible photo with no file on the server is listed in MISSING.txt', async () => {
  const meta = await insertPhoto({ synced_file_version: null, scan_batch: 'Batch 00033', scan_sequence: 2 });
  await addToGroup(meta, w.clay);
  const a = await agentFor(app, w.alice);
  const zip = await zipFor(a, 'kind=browse');
  const missing = zip.get('MISSING.txt').data.toString();
  assert.match(missing, new RegExp(`#${meta}  undated  Batch 00033 #002  \\(not on the server yet\\)`));
  assert.ok(![...zip.keys()].some((n) => n.includes(String(meta).padStart(8, '0'))));
  // And it adds no bytes to the estimate.
  await pool.query(`update photos set file_size = 9e15 where id = $1`, [meta]);
  const g = await dl.gather(pool, { ...w.alice, role: 'contributor' }, dl.parseSelection({ kind: 'browse' }));
  assert.ok(g.summary.bytes < 1e6);
  assert.equal(g.summary.missing_count, 1);
});

test('one photo downloads as the file itself, named for its date', async () => {
  const a = await agentFor(app, w.alice);
  const r = await a.get(`/download/photo/${w.photos.clay1}`).buffer(true).parse(binary);
  assert.equal(r.status, 200);
  assert.match(r.headers['content-disposition'], new RegExp(`1962-03_${String(w.photos.clay1).padStart(8, '0')}\\.jpg`));
  assert.equal(r.body.toString(), `image-bytes-${w.photos.clay1}`);
  // Not visible → 404, like the photo page.
  assert.equal((await a.get(`/download/photo/${w.photos.boots1}`)).status, 404);
  // Visible but no file → a 200 page that says so.
  const meta = await insertPhoto({ synced_file_version: null });
  await addToGroup(meta, w.clay);
  const none = await a.get(`/download/photo/${meta}`);
  assert.equal(none.status, 200);
  assert.match(none.text, /No image on the server yet/);
});

// ---- the background path -----------------------------------------------

async function waitFor(fn, ms = 20000) {
  const until = Date.now() + ms;
  for (;;) {
    const v = await fn();
    if (v) return v;
    if (Date.now() > until) throw new Error('timed out');
    await new Promise((r) => setTimeout(r, 100));
  }
}

test('background job: child process builds the zip, the API keeps answering, email, 72 h in SQL', async () => {
  const meta = await insertPhoto({ synced_file_version: null });
  const a = await agentFor(app, w.admin);
  const conf = await a.get('/download?kind=everything');
  assert.equal(conf.status, 200);
  assert.match(conf.text, /emails you a link/);
  const post = await a.post('/download').type('form').send({ _csrf: a.csrf, kind: 'everything' });
  assert.equal(post.status, 303);
  const jobId = Number(post.headers.location.split('/').pop());

  // The page request while the worker runs: the worker is held at its
  // start until the page has answered, so "while" is not a race.
  const hold = path.join(HOLDS, `hold-${jobId}`);
  await waitFor(async () => {
    const r = (await pool.query(`select status, worker_pid from download_jobs where id = $1`, [jobId])).rows[0];
    return r.status === 'running' && r.worker_pid ? r : null;
  });
  const t0 = Date.now();
  const page = await a.get('/');
  const ms = Date.now() - t0;
  assert.equal(page.status, 200);
  assert.equal((await pool.query(`select status from download_jobs where id = $1`, [jobId])).rows[0].status, 'running');
  assert.ok(ms < 5000, `page took ${ms} ms`);
  fs.writeFileSync(hold, '');

  const job = await waitFor(async () => {
    const r = (await pool.query(`select * from download_jobs where id = $1`, [jobId])).rows[0];
    return r.status === 'done' || r.status === 'failed' ? r : null;
  });
  assert.equal(job.status, 'done', job.error);
  assert.ok(job.worker_pid == null);
  const span = (await pool.query(
    `select extract(epoch from expires_at - finished_at)::int as s from download_jobs where id = $1`, [jobId],
  )).rows[0].s;
  assert.equal(span, 72 * 3600, 'expires_at = finished_at + 72 hours, both from the same SQL now()');

  const zip = readZip(fs.readFileSync(job.zip_path));
  assert.match(zip.get('MISSING.txt').data.toString(), new RegExp(`#${meta} `));
  assert.ok(zip.has(`1962/03/1962-03_${String(w.photos.clay1).padStart(8, '0')}.jpg`));
  assert.ok(!fs.existsSync(`${job.zip_path}.part`));

  const mail = await waitFor(() => fs.readdirSync(MAIL_SINK)
    .map((f) => JSON.parse(fs.readFileSync(path.join(MAIL_SINK, f), 'utf8')))
    .find((m) => m.template === 'download-ready' && m.vars.url.endsWith(`/downloads/${jobId}`)));
  assert.equal(mail.to, 'admin@example.com');

  const fetched = await a.get(`/downloads/${jobId}/file`).buffer(true).parse(binary);
  assert.equal(fetched.status, 200);
  assert.equal(fetched.body.length, Number(job.zip_bytes));
  const actions = (await pool.query(`select action from audit_log where entity_type = 'download_job' and entity_id = $1 order by id`, [jobId])).rows.map((r) => r.action);
  assert.deepEqual(actions, ['download.request', 'download.ready', 'download.fetched']);

  // Let the exit handler's sweep/kick settle before the next test truncates.
  await new Promise((r) => setTimeout(r, 300));
});

test('one queued-or-running job per user; admins exempt', async () => {
  const realKick = dl.kick;
  dl.kick = async () => null; // keep the jobs queued
  try {
    process.env.DOWNLOAD_SYNC_MAX_PHOTOS = '1';
    const a = await agentFor(app, w.alice);
    const first = await a.post('/download').type('form').send({ _csrf: a.csrf, kind: 'browse' });
    assert.equal(first.status, 303);
    const second = await a.post('/download').type('form').send({ _csrf: a.csrf, kind: 'browse' });
    assert.equal(second.status, 303);
    assert.equal(second.headers.location, `${first.headers.location}?already=1`);
    const conf = await a.get('/download?kind=browse');
    assert.match(conf.text, /already have a download being made/);

    const ad = await agentFor(app, w.admin);
    const x = await ad.post('/download').type('form').send({ _csrf: ad.csrf, kind: 'everything' });
    const y = await ad.post('/download').type('form').send({ _csrf: ad.csrf, kind: 'everything' });
    assert.notEqual(x.headers.location, y.headers.location);
  } finally {
    dl.kick = realKick;
  }
});

test('disk guard refuses what will not fit, and says so with a 200', async () => {
  await pool.query(`update photos set file_size = 9e15 where id = $1`, [w.photos.clay1]);
  const a = await agentFor(app, w.admin);
  const r = await a.post('/download').type('form').send({ _csrf: a.csrf, kind: 'everything' });
  assert.equal(r.status, 200);
  assert.match(r.text, /too big for the server's free space/);
  assert.match(r.text, /5 GB headroom/, 'admins see the numbers');
  assert.equal((await pool.query(`select count(*)::int as n from download_jobs`)).rows[0].n, 0);
});

// ---- sweep, restart, pages ------------------------------------------------

test('sweep: an expired zip is deleted and the row kept; startup fails orphaned running jobs', async () => {
  fs.mkdirSync(dl.downloadsDir(), { recursive: true });
  const z = dl.zipPathFor(900);
  fs.writeFileSync(z, 'zip');
  await pool.query(
    `insert into download_jobs (id, user_id, selection, status, zip_path, zip_bytes, finished_at, expires_at)
     values (900, $1, '{"kind":"everything","label":"Everything I can see"}', 'done', $2, 3,
             now() - interval '73 hours', now() - interval '1 hour'),
            (901, $1, '{"kind":"browse","label":"All photos in view"}', 'running', null, null, null, null),
            (902, $1, '{"kind":"browse","label":"Fresh"}', 'done', $3, 3, now(), now() + interval '72 hours')`,
    [w.alice.id, z, dl.zipPathFor(902)],
  );
  await pool.query(`update download_jobs set worker_pid = 2147483000, started_at = now() where id = 901`);
  const r = await dl.sweep(pool, { startup: true });
  assert.deepEqual(r, { failed: 1, expired: 1 });
  assert.ok(!fs.existsSync(z));
  const rows = (await pool.query(`select id, status, error from download_jobs order by id`)).rows;
  assert.deepEqual(rows.map((x) => [Number(x.id), x.status]), [[900, 'expired'], [901, 'failed'], [902, 'done']]);
  assert.match(rows[1].error, /request it again/);
  assert.ok(email.outbox.some((m) => m.template === 'download-failed' && m.to === 'alice@example.com'));
  const acts = (await pool.query(`select action from audit_log where action like 'download.%' order by id`)).rows.map((x) => x.action);
  assert.deepEqual(acts.sort(), ['download.expired', 'download.failed']);

  // The pages: expired and not-ready are 200 and say so.
  const a = await agentFor(app, w.alice);
  const exp = await a.get('/downloads/900/file');
  assert.equal(exp.status, 200);
  assert.match(exp.text, /Expired/);
  const failed = await a.get('/downloads/901');
  assert.equal(failed.status, 200);
  assert.match(failed.text, /Could not be made/);
  await pool.query(`insert into download_jobs (id, user_id, selection) values (903, $1, '{"label":"Later"}')`, [w.alice.id]);
  const queued = await a.get('/downloads/903/file');
  assert.equal(queued.status, 200);
  assert.match(queued.text, /Waiting its turn/);
  const list = await a.get('/downloads');
  assert.equal(list.status, 200);
  assert.match(list.text, /Everything I can see/);
  // Someone else's job is not theirs to see.
  const c = await agentFor(app, w.carol);
  assert.equal((await c.get('/downloads/900')).status, 404);
});

test('a signed-out reader of the email link lands back on the download after signing in', async () => {
  await pool.query(`insert into download_jobs (id, user_id, selection) values (910, $1, '{"label":"x"}')`, [w.alice.id]);
  const anon = request.agent(app);
  const r = await anon.get('/downloads/910');
  assert.equal(r.status, 302);
  assert.equal(r.headers.location, '/login?return_to=%2Fdownloads%2F910');
  const login = await anon.get(r.headers.location);
  assert.match(login.text, /name="return_to" value="\/downloads\/910"/);
  const csrf = /name="_csrf" value="([^"]+)"/.exec(login.text)[1];
  await anon.post('/login').type('form').send({ _csrf: csrf, email: 'alice@example.com', return_to: '/downloads/910' });
  const link = email.outbox.find((m) => m.template === 'login-link');
  assert.ok(!link.vars.signInUrl.includes('downloads'), 'the path is not in the emailed URL');
  const token = link.vars.signInUrl.split('/a/')[1];
  const done = await anon.post(`/a/${token}`);
  assert.equal(done.headers.location, '/downloads/910');
  // An off-site return_to is ignored.
  await pool.query(`update magic_links set return_to = '//evil.example' where return_to is not null`);
  const r2 = await request(app).get('/login?return_to=//evil.example');
  assert.doesNotMatch(r2.text, /evil/);
});
