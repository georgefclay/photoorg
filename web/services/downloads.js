// Phase 12 — downloads. "Everyone holds a copy, so the archive survives
// the server dying."
//
// A download is a SELECTION expressed as a filter the other services
// already understand (an album, a person, a place, a search, Browse's
// filters, one photo, or everything) — never a list of ids posted by the
// browser. It is re-evaluated through visibility and the group scope
// every time it is used, including inside the background worker, so a
// zip can only ever hold what its owner can open at that moment.
//
// Two paths by size (CLAUDE.md "Downloads"):
//   * immediate — ≤ DOWNLOAD_SYNC_MAX_PHOTOS files and ≤
//     DOWNLOAD_SYNC_MAX_BYTES: the zip (store method) is streamed straight
//     to the response; nothing touches the disk and no file is buffered.
//     One photo is the degenerate case and gets the file itself.
//   * background — anything larger, and "everything I can see" always: a
//     `download_jobs` row, built by `node tools/zip-worker.js <id>` (one at
//     a time) into PHOTO_DIR/downloads/<id>.zip, announced by email,
//     expiring 72 hours later (computed in SQL).
//
// Sizes count only photos whose file is on this server: a visible photo
// with no file goes into MISSING.txt and adds no bytes (Phase 12 answer
// Q0), so metadata-only photos can never trip the disk guard.

const fs = require('fs');
const fsp = require('fs/promises');
const path = require('path');
const { spawn } = require('child_process');
const archiver = require('archiver');

const { visibleSql, filtersSql, parseFilters } = require('./photos');
const { scopeSql, resolveScope, groupsForSwitcher } = require('./scope');
const { selectionIdsSql } = require('./search');
const { audit } = require('./audit');
const email = require('./email');
const tree = require('./zip-tree');
const fmt = require('./format');

const GB = 1024 ** 3;

function limits() {
  const n = Number(process.env.DOWNLOAD_SYNC_MAX_PHOTOS);
  const b = Number(process.env.DOWNLOAD_SYNC_MAX_BYTES);
  return {
    maxPhotos: Number.isFinite(n) && n > 0 ? n : 200,
    maxBytes: Number.isFinite(b) && b > 0 ? b : 1 * GB,
  };
}

// Free space the disk guard keeps in hand beyond the zip itself.
const DISK_HEADROOM = 5 * GB;

function photoDir() {
  const dir = process.env.PHOTO_DIR;
  if (!dir) throw new Error('PHOTO_DIR not configured');
  return dir;
}
function downloadsDir() { return path.join(photoDir(), 'downloads'); }
function zipPathFor(jobId) { return path.join(downloadsDir(), `${jobId}.zip`); }

function toInt(v) {
  if (v == null || v === '') return null;
  const n = Number(v);
  return Number.isSafeInteger(n) && n > 0 ? n : null;
}
function truthy(v) { return v === true || v === 'true' || v === '1' || v === 'on'; }

// ---- selection --------------------------------------------------------

const KINDS = ['photo', 'album', 'person', 'place', 'search', 'browse', 'everything'];
const SEARCH_KEYS = ['year_from', 'year_to', 'person_id', 'place_id', 'album_id',
  'has_no_date', 'has_untagged_faces', 'has_unknown_faces', 'completeness_below'];
const BROWSE_KEYS = ['has_no_date', 'has_untagged_faces', 'has_unknown_faces',
  'year', 'decade', 'year_from', 'year_to'];

function pick(f, keys) {
  const out = {};
  for (const k of keys) {
    if (f[k] != null && f[k] !== false) out[k] = f[k];
  }
  return out;
}

// Query string / form body → a normalised selection, or null when it
// names nothing. `scopeKey` is the session's group scope: the pages apply
// it, so a download of "this page" does too. "Everything" and one photo
// ignore it (everything means everything; a photo is just itself).
function parseSelection(raw = {}, { scopeKey = 'all' } = {}) {
  const kind = KINDS.includes(String(raw.kind)) ? String(raw.kind) : null;
  if (!kind) return null;
  const id = toInt(raw.id);
  const f = parseFilters(raw);
  const sel = {
    kind,
    scope: kind === 'everything' || kind === 'photo' ? 'all' : String(raw.scope || scopeKey || 'all'),
    people_tree: truthy(raw.people_tree),
    filters: {},
  };
  switch (kind) {
    case 'photo': if (!id) return null; sel.photo_id = id; break;
    case 'album': if (!id) return null; sel.filters = { album_id: id }; break;
    case 'person': if (!id) return null; sel.filters = { person_id: id }; break;
    case 'place': if (!id) return null; sel.filters = { place_id: id }; break;
    case 'search': sel.q = f.q || ''; sel.filters = pick(f, SEARCH_KEYS); break;
    case 'browse': sel.filters = pick(f, BROWSE_KEYS); break;
    default: break;
  }
  return sel;
}

// The selection as query-string parameters (for the confirm form and the
// immediate link). Inverse of parseSelection.
function selectionQuery(sel) {
  const q = new URLSearchParams();
  q.set('kind', sel.kind);
  if (sel.photo_id) q.set('id', String(sel.photo_id));
  const idKey = { album: 'album_id', person: 'person_id', place: 'place_id' }[sel.kind];
  if (idKey) q.set('id', String(sel.filters[idKey]));
  if (sel.kind === 'search' || sel.kind === 'browse') {
    if (sel.q) q.set('q', sel.q);
    for (const [k, v] of Object.entries(sel.filters)) q.set(k, v === true ? '1' : String(v));
  }
  if (sel.kind !== 'everything' && sel.kind !== 'photo') q.set('scope', sel.scope);
  if (sel.people_tree) q.set('people_tree', '1');
  return q;
}

async function resolveSelScope(pool, user, sel) {
  const groups = await groupsForSwitcher(pool, user);
  return resolveScope(user, groups, sel.scope);
}

// `select p.id …` for the selection, through visibility and scope.
async function idsSql(pool, user, sel, scope) {
  if (sel.kind === 'search') {
    return selectionIdsSql(pool, user, { q: sel.q || '', filters: sel.filters || {}, scope });
  }
  const params = [];
  const clauses = [visibleSql(user, params, 'p')];
  if (sel.kind === 'photo') {
    params.push(sel.photo_id);
    clauses.push(`p.id = $${params.length}`);
  } else {
    const sc = scopeSql(scope, params, 'p');
    if (sc) clauses.push(sc);
    clauses.push(...filtersSql(sel.filters || {}, params, 'p'));
  }
  return { sql: `select p.id from photos p where ${clauses.join(' and ')}`, params };
}

// Plain-words name of the selection, for the page, README and email.
async function describeSelection(pool, sel, scope) {
  const one = async (sql, id) => ((await pool.query(sql, [id])).rows[0] || {}).n;
  let label;
  switch (sel.kind) {
    case 'photo': label = `Photo #${sel.photo_id}`; break;
    case 'album': {
      const n = await one('select name as n from albums where id = $1', sel.filters.album_id);
      label = n ? `Album “${n}”` : 'An album'; break;
    }
    case 'person': {
      const n = await one('select display_name as n from people where id = $1', sel.filters.person_id);
      label = n ? `Photos of ${n}` : 'Photos of one person'; break;
    }
    case 'place': {
      const n = await one('select name as n from places where id = $1', sel.filters.place_id);
      label = n ? `Photos at ${n}` : 'Photos at one place'; break;
    }
    case 'search': {
      const bits = [];
      if (sel.q) bits.push(`“${sel.q}”`);
      const f = sel.filters || {};
      if (f.year_from != null || f.year_to != null) bits.push(`${f.year_from ?? '…'}–${f.year_to ?? '…'}`);
      if (f.person_id) bits.push(`person ${(await one('select display_name as n from people where id = $1', f.person_id)) || f.person_id}`);
      if (f.place_id) bits.push(`place ${(await one('select name as n from places where id = $1', f.place_id)) || f.place_id}`);
      if (f.album_id) bits.push(`album ${(await one('select name as n from albums where id = $1', f.album_id)) || f.album_id}`);
      if (f.has_no_date) bits.push('no date');
      if (f.has_untagged_faces) bits.push('untagged faces');
      label = `Search ${bits.join(', ') || '(empty)'}`; break;
    }
    case 'browse': {
      const f = sel.filters || {};
      label = f.has_no_date ? 'Photos with no date'
        : f.has_untagged_faces ? 'Photos with untagged faces'
          : f.has_unknown_faces ? 'Photos with faces nobody has named'
            : f.year != null ? `Photos from ${f.year}`
              : 'All photos in view';
      break;
    }
    default: label = 'Everything I can see';
  }
  if (scope && scope.kind !== 'all' && sel.kind !== 'everything' && sel.kind !== 'photo') {
    label += ` (${scope.label})`;
  }
  return label;
}

// ---- gathering --------------------------------------------------------

function scanLocator(r) {
  if (!r.scan_batch && r.scan_sequence == null) return '';
  const seq = r.scan_sequence != null ? ` #${String(r.scan_sequence).padStart(3, '0')}` : '';
  return `${r.scan_batch || ''}${seq}`.trim();
}

function extOf(r) {
  const fromPath = r.working_path ? path.extname(String(r.working_path)) : '';
  return tree.normExt(fromPath || ({ 'image/tiff': 'tif', 'image/png': 'png' }[r.mime] || 'jpg'));
}

function workingAbs(r) {
  return r.working_path ? path.join(photoDir(), 'working', path.basename(String(r.working_path))) : null;
}

// Everything a zip needs, in date order: the photos with their people /
// albums / places (live rows only), the People folder names, and a summary.
async function gather(pool, user, sel) {
  const scope = await resolveSelScope(pool, user, sel);
  const { sql, params } = await idsSql(pool, user, sel, scope);
  const rows = (await pool.query(
    `with sel as (${sql})
     select p.id, to_char(p.capture_date, 'YYYY-MM-DD') as capture_date,
            p.capture_date_precision as precision, p.capture_date_confirmed as confirmed,
            p.sha256, p.mime, p.file_size, p.working_path, p.synced_file_version,
            p.scan_batch, p.scan_sequence, p.physical_ref_note, p.source_filename
       from photos p
       join (select distinct id from sel) s on s.id = p.id
      order by p.capture_date nulls last, p.id`,
    params,
  )).rows.map((r) => ({
    ...r,
    id: Number(r.id),
    file_size: r.file_size != null ? Number(r.file_size) : 0,
    ext: extOf(r),
    has_file: r.synced_file_version != null && Boolean(r.working_path),
    people: [], albums: [], places: [],
  }));

  const ids = rows.map((r) => r.id);
  const byId = new Map(rows.map((r) => [r.id, r]));
  const peopleById = new Map();
  if (ids.length) {
    const [faces, albums, places] = await Promise.all([
      pool.query(
        `select distinct f.photo_id, pe.id, pe.given_name, pe.nickname, pe.surname, pe.suffix,
                pe.display_name, pe.birth_year
           from faces f join people pe on pe.id = f.person_id
          where f.photo_id = any($1::bigint[]) and f.is_deleted = false and f.is_disputed = false
            and coalesce(f.review_status, 'pending') <> 'ignore' and pe.is_deleted = false
          order by f.photo_id, pe.id`, [ids]),
      pool.query(
        `select ap.photo_id, a.name
           from album_photos ap join albums a on a.id = ap.album_id
          where ap.photo_id = any($1::bigint[]) and ap.is_deleted = false and a.is_deleted = false
          order by ap.photo_id, lower(a.name)`, [ids]),
      pool.query(
        `select pp.photo_id, pl.name
           from photo_places pp join places pl on pl.id = pp.place_id
          where pp.photo_id = any($1::bigint[]) and pp.is_deleted = false and pl.is_deleted = false
          order by pp.photo_id, lower(pl.name)`, [ids]),
    ]);
    for (const f of faces.rows) {
      const pid = Number(f.id);
      if (!peopleById.has(pid)) peopleById.set(pid, { ...f, id: pid });
      byId.get(Number(f.photo_id)).people.push(pid);
    }
    for (const a of albums.rows) byId.get(Number(a.photo_id)).albums.push(a.name);
    for (const p of places.rows) byId.get(Number(p.photo_id)).places.push(p.name);
  }
  const folders = tree.peopleFolders([...peopleById.values()]);
  for (const r of rows) r.people_names = r.people.map((pid) => tree.commonName(peopleById.get(pid)));

  const label = await describeSelection(pool, sel, scope);
  return { sel, scope, label, rows, folders, summary: summarize(rows, sel) };
}

function summarize(rows, sel) {
  const files = rows.filter((r) => r.has_file);
  const copies = sel.people_tree ? files.reduce((n, r) => n + r.people.length, 0) : 0;
  const bytes = files.reduce((n, r) => n + r.file_size, 0)
    + (sel.people_tree ? files.reduce((n, r) => n + r.file_size * r.people.length, 0) : 0);
  return {
    photo_count: rows.length,
    file_count: files.length,
    missing_count: rows.length - files.length,
    people_copies: copies,
    bytes,
  };
}

// 'single' | 'immediate' | 'background'.
function pathFor(sel, summary) {
  const { maxPhotos, maxBytes } = limits();
  if (sel.kind === 'photo') return 'single';
  if (sel.kind === 'everything') return 'background';
  if (summary.file_count + summary.people_copies <= maxPhotos && summary.bytes <= maxBytes) return 'immediate';
  return 'background';
}

// ---- the zip ----------------------------------------------------------

function csvCell(v) {
  const s = v == null ? '' : String(v);
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

function confirmedDate(r) {
  return r.confirmed && r.capture_date && r.precision && r.precision !== 'unknown' ? r.capture_date : '';
}

// Stat every file once, up front, so README, manifest and MISSING agree
// with what is actually in the zip.
async function plan(g) {
  const files = [];
  const missing = [];
  for (const r of g.rows) {
    const abs = r.has_file ? workingAbs(r) : null;
    let st = null;
    if (abs) { try { st = await fsp.stat(abs); } catch { st = null; } }
    if (!st || !st.isFile()) {
      missing.push({ row: r, reason: r.has_file ? 'file missing on the server' : 'not on the server yet' });
      continue;
    }
    const main = tree.photoPath(r);
    files.push({ row: r, abs, size: st.size, name: main, copies: [] });
  }
  if (g.sel.people_tree) {
    for (const f of files) {
      f.copies = f.row.people.map((pid) => tree.peoplePath(f.row, g.folders.get(pid)));
    }
  }
  return { files, missing };
}

function readme(g, p, { requestedBy, requestedAt }) {
  const nCopies = p.files.reduce((n, f) => n + f.copies.length, 0);
  const lines = [
    'Cyber Dinosaurs — family photo archive',
    '======================================',
    '',
    'This zip is a copy of photographs from the family archive at',
    `${process.env.BASE_URL || 'https://cyberdinosaurs.com'}. Keep it: the point of the`,
    'archive is that everyone holds a copy, so nothing is lost if the server is.',
    '',
    `Requested by: ${requestedBy}`,
    `Requested:    ${requestedAt.toISOString().replace('T', ' ').slice(0, 16)} UTC`,
    `Selection:    ${g.label}`,
    '',
    'Counts',
    `  Photos in this selection:  ${g.rows.length}`,
    `  Photos in this zip:        ${p.files.length}`,
    `  Listed in MISSING.txt:     ${p.missing.length}${p.missing.length ? '' : ' (no MISSING.txt)'}`,
    `  People/ copies:            ${g.sel.people_tree ? nCopies : 'not requested'}`,
    '',
    'How the folders are built',
    '  A photo is filed by its date only when that date has been confirmed.',
    '  YYYY/MM/   the day or the month is known',
    '  YYYY/      only the year is known (no month is invented)',
    '  1960s/     only the decade is known',
    '  Unsorted/  no confirmed date',
    g.sel.people_tree ? '  People/<name>/  a second copy under each person tagged in the photo' : null,
    '',
    '  File names are <date>_<photo number>.<ext>, so they sort by date;',
    '  the photo number is the one the archive uses (cyberdinosaurs.com/photos/<number>).',
    '',
    'What else is inside',
    '  Every image carries its own metadata, written by the archive: who is in',
    '  it, the confirmed date, places, albums, what is written on the back, and',
    '  where the original print is filed (batch and number). Windows Photos,',
    '  macOS Photos, Lightroom and most other programs read it.',
    '  manifest.csv  one line per photo: number, path in this zip, the original',
    '                file\'s fingerprint (sha256), print location, date, people,',
    '                albums and places.',
    p.missing.length ? '  MISSING.txt   photos in the selection whose image is not on the server yet.' : null,
    '',
    'Backs of prints are not included as images: what is written on them is',
    'already in each photo\'s description.',
    '',
  ];
  return lines.filter((l) => l !== null).join('\r\n');
}

function manifest(p) {
  const head = ['photo_id', 'path', 'master_sha256', 'scan_locator', 'capture_date', 'date_precision',
    'people', 'albums', 'places', 'in_zip'];
  const out = [head.join(',')];
  const line = (r, pth, inZip) => [
    r.id, pth, r.sha256, [scanLocator(r), r.physical_ref_note].filter(Boolean).join(' | '),
    confirmedDate(r), confirmedDate(r) ? r.precision : '',
    r.people_names.join('; '), r.albums.join('; '), r.places.join('; '), inZip,
  ].map(csvCell).join(',');
  const all = [
    ...p.files.map((f) => ({ r: f.row, pth: [f.name, ...f.copies].join(' ; '), z: 'yes' })),
    ...p.missing.map((m) => ({ r: m.row, pth: '', z: 'missing' })),
  ].sort((a, b) => a.r.id - b.r.id);
  for (const x of all) out.push(line(x.r, x.pth, x.z));
  return out.join('\r\n') + '\r\n';
}

function missingTxt(p) {
  const lines = [
    'These photos are part of the selection, but their image is not on the server',
    'yet (the family archive pushes images group by group). Ask George for them,',
    'or download again later.',
    '',
  ];
  for (const m of p.missing) {
    const r = m.row;
    const date = confirmedDate(r) || 'undated';
    lines.push([`#${r.id}`, date, scanLocator(r) || r.source_filename || '', `(${m.reason})`]
      .filter(Boolean).join('  '));
  }
  return lines.join('\r\n') + '\r\n';
}

// Stream the zip into `out` (a response or a file stream). Resolves with
// the plan once archiver has finished writing.
async function writeZip(out, g, meta) {
  const p = await plan(g);
  const archive = archiver('zip', { store: true });
  const done = new Promise((resolve, reject) => {
    archive.on('error', reject);
    archive.on('warning', (err) => { if (err.code !== 'ENOENT') reject(err); });
    out.on('error', reject);
    archive.on('end', resolve);
    // A phone that gives up mid-download closes the response: stop reading
    // files into a socket nobody is listening to, and let the caller go.
    out.on('close', () => {
      if (!out.writableFinished) {
        archive.abort();
        reject(new Error('the download was closed before it finished'));
      }
    });
  });
  archive.pipe(out);
  archive.append(readme(g, p, meta), { name: 'README.txt' });
  archive.append(manifest(p), { name: 'manifest.csv' });
  if (p.missing.length) archive.append(missingTxt(p), { name: 'MISSING.txt' });
  for (const f of p.files) {
    archive.file(f.abs, { name: f.name });
    for (const c of f.copies) archive.file(f.abs, { name: c });
  }
  archive.finalize().catch(() => {}); // its outcome arrives through `done`
  await done;
  return { ...p, bytes: archive.pointer() };
}

function zipFileName(g, when = new Date()) {
  const slug = String(g.label).normalize('NFD').replace(/[̀-ͯ]/g, '')
    .replace(/[^A-Za-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 50).toLowerCase() || 'photos';
  const d = when.toISOString().slice(0, 10).replace(/-/g, '');
  return `family-photos-${slug}-${d}.zip`;
}

function requester(user) {
  return user.display_name ? `${user.display_name} <${user.email}>` : user.email;
}

// ---- audit helpers ----------------------------------------------------

async function auditDownload(pg, user, action, entityId, newValue) {
  await audit(pg, {
    actor: user ? user.email : 'system', action, entityType: 'download_job',
    entityId, userId: user ? user.id : null, newValue,
  });
}

// ---- background jobs --------------------------------------------------

function freeBytes(dir) {
  const s = fs.statfsSync(dir);
  return Number(s.bavail) * Number(s.bsize);
}

// { ok, need, free }: refuse when bytes × 1.1 + 5 GB will not fit.
function diskCheck(bytes) {
  fs.mkdirSync(downloadsDir(), { recursive: true });
  const need = Math.ceil(bytes * 1.1) + DISK_HEADROOM;
  const free = freeBytes(downloadsDir());
  return { ok: need <= free, need, free };
}

async function openJobFor(pg, userId) {
  return (await pg.query(
    `select * from download_jobs where user_id = $1 and status in ('queued','running')
      order by id limit 1`, [userId],
  )).rows[0] || null;
}

// Create a queued job. Returns { job } or { conflict: job } or
// { refused: { need, free } }.
async function createJob(pool, user, sel, g) {
  const disk = diskCheck(g.summary.bytes);
  if (!disk.ok) {
    await auditDownload(pool, user, 'download.refused', null,
      { selection: sel, reason: 'disk', need: disk.need, free: disk.free, ...g.summary });
    return { refused: disk };
  }
  const client = await pool.connect();
  try {
    await client.query('begin');
    await client.query(`select pg_advisory_xact_lock(hashtext('download_jobs:' || $1::text))`, [user.id]);
    if (user.role !== 'admin') {
      const open = await openJobFor(client, user.id);
      if (open) { await client.query('rollback'); return { conflict: open }; }
    }
    const job = (await client.query(
      `insert into download_jobs (user_id, selection, photo_count, missing_count, bytes_estimate)
       values ($1, $2, $3, $4, $5) returning *`,
      [user.id, { ...sel, label: g.label }, g.summary.photo_count, g.summary.missing_count, g.summary.bytes],
    )).rows[0];
    await auditDownload(client, user, 'download.request', job.id,
      { selection: sel, label: g.label, path: 'background', ...g.summary });
    await client.query('commit');
    return { job };
  } catch (err) {
    await client.query('rollback').catch(() => {});
    throw err;
  } finally {
    client.release();
  }
}

// Start the next queued job if none is running. One at a time across the
// whole server: the claim runs under one advisory lock.
let workerEnv = null;
function setWorkerEnv(env) { workerEnv = env; }

async function kick(pool, { onExit } = {}) {
  const client = await pool.connect();
  let job = null;
  try {
    await client.query('begin');
    await client.query(`select pg_advisory_xact_lock(hashtext('download_jobs:dispatch'))`);
    const running = (await client.query(`select 1 from download_jobs where status = 'running' limit 1`)).rows[0];
    if (!running) {
      job = (await client.query(
        `update download_jobs set status = 'running', started_at = now()
          where id = (select id from download_jobs where status = 'queued' order by id limit 1)
          returning id`,
      )).rows[0] || null;
    }
    await client.query('commit');
  } catch (err) {
    await client.query('rollback').catch(() => {});
    throw err;
  } finally {
    client.release();
  }
  if (!job) return null;
  const child = spawn(process.execPath, [path.join(__dirname, '..', 'tools', 'zip-worker.js'), String(job.id)], {
    stdio: ['ignore', 'inherit', 'inherit'],
    env: { ...process.env, ...(workerEnv || {}) },
  });
  await pool.query(`update download_jobs set worker_pid = $2 where id = $1`, [job.id, child.pid]);
  child.on('exit', (code) => {
    if (onExit) onExit(job.id, code);
    // A worker that died without recording an outcome is an orphan; the
    // sweep marks it failed, then the queue moves on.
    sweep(pool).catch((e) => console.error('[downloads] sweep after exit', e))
      .then(() => kick(pool, { onExit }))
      .catch((e) => console.error('[downloads] kick after exit', e));
  });
  return { id: Number(job.id), pid: child.pid };
}

function pidAlive(pid) {
  if (!pid) return false;
  try { process.kill(pid, 0); return true; } catch (e) { return e.code === 'EPERM'; }
}

async function jobUser(pg, userId) {
  return (await pg.query(`select id, email, display_name, role, status from users where id = $1`, [userId])).rows[0];
}

function jobUrl(id) {
  return `${process.env.BASE_URL || ''}/downloads/${id}`;
}

async function notify(user, template, subject, vars) {
  try {
    await email.send({ to: user.email, subject, template, vars });
  } catch (err) {
    console.error(`[downloads] email ${template} to ${user.email} failed`, err);
  }
}

async function failJob(pool, job, reason) {
  const row = (await pool.query(
    `update download_jobs set status = 'failed', error = $2, finished_at = now(), worker_pid = null
      where id = $1 and status in ('queued','running') returning *`,
    [job.id, String(reason).split('\n')[0].slice(0, 300)],
  )).rows[0];
  if (!row) return false;
  const user = await jobUser(pool, row.user_id);
  await auditDownload(pool, user, 'download.failed', row.id,
    { selection: row.selection, error: row.error });
  await notify(user, 'download-failed', 'Your download could not be made', {
    displayName: user.display_name || user.email, label: row.selection.label, reason: row.error,
    url: jobUrl(row.id),
  });
  return true;
}

// The worker's body. Builds PHOTO_DIR/downloads/<id>.zip via a .part file.
async function runJob(pool, jobId) {
  const job = (await pool.query(`select * from download_jobs where id = $1`, [jobId])).rows[0];
  if (!job || job.status !== 'running') return { skipped: true };
  const user = await jobUser(pool, job.user_id);
  try {
    if (!user || user.status !== 'active') throw new Error('the account is no longer active');
    const sel = job.selection;
    const g = await gather(pool, user, sel);
    fs.mkdirSync(downloadsDir(), { recursive: true });
    const final = zipPathFor(job.id);
    const part = `${final}.part`;
    const out = fs.createWriteStream(part);
    const closed = new Promise((resolve, reject) => { out.on('close', resolve); out.on('error', reject); });
    const p = await writeZip(out, g, { requestedBy: requester(user), requestedAt: new Date(job.created_at) });
    await closed;
    await fsp.rename(part, final);
    const size = (await fsp.stat(final)).size;
    const row = (await pool.query(
      `update download_jobs
          set status = 'done', zip_path = $2, zip_bytes = $3, photo_count = $4, missing_count = $5,
              finished_at = now(), expires_at = now() + interval '72 hours', worker_pid = null
        where id = $1 and status = 'running' returning *`,
      [job.id, final, size, g.rows.length, p.missing.length],
    )).rows[0];
    if (!row) return { skipped: true };
    await auditDownload(pool, user, 'download.ready', job.id, {
      selection: sel, zip_bytes: size, photo_count: g.rows.length, files: p.files.length,
      missing_count: p.missing.length,
    });
    await notify(user, 'download-ready', 'Your download is ready', {
      displayName: user.display_name || user.email, label: sel.label,
      photoCount: p.files.length, missingCount: p.missing.length,
      size: fmt.bytes(size),
      url: jobUrl(job.id), expiresAt: row.expires_at,
    });
    return { done: true, size };
  } catch (err) {
    console.error(`[downloads] job ${job.id} failed`, err);
    await fsp.rm(`${zipPathFor(job.id)}.part`, { force: true }).catch(() => {});
    await failJob(pool, job, err && err.message ? err.message : String(err));
    return { failed: true };
  }
}

// One sweep, run at server start, after every worker exit, and hourly from
// cron (tools/download-sweep.js):
//   * a `running` job whose worker is gone is failed and its owner told to
//     ask again (at startup that is every running job — the server's
//     children die with it);
//   * a `done` job past `expires_at` loses its zip (a derived file) and
//     becomes `expired`; the row stays.
async function sweep(pool, { startup = false } = {}) {
  const out = { failed: 0, expired: 0 };
  const running = (await pool.query(`select * from download_jobs where status = 'running'`)).rows;
  for (const j of running) {
    if (startup || !pidAlive(j.worker_pid)) {
      if (await failJob(pool, j, 'the server restarted while the zip was being made — please request it again')) out.failed += 1;
    }
  }
  const due = (await pool.query(
    `select * from download_jobs where status = 'done' and expires_at <= now()`,
  )).rows;
  for (const j of due) {
    const p = j.zip_path || zipPathFor(j.id);
    await fsp.rm(p, { force: true });
    const row = (await pool.query(
      `update download_jobs set status = 'expired' where id = $1 and status = 'done' returning *`, [j.id],
    )).rows[0];
    if (row) {
      out.expired += 1;
      const user = await jobUser(pool, row.user_id);
      await auditDownload(pool, user, 'download.expired', row.id,
        { selection: row.selection, zip_bytes: row.zip_bytes != null ? Number(row.zip_bytes) : null });
    }
  }
  return out;
}

async function listJobs(pool, user) {
  return (await pool.query(
    `select *, expires_at > now() as live from download_jobs where user_id = $1 order by id desc limit 50`,
    [user.id],
  )).rows;
}

async function getJob(pool, user, id) {
  const row = (await pool.query(
    `select j.*, j.expires_at > now() as live, u.email as owner_email
       from download_jobs j join users u on u.id = j.user_id where j.id = $1`, [id],
  )).rows[0];
  if (!row) return null;
  if (Number(row.user_id) !== Number(user.id) && user.role !== 'admin') return null;
  return row;
}

module.exports = {
  limits, KINDS, parseSelection, selectionQuery, describeSelection, gather, summarize, pathFor,
  writeZip, zipFileName, requester, auditDownload, diskCheck, createJob, openJobFor, kick,
  setWorkerEnv, runJob, sweep, failJob, listJobs, getJob, zipPathFor, downloadsDir, workingAbs,
  DISK_HEADROOM,
};
