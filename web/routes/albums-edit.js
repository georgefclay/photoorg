// Phase 12 — album editing for admins (closes PROJECT-PLAN §5 item 11).
// Plain forms, so it works one-handed on a phone with or without JS:
//
//   POST /albums                              create (name, description[, photo_id])
//   GET  /albums/:id/edit                     rename, describe, re-order, remove
//   POST /albums/:id/edit                     name / description
//   POST /albums/:id/delete | /restore        soft-delete / restore
//   POST /albums/:id/photos/:pid/remove       soft-remove one photo
//   POST /albums/:id/photos/:pid/move         dir=up|down
//   POST /photos/:pid/albums                  add a photo (album_id) from the photo page
//
// services/albums.js holds the rules, the audit rows and the sync stamps.
const express = require('express');
const { requireAdmin } = require('../middleware/require-user');
const { safeReturnTo } = require('../services/scope');
const albums = require('../services/albums');

function toId(v) {
  const n = Number(v);
  return Number.isSafeInteger(n) && n > 0 ? n : null;
}

module.exports = function albumEditRoutes({ pool }) {
  const router = express.Router();

  function back(req, res, fallback) {
    const to = req.body && req.body.return_to ? safeReturnTo(req.body.return_to) : fallback;
    res.redirect(303, to);
  }

  router.post('/albums', requireAdmin, async (req, res, next) => {
    try {
      const r = await albums.createAlbum(pool, req.user, { name: req.body.name, description: req.body.description });
      if (r.error) return back(req, res, '/albums?error=name');
      const pid = toId(req.body.photo_id);
      if (pid) await albums.setPhotoInAlbum(pool, req.user, r.id, pid, true);
      back(req, res, `/albums/${r.id}`);
    } catch (err) { next(err); }
  });

  router.get('/albums/:id(\\d+)/edit', requireAdmin, async (req, res, next) => {
    try {
      const id = toId(req.params.id);
      const album = id ? await albums.getAlbum(pool, id) : null;
      if (!album) {
        return res.status(404).render('error', { title: 'Not found', message: 'That album does not exist, or it has been removed.' });
      }
      res.render('album-edit', {
        album, rows: await albums.editRows(pool, id), saved: req.query.saved || null, error: req.query.error || null,
      });
    } catch (err) { next(err); }
  });

  router.post('/albums/:id(\\d+)/edit', requireAdmin, async (req, res, next) => {
    try {
      const id = toId(req.params.id);
      const r = await albums.updateAlbum(pool, req.user, id, { name: req.body.name, description: req.body.description });
      if (!r) return res.redirect(303, '/albums');
      if (r.error) return res.redirect(303, `/albums/${id}/edit?error=name`);
      res.redirect(303, `/albums/${id}/edit?saved=${r.changed ? 'details' : 'none'}`);
    } catch (err) { next(err); }
  });

  router.post('/albums/:id(\\d+)/delete', requireAdmin, async (req, res, next) => {
    try {
      await albums.setAlbumDeleted(pool, req.user, toId(req.params.id), true);
      res.redirect(303, '/albums?removed=1');
    } catch (err) { next(err); }
  });

  router.post('/albums/:id(\\d+)/restore', requireAdmin, async (req, res, next) => {
    try {
      const id = toId(req.params.id);
      await albums.setAlbumDeleted(pool, req.user, id, false);
      res.redirect(303, `/albums/${id}`);
    } catch (err) { next(err); }
  });

  router.post('/albums/:id(\\d+)/photos/:pid(\\d+)/remove', requireAdmin, async (req, res, next) => {
    try {
      const id = toId(req.params.id);
      await albums.setPhotoInAlbum(pool, req.user, id, toId(req.params.pid), false);
      back(req, res, `/albums/${id}/edit?saved=removed`);
    } catch (err) { next(err); }
  });

  router.post('/albums/:id(\\d+)/photos/:pid(\\d+)/move', requireAdmin, async (req, res, next) => {
    try {
      const id = toId(req.params.id);
      const pid = toId(req.params.pid);
      await albums.movePhoto(pool, req.user, id, pid, req.body.dir === 'up' ? -1 : 1);
      res.redirect(303, `/albums/${id}/edit#p${pid}`);
    } catch (err) { next(err); }
  });

  router.post('/photos/:pid(\\d+)/albums', requireAdmin, async (req, res, next) => {
    try {
      const pid = toId(req.params.pid);
      const aid = toId(req.body.album_id);
      if (aid) await albums.setPhotoInAlbum(pool, req.user, aid, pid, true);
      res.redirect(303, `/photos/${pid}#albums`);
    } catch (err) { next(err); }
  });

  return router;
};
