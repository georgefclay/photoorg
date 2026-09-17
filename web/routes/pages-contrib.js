// Upload (/upload, /upload/mine) and Who is this? (/who-is-this).
//
//   GET /upload           upload page (JS required for the upload itself)
//   GET /upload/mine      the caller's own contributions, newest first
//   GET /who-is-this      faces marked `unknown` on visible photos, scoped
//                         (?cursor= is the no-JS "More faces" continuation)
//
// Visibility and scope live in services/ (faces.js, scope.js,
// contributions.js); nothing here queries photos directly.
const express = require('express');
const { requireUser } = require('../middleware/require-user');
const { getScope } = require('../services/scope');
const { listUnknownFaces } = require('../services/faces');
const { listMyContributions, summaryLine } = require('../services/contributions');

const FACES_PAGE = 24;

// Percent box for the face on the whole photo, or null when we can't place it.
function faceBox(f) {
  const b = f.bbox || {};
  const W = Number(f.width), H = Number(f.height);
  const x = Number(b.x), y = Number(b.y), w = Number(b.w), h = Number(b.h);
  if (!(W > 0 && H > 0) || ![x, y, w, h].every(Number.isFinite) || w <= 0 || h <= 0) return null;
  const pct = (v, d) => Math.max(0, Math.min(100, (v / d) * 100));
  const left = pct(x, W), top = pct(y, H);
  return {
    left: +left.toFixed(2),
    top: +top.toFixed(2),
    width: +Math.min(pct(w, W), 100 - left).toFixed(2),
    height: +Math.min(pct(h, H), 100 - top).toFixed(2),
  };
}

module.exports = function contribPageRoutes({ pool }) {
  const router = express.Router();

  router.get('/upload', requireUser, async (req, res, next) => {
    try {
      // Admins get every group; contributors only their own (services/scope.js).
      const { groups } = await getScope(req, pool);
      res.render('upload', {
        groups,
        preselect: groups.length === 1 ? groups[0].id : null,
      });
    } catch (err) { next(err); }
  });

  router.get('/upload/mine', requireUser, async (req, res, next) => {
    try {
      const mine = await listMyContributions(pool, req.user.id);
      res.render('upload-mine', { mine, summary: summaryLine(mine.summary) });
    } catch (err) { next(err); }
  });

  router.get('/who-is-this', requireUser, async (req, res, next) => {
    try {
      const { scope } = await getScope(req, pool);
      const cursor = req.query.cursor ? String(req.query.cursor) : null;
      let faces;
      try {
        faces = await listUnknownFaces(pool, req.user, { cursor, limit: FACES_PAGE, scope });
      } catch (err) {
        if (!cursor) throw err;
        // A mangled continuation link: start again from the top.
        return res.redirect(303, '/who-is-this');
      }
      res.render('who-is-this', {
        faces: faces.items.map((f) => ({ ...f, box: faceBox(f) })),
        next: faces.next,
        isContinuation: Boolean(cursor),
        feedScope: scope,
        apiUrl: `/api/faces/unknown?limit=${FACES_PAGE}`,
      });
    } catch (err) { next(err); }
  });

  return router;
};

module.exports.faceBox = faceBox;
