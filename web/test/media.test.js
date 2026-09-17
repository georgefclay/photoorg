// /media/display, on-demand face crops, back images and contribution
// thumbnails: visibility gate, on-disk cache, EXIF orientation.
const test = require('node:test');
const { after, before, beforeEach } = require('node:test');
const fs = require('fs');
const path = require('path');
const sharp = require('sharp');
const {
  pool, makeApp, assert, request, resetDb, seedWorld, agentFor, insertPhoto, addToGroup, insertFace,
} = require('./page-helpers');

let app;
before(() => { app = makeApp(); });
after(async () => { await pool.end(); });
beforeEach(resetDb);

const DIR = () => process.env.PHOTO_DIR;

// Display frame 400×300: red, with a blue 100×100 square at (250, 50).
async function displayFrame() {
  const square = await sharp({ create: { width: 100, height: 100, channels: 3, background: { r: 0, g: 0, b: 255 } } }).png().toBuffer();
  return sharp({ create: { width: 400, height: 300, channels: 3, background: { r: 255, g: 0, b: 0 } } })
    .composite([{ input: square, left: 250, top: 50 }]).png().toBuffer();
}

// Write a working file whose pixels are stored rotated with EXIF orientation 6
// (a phone held upright): viewers rotate 90° clockwise to get the display frame.
async function writeRotatedWorking(photoId) {
  const raw = await sharp(await displayFrame()).rotate(-90).jpeg({ quality: 95 })
    .withMetadata({ orientation: 6 }).toBuffer();
  const base = `${String(photoId).padStart(8, '0')}_rot6.jpg`;
  fs.mkdirSync(path.join(DIR(), 'working'), { recursive: true });
  fs.writeFileSync(path.join(DIR(), 'working', base), raw);
  await pool.query(`update photos set working_path = $1, width = 400, height = 300, orientation = 6,
                    file_version = 2, synced_file_version = 2 where id = $2`, [base, photoId]);
}

async function meanRGB(buf) {
  const s = await sharp(buf).stats();
  return s.channels.slice(0, 3).map((c) => Math.round(c.mean));
}

test('face crop is cut in the EXIF-transposed frame and cached per synced file version', async () => {
  const w = await seedWorld();
  const pid = await insertPhoto({});
  await addToGroup(pid, w.clay);
  await writeRotatedWorking(pid);
  const faceId = await insertFace(pid, { bbox: { x: 250, y: 50, w: 100, h: 100 }, review: 'unknown' });
  const alice = await agentFor(app, w.alice);

  const r = await alice.get(`/media/faces/${faceId}`).buffer(true).parse((res, cb) => {
    const chunks = []; res.on('data', (c) => chunks.push(c)); res.on('end', () => cb(null, Buffer.concat(chunks)));
  });
  assert.equal(r.status, 200);
  // 15 % padding around the box is red; the centre must be the blue square.
  const meta = await sharp(r.body).metadata();
  const centre = await sharp(r.body)
    .extract({ left: Math.floor(meta.width / 2) - 10, top: Math.floor(meta.height / 2) - 10, width: 20, height: 20 })
    .toBuffer();
  const [red, green, blue] = await meanRGB(centre);
  assert.ok(blue > 200 && red < 40, `crop centre should be the blue square, got rgb(${red},${green},${blue})`);
  const [redAll] = await meanRGB(r.body);
  assert.ok(redAll > 60 && redAll < 140, 'padding is red on every side, so the box is centred');

  const cached = fs.readdirSync(path.join(DIR(), 'faces')).filter((f) => f.startsWith(`gen_${faceId}_v2_`));
  assert.equal(cached.length, 1, 'cached with the synced file version in the name');

  const carol = await agentFor(app, w.carol);
  assert.equal((await carol.get(`/media/faces/${faceId}`)).status, 404, 'non-member');
});

test('/media/display: 404 for non-members, private and deleted; cached on disk; oriented', async () => {
  const w = await seedWorld();
  const pid = await insertPhoto({});
  await addToGroup(pid, w.clay);
  await writeRotatedWorking(pid);
  const alice = await agentFor(app, w.alice);
  const r = await alice.get(`/media/display/${pid}`).buffer(true).parse((res, cb) => {
    const chunks = []; res.on('data', (c) => chunks.push(c)); res.on('end', () => cb(null, Buffer.concat(chunks)));
  });
  assert.equal(r.status, 200);
  const meta = await sharp(r.body).metadata();
  assert.deepEqual([meta.width, meta.height], [400, 300], 'display copy is upright');
  assert.ok(fs.existsSync(path.join(DIR(), 'display', `${pid}_v2.jpg`)));

  const carol = await agentFor(app, w.carol);
  assert.equal((await carol.get(`/media/display/${pid}`)).status, 404);
  await pool.query('update photos set is_private = true where id = $1', [pid]);
  assert.equal((await alice.get(`/media/display/${pid}`)).status, 404);
  const admin = await agentFor(app, w.admin);
  assert.equal((await admin.get(`/media/display/${pid}`)).status, 404, 'private is never served, even to admins');
  await pool.query('update photos set is_private = false, is_deleted = true where id = $1', [pid]);
  assert.equal((await admin.get(`/media/display/${pid}`)).status, 404, 'deleted');
});

test('a visible photo whose file is not on the server gets a 200 placeholder, never a 404 (fail2ban)', async () => {
  const w = await seedWorld();
  const pid = await insertPhoto({ synced_file_version: null });
  await addToGroup(pid, w.clay);
  const faceId = await insertFace(pid, { review: 'unknown' });
  const alice = await agentFor(app, w.alice);
  for (const url of [`/media/thumbs/${pid}`, `/media/display/${pid}`, `/media/working/${pid}`, `/media/faces/${faceId}`]) {
    const r = await alice.get(url);
    assert.equal(r.status, 200, url);
    assert.equal(r.headers['x-media-placeholder'], '1', url);
    assert.match(r.headers['content-type'], /image\/svg\+xml/, url);
  }
  // Not visible stays 404.
  const carol = await agentFor(app, w.carol);
  assert.equal((await carol.get(`/media/thumbs/${pid}`)).status, 404);
  assert.equal((await alice.get('/media/thumbs/999999')).status, 404);

  // Pages don't even ask for it: tile without <img>, photo page without display image or tagger.
  const browse = await alice.get('/');
  const tile = new RegExp(`<a class="tile missing" href="/photos/${pid}\\?[^>]*>(?!<img)`);
  assert.match(browse.text, tile);
  const api = await alice.get('/api/photos');
  assert.equal(api.body.items.find((p) => p.id === pid).has_file, false);
  const page = await alice.get(`/photos/${pid}`);
  assert.equal(page.status, 200);
  assert.doesNotMatch(page.text, new RegExp(`/media/display/${pid}`));
  assert.doesNotMatch(page.text, /data-face-layer/);
  // Who is this? skips faces nobody can see.
  const who = await alice.get('/api/faces/unknown');
  assert.ok(!who.body.items.some((f) => f.id === faceId));
});

test('/media/backs serves the JPEG pushed under the id+sha name; 404 for non-members', async () => {
  const w = await seedWorld();
  const back = (await pool.query('select id, sha256 from photo_backs limit 1')).rows[0];
  const storage = require('../services/photo-storage');
  fs.mkdirSync(path.join(DIR(), 'backs'), { recursive: true });
  fs.writeFileSync(storage.backPath(Number(back.id), back.sha256),
    await sharp({ create: { width: 20, height: 10, channels: 3, background: '#eee' } }).jpeg().toBuffer());
  const alice = await agentFor(app, w.alice);
  assert.equal((await alice.get(`/media/backs/${back.id}`)).status, 200);
  const carol = await agentFor(app, w.carol);
  assert.equal((await carol.get(`/media/backs/${back.id}`)).status, 404);
});

test('/media/contrib: uploader and admin yes, other contributors no', async () => {
  const w = await seedWorld();
  const cid = Number((await pool.query(
    `insert into contributions (user_id, status, group_ids) values ($1, 'pending', $2) returning id`,
    [w.alice.id, [w.clay]],
  )).rows[0].id);
  const rel = path.join('uploads', String(cid), '1.jpg');
  fs.mkdirSync(path.join(DIR(), 'uploads', String(cid)), { recursive: true });
  fs.writeFileSync(path.join(DIR(), rel), await sharp({ create: { width: 800, height: 600, channels: 3, background: '#0a0' } }).jpeg().toBuffer());
  const fid = Number((await pool.query(
    `insert into contribution_files (contribution_id, original_filename, stored_path, sha256, size, mime, status)
     values ($1, 'a.jpg', $2, 'csha', 10, 'image/jpeg', 'pending') returning id`, [cid, rel],
  )).rows[0].id);
  assert.equal((await (await agentFor(app, w.alice)).get(`/media/contrib/${fid}`)).status, 200);
  assert.equal((await (await agentFor(app, w.admin)).get(`/media/contrib/${fid}`)).status, 200);
  assert.equal((await (await agentFor(app, w.bob)).get(`/media/contrib/${fid}`)).status, 200, 'moderator of target group');
  assert.equal((await (await agentFor(app, w.carol)).get(`/media/contrib/${fid}`)).status, 404);
});
