// /api/albums — read only (answer C5: no album editing on the web in
// Phase 10). Albums are global; photo counts and the photo list are
// visibility- and scope-filtered, so an album with nothing visible still
// appears with count 0.

const express = require('express');
const { requireUser } = require('../middleware/require-user');
const { getScope } = require('../services/scope');
const { listPhotos } = require('../services/photos');
const { listAlbums, getAlbum } = require('../services/albums');

module.exports = function apiAlbumsRoutes({ pool }) {
  const router = express.Router();
  router.use(requireUser);

  router.get('/', async (req, res, next) => {
    try {
      const { scope } = await getScope(req, pool);
      res.json({ items: await listAlbums(pool, req.user, scope) });
    } catch (err) { next(err); }
  });

  router.get('/:id(\\d+)', async (req, res, next) => {
    try {
      const id = Number(req.params.id);
      const album = await getAlbum(pool, id);
      if (!album) return res.status(404).json({ error: 'not found' });
      const { scope } = await getScope(req, pool);
      const photos = await listPhotos(pool, req.user, {
        filters: { album_id: id }, sort: 'position', cursor: req.query.cursor, limit: req.query.limit, scope,
      });
      res.json({ ...album, photos: photos.items, next: photos.next });
    } catch (err) { next(err); }
  });

  return router;
};
