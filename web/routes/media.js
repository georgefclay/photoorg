// Photo file serving. Session-gated; never express.static on PHOTO_DIR.
//
//   /media/thumbs/:id           thumbs/<id:08d>.jpg
//   /media/working/:id          working/<id:08d>_<sha8>.<ext>
//   /media/backs/:back_id       backs/<back_id>.<ext> (working copy of the back)
//   /media/faces/:face_id       faces/<face_id>.jpg (precomputed crop)
//
// All check: the photo is visible to req.user, not private, not deleted.
// Cache-Control: private, max-age=86400. 404 when visibility fails —
// never 403 (don't confirm existence to a non-member).
//
// Paths are resolved against PHOTO_DIR from the environment. Files are
// streamed with res.sendFile (which sets ETag / Last-Modified / Range).

const path = require('path');
const fs = require('fs');
const express = require('express');
const { requireUser } = require('../middleware/require-user');
const { photoVisibleSql } = require('../middleware/visibility');

function photoDir() {
  const dir = process.env.PHOTO_DIR;
  if (!dir) throw new Error('PHOTO_DIR not configured');
  return dir;
}

function paddedId(n) {
  return String(n).padStart(8, '0');
}

function serve(res, absPath) {
  // Set cache before sendFile so any error-path 404 still gets the header
  // stripped (Express won't overwrite a 404 with cache headers). Actual
  // set happens after existence check.
  fs.stat(absPath, (err, stat) => {
    if (err || !stat.isFile()) return res.status(404).end();
    res.setHeader('Cache-Control', 'private, max-age=86400');
    res.sendFile(absPath);
  });
}

module.exports = function mediaRoutes({ pool }) {
  const router = express.Router();
  router.use(requireUser);

  // Thumbnail — same-shape regardless of file_version.
  router.get('/thumbs/:id(\\d+)', async (req, res, next) => {
    try {
      const id = Number(req.params.id);
      const vSql = photoVisibleSql(req.user, { alias: 'p', paramIndex: 1 });
      const params = req.user.role === 'admin' ? [] : [req.user.id];
      const { rows } = await pool.query(
        `select p.id from photos p where p.id = $${params.length + 1} and ${vSql}`,
        [...params, id],
      );
      if (rows.length === 0) return res.status(404).end();
      const abs = path.join(photoDir(), 'thumbs', `${paddedId(id)}.jpg`);
      return serve(res, abs);
    } catch (err) { next(err); }
  });

  // Working copy — full working-file (may be large).
  router.get('/working/:id(\\d+)', async (req, res, next) => {
    try {
      const id = Number(req.params.id);
      const vSql = photoVisibleSql(req.user, { alias: 'p', paramIndex: 1 });
      const params = req.user.role === 'admin' ? [] : [req.user.id];
      const { rows } = await pool.query(
        `select p.working_path, p.mime
           from photos p
          where p.id = $${params.length + 1}
            and ${vSql}`,
        [...params, id],
      );
      if (rows.length === 0 || !rows[0].working_path) return res.status(404).end();
      // working_path on the web side is a relative filename under
      // PHOTO_DIR/working/ (basename of the desktop working file).
      const base = path.basename(rows[0].working_path);
      const abs = path.join(photoDir(), 'working', base);
      return serve(res, abs);
    } catch (err) { next(err); }
  });

  // Back image — served through the parent photo's visibility.
  router.get('/backs/:back_id(\\d+)', async (req, res, next) => {
    try {
      const backId = Number(req.params.back_id);
      const vSql = photoVisibleSql(req.user, { alias: 'p', paramIndex: 1 });
      const params = req.user.role === 'admin' ? [] : [req.user.id];
      const { rows } = await pool.query(
        `select pb.working_path, pb.master_path
           from photo_backs pb
           join photos p on p.id = pb.photo_id
          where pb.id = $${params.length + 1}
            and ${vSql}`,
        [...params, backId],
      );
      if (rows.length === 0 || !rows[0].working_path) return res.status(404).end();
      const base = path.basename(rows[0].working_path);
      const abs = path.join(photoDir(), 'backs', base);
      return serve(res, abs);
    } catch (err) { next(err); }
  });

  // Face crop — served through the parent photo's visibility.
  router.get('/faces/:face_id(\\d+)', async (req, res, next) => {
    try {
      const faceId = Number(req.params.face_id);
      const vSql = photoVisibleSql(req.user, { alias: 'p', paramIndex: 1 });
      const params = req.user.role === 'admin' ? [] : [req.user.id];
      const { rows } = await pool.query(
        `select f.id
           from faces f
           join photos p on p.id = f.photo_id
          where f.id = $${params.length + 1}
            and f.is_deleted = false
            and ${vSql}`,
        [...params, faceId],
      );
      if (rows.length === 0) return res.status(404).end();
      const abs = path.join(photoDir(), 'faces', `${faceId}.jpg`);
      return serve(res, abs);
    } catch (err) { next(err); }
  });

  return router;
};
