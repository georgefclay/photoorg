// Phase 10 read endpoints the pages' JavaScript needs:
//
//   GET /api/attention               needs-attention counts (scoped)
//   GET /api/faces/unknown           "Who is this?" feed (scoped, keyset)
//   GET /api/dates/interpret?text=   live date-field interpretation
//   GET /api/places/autocomplete?q=  place picker
//   GET /api/search?q=&year_from=…   small search (Phase 11 replaces)
//
// All session-authed; visibility + scope applied in services/.

const express = require('express');
const { requireUser } = require('../middleware/require-user');
const { getScope } = require('../services/scope');
const { attentionCounts } = require('../services/photos');
const { listUnknownFaces } = require('../services/faces');
const { interpretDate } = require('../services/dates');
const { autocompletePlaces } = require('../services/places');
const { search } = require('../services/search');

module.exports = function apiMiscRoutes({ pool }) {
  const router = express.Router();
  router.use(requireUser);

  router.get('/attention', async (req, res, next) => {
    try {
      const { scope } = await getScope(req, pool);
      res.json(await attentionCounts(pool, req.user, scope));
    } catch (err) { next(err); }
  });

  router.get('/faces/unknown', async (req, res, next) => {
    try {
      const { scope } = await getScope(req, pool);
      res.json(await listUnknownFaces(pool, req.user, {
        cursor: req.query.cursor, limit: req.query.limit, scope,
      }));
    } catch (err) { next(err); }
  });

  router.get('/dates/interpret', (req, res) => {
    res.json(interpretDate(req.query.text));
  });

  router.get('/places/autocomplete', async (req, res, next) => {
    try {
      res.json({ items: await autocompletePlaces(pool, req.query.q) });
    } catch (err) { next(err); }
  });

  router.get('/search', async (req, res, next) => {
    try {
      const { scope } = await getScope(req, pool);
      const r = await search(pool, req.user, req.query, {
        scope, cursor: req.query.cursor, limit: req.query.limit,
      });
      res.json({ people: r.people, photos: r.photos.items, next: r.photos.next, filters: r.filters });
    } catch (err) { next(err); }
  });

  return router;
};
