// Phase 10 read endpoints the pages' JavaScript needs:
//
//   GET /api/attention               needs-attention counts (scoped)
//   GET /api/faces/unknown           "Who is this?" feed (scoped, keyset)
//   GET /api/dates/interpret?text=   live date-field interpretation
//   GET /api/places/autocomplete?q=  place picker
//   GET /api/search?q=&sort=&…       ranked search with a `why` per hit
//   GET /api/search/autocomplete?q=  header box: people + places
//
// All session-authed; visibility + scope applied in services/.

const express = require('express');
const { requireUser } = require('../middleware/require-user');
const { getScope } = require('../services/scope');
const { attentionCounts } = require('../services/photos');
const { listUnknownFaces } = require('../services/faces');
const { interpretDate } = require('../services/dates');
const { autocompletePlaces } = require('../services/places');
const { search, autocompleteSearch } = require('../services/search');
const { parseFilters } = require('../services/photos');

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

  // Ranked search. Same service as the page, so the JSON and the HTML can
  // never disagree; `why` explains each hit in plain words.
  router.get('/search', async (req, res, next) => {
    try {
      const { scope } = await getScope(req, pool);
      const filters = parseFilters(req.query);
      const r = await search(pool, req.user, {
        q: req.query.q || '', filters, sort: req.query.sort, scope,
        cursor: req.query.cursor, limit: req.query.limit,
        withCount: req.query.count === '1',
      });
      res.json({
        query: r.query,
        understood: r.described,
        ignored: r.ignored,
        people: r.people,
        places: r.places,
        photos: r.photos.items,
        items: r.photos.items,
        next: r.photos.next,
        sort: r.photos.sort,
        count: r.count,
        took_ms: r.took_ms,
      });
    } catch (err) { next(err); }
  });

  router.get('/search/autocomplete', async (req, res, next) => {
    try {
      const { scope } = await getScope(req, pool);
      res.json(await autocompleteSearch(pool, req.user, req.query.q, scope));
    } catch (err) { next(err); }
  });

  return router;
};
