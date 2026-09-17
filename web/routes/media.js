// Photo file serving. Session-gated; never express.static on PHOTO_DIR.
//
//   /media/thumbs/:id           thumbs/<id:08d>.jpg
//   /media/working/:id          working/<id:08d>_<sha8>.<ext>
//   /media/display/:id          ≤1600 px JPEG of the working copy, cut on first
//                               request and cached as display/<id>_v<version>.jpg, where
//                               version is synced_file_version (the file actually on disk)
//                               (what the photo page shows — phones don't pull
//                               multi-megabyte scans)
//   /media/backs/:back_id       backs/<back_id>.<ext> (working copy of the back)
//   /media/faces/:face_id       faces/<face_id>.jpg when the desktop pushed one;
//                               otherwise cut from the working copy at the bbox
//                               and cached as faces/gen_<id>_v<file_version>_<bbox hash>.jpg
//   /media/contrib/:file_id     contribution upload thumbnail (uploader, admin,
//                               moderator of a target group), cached next to
//                               the upload as thumb_<file_id>.jpg
//
// All photo routes check the photo is visible to req.user (not private, not
// deleted). Cache-Control: private, max-age=86400. 404 when visibility
// fails — never 403 (don't confirm existence to a non-member).
//
// Paths are resolved against PHOTO_DIR from the environment. Files are
// streamed with res.sendFile (which sets ETag / Last-Modified / Range).

const path = require('path');
const fs = require('fs');
const fsp = require('fs/promises');
const crypto = require('crypto');
const express = require('express');
const sharp = require('sharp');
const { requireUser } = require('../middleware/require-user');
const { photoVisibleSql } = require('../middleware/visibility');
const storage = require('../services/photo-storage');

const DISPLAY_EDGE = 1600;
const FACE_EDGE = 256;
const CONTRIB_THUMB_EDGE = 400;

function photoDir() {
  const dir = process.env.PHOTO_DIR;
  if (!dir) throw new Error('PHOTO_DIR not configured');
  return dir;
}

function paddedId(n) {
  return String(n).padStart(8, '0');
}

// The viewer may see this photo, but its file isn't on the server yet
// (metadata-only push, back not pushed, undecodable upload). Answer 200
// with a small neutral image, never 404: browsing a grid of metadata-only
// photos would otherwise fire dozens of 404s a minute and the VM's
// fail2ban `caddy-4xx-rate` jail bans the family member's IP (it banned
// George on 2026-09-17). Real "not found / not yours" stays 404.
const PLACEHOLDER_SVG = Buffer.from(
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 120" width="120" height="120">'
  + '<rect width="120" height="120" fill="#e6e1d6"/>'
  + '<path d="M38 44h44v32H38z M44 70l12-12 8 8 6-6 12 10" fill="none" stroke="#8a8378" stroke-width="3" stroke-linejoin="round"/>'
  + '<text x="60" y="96" font-family="system-ui,sans-serif" font-size="10" fill="#6b655b" text-anchor="middle">No image yet</text>'
  + '</svg>',
);

function placeholder(res) {
  res.setHeader('Cache-Control', 'private, no-cache');
  res.setHeader('X-Media-Placeholder', '1');
  res.type('image/svg+xml').send(PLACEHOLDER_SVG);
}

// Callers have already passed the visibility check.
function serve(res, absPath) {
  fs.stat(absPath, (err, stat) => {
    if (err || !stat.isFile()) return placeholder(res);
    res.setHeader('Cache-Control', 'private, max-age=86400');
    res.sendFile(absPath);
  });
}

async function exists(p) {
  try { return (await fsp.stat(p)).isFile(); } catch { return false; }
}

// Write via a temp name + rename so two concurrent first requests can't
// serve a half-written file.
async function writeAtomic(dest, buffer) {
  await fsp.mkdir(path.dirname(dest), { recursive: true });
  const tmp = `${dest}.${process.pid}.${crypto.randomBytes(4).toString('hex')}.tmp`;
  await fsp.writeFile(tmp, buffer);
  await fsp.rename(tmp, dest);
}

module.exports = function mediaRoutes({ pool }) {
  const router = express.Router();
  router.use(requireUser);

  async function visiblePhoto(user, id, cols = 'p.id') {
    const vSql = photoVisibleSql(user, { alias: 'p', paramIndex: 1 });
    const params = user.role === 'admin' ? [] : [user.id];
    const { rows } = await pool.query(
      `select ${cols} from photos p where p.id = $${params.length + 1} and ${vSql}`,
      [...params, id],
    );
    return rows[0] || null;
  }

  // Thumbnail — same-shape regardless of file_version.
  router.get('/thumbs/:id(\\d+)', async (req, res, next) => {
    try {
      const id = Number(req.params.id);
      if (!(await visiblePhoto(req.user, id))) return res.status(404).end();
      return serve(res, path.join(photoDir(), 'thumbs', `${paddedId(id)}.jpg`));
    } catch (err) { next(err); }
  });

  // Working copy — full working-file (may be large).
  router.get('/working/:id(\\d+)', async (req, res, next) => {
    try {
      const id = Number(req.params.id);
      const row = await visiblePhoto(req.user, id, 'p.working_path');
      if (!row) return res.status(404).end();
      if (!row.working_path) return placeholder(res);
      // working_path on the web side is a relative filename under
      // PHOTO_DIR/working/ (basename of the desktop working file).
      return serve(res, path.join(photoDir(), 'working', path.basename(row.working_path)));
    } catch (err) { next(err); }
  });

  // Display-size copy for the photo page.
  router.get('/display/:id(\\d+)', async (req, res, next) => {
    try {
      const id = Number(req.params.id);
      const row = await visiblePhoto(req.user, id, 'p.working_path, coalesce(p.synced_file_version, p.file_version) as file_version');
      if (!row) return res.status(404).end();
      if (!row.working_path) return placeholder(res);
      const dest = path.join(photoDir(), 'display', `${id}_v${row.file_version || 1}.jpg`);
      if (!(await exists(dest))) {
        const src = path.join(photoDir(), 'working', path.basename(row.working_path));
        if (!(await exists(src))) return placeholder(res);
        const buf = await sharp(src, { failOn: 'none' })
          .rotate()
          .resize({ width: DISPLAY_EDGE, height: DISPLAY_EDGE, fit: 'inside', withoutEnlargement: true })
          .jpeg({ quality: 82, mozjpeg: true })
          .toBuffer();
        await writeAtomic(dest, buf);
      }
      return serve(res, dest);
    } catch (err) { next(err); }
  });

  // Back image — served through the parent photo's visibility.
  router.get('/backs/:back_id(\\d+)', async (req, res, next) => {
    try {
      const backId = Number(req.params.back_id);
      const vSql = photoVisibleSql(req.user, { alias: 'p', paramIndex: 1 });
      const params = req.user.role === 'admin' ? [] : [req.user.id];
      const { rows } = await pool.query(
        `select pb.id, pb.sha256, pb.working_path
           from photo_backs pb
           join photos p on p.id = pb.photo_id
          where pb.id = $${params.length + 1}
            and ${vSql}`,
        [...params, backId],
      );
      if (rows.length === 0) return res.status(404).end();
      // The JPEG written by PUT /sync/photo_backs/:id/file (name from id + sha).
      const derived = storage.backPath(backId, rows[0].sha256);
      if (await exists(derived)) return serve(res, derived);
      if (!rows[0].working_path) return placeholder(res);
      return serve(res, path.join(photoDir(), 'backs', path.basename(rows[0].working_path)));
    } catch (err) { next(err); }
  });

  // Face crop — served through the parent photo's visibility.
  router.get('/faces/:face_id(\\d+)', async (req, res, next) => {
    try {
      const faceId = Number(req.params.face_id);
      const vSql = photoVisibleSql(req.user, { alias: 'p', paramIndex: 1 });
      const params = req.user.role === 'admin' ? [] : [req.user.id];
      const { rows } = await pool.query(
        `select f.id, f.bbox, p.working_path, p.width, p.height, coalesce(p.synced_file_version, p.file_version) as file_version
           from faces f
           join photos p on p.id = f.photo_id
          where f.id = $${params.length + 1}
            and f.is_deleted = false
            and ${vSql}`,
        [...params, faceId],
      );
      const face = rows[0];
      if (!face) return res.status(404).end();
      const pushed = path.join(photoDir(), 'faces', `${faceId}.jpg`);
      if (await exists(pushed)) return serve(res, pushed);

      const b = face.bbox || {};
      const hash = crypto.createHash('sha1').update(JSON.stringify([b.x, b.y, b.w, b.h])).digest('hex').slice(0, 8);
      const dest = path.join(photoDir(), 'faces', `gen_${faceId}_v${face.file_version || 1}_${hash}.jpg`);
      if (!(await exists(dest))) {
        const src = face.working_path && path.join(photoDir(), 'working', path.basename(face.working_path));
        if (!src || !(await exists(src))) return placeholder(res);
        // bbox is in the EXIF-transposed frame at full resolution (fix-up 6).
        const rotated = await sharp(src, { failOn: 'none' }).rotate().toBuffer({ resolveWithObject: true });
        const W = rotated.info.width, H = rotated.info.height;
        // Scale if the stored dims differ from the file (e.g. a downsized working copy).
        const sx = face.width ? W / face.width : 1;
        const sy = face.height ? H / face.height : 1;
        const x = Number(b.x) * sx, y = Number(b.y) * sy, w = Number(b.w) * sx, h = Number(b.h) * sy;
        if (![x, y, w, h].every(Number.isFinite) || w <= 0 || h <= 0) return placeholder(res);
        const px = w * 0.15, py = h * 0.15;
        const left = Math.max(0, Math.floor(x - px));
        const top = Math.max(0, Math.floor(y - py));
        const right = Math.min(W, Math.ceil(x + w + px));
        const bottom = Math.min(H, Math.ceil(y + h + py));
        if (right - left < 2 || bottom - top < 2) return placeholder(res);
        const buf = await sharp(rotated.data)
          .extract({ left, top, width: right - left, height: bottom - top })
          .resize({ width: FACE_EDGE, height: FACE_EDGE, fit: 'inside', withoutEnlargement: false })
          .jpeg({ quality: 85 })
          .toBuffer();
        await writeAtomic(dest, buf);
      }
      return serve(res, dest);
    } catch (err) { next(err); }
  });

  // Contribution upload thumbnail.
  router.get('/contrib/:file_id(\\d+)', async (req, res, next) => {
    try {
      const fileId = Number(req.params.file_id);
      const { rows } = await pool.query(
        `select cf.id, cf.contribution_id, cf.stored_path, cf.mime, cf.is_video, c.user_id, c.group_ids
           from contribution_files cf join contributions c on c.id = cf.contribution_id
          where cf.id = $1`, [fileId],
      );
      const f = rows[0];
      if (!f) return res.status(404).end();
      let allowed = req.user.role === 'admin' || Number(f.user_id) === Number(req.user.id);
      if (!allowed && (f.group_ids || []).length) {
        const mod = await pool.query(
          `select 1 from group_members
            where user_id = $1 and role = 'moderator' and is_deleted = false
              and group_id = any($2::bigint[]) limit 1`,
          [req.user.id, f.group_ids],
        );
        allowed = mod.rows.length > 0;
      }
      if (!allowed) return res.status(404).end();
      if (f.is_video || !f.stored_path) return placeholder(res);
      const src = path.join(photoDir(), f.stored_path);
      const dest = path.join(photoDir(), 'uploads', String(f.contribution_id), `thumb_${fileId}.jpg`);
      if (!(await exists(dest))) {
        if (!(await exists(src))) return placeholder(res);
        let buf;
        try {
          buf = await sharp(src, { failOn: 'none' })
            .rotate()
            .resize({ width: CONTRIB_THUMB_EDGE, height: CONTRIB_THUMB_EDGE, fit: 'inside', withoutEnlargement: true })
            .jpeg({ quality: 80 })
            .toBuffer();
        } catch {
          return placeholder(res); // e.g. HEIC the bundled libvips can't decode
        }
        await writeAtomic(dest, buf);
      }
      return serve(res, dest);
    } catch (err) { next(err); }
  });

  return router;
};
