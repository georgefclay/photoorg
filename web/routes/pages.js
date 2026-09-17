// Browse (/) and the group switcher (POST /scope).
//
// Anonymous visitors get the landing page. Everything else is visibility-
// and scope-filtered through services/photos.js.

const express = require('express');
const { requireUser } = require('../middleware/require-user');
const { getScope, resolveScope, groupsForSwitcher, safeReturnTo } = require('../services/scope');
const {
  listPhotos, parseFilters, attentionCounts, listKeyForBrowse, BROWSE_SORTS, SORTS,
} = require('../services/photos');

const PAGE_SIZE = 60;

// Query string for a browse-style list, minus the cursor.
function listQuery(filters, sort) {
  const q = new URLSearchParams();
  if (sort && sort !== 'recent') q.set('sort', sort);
  if (filters.has_no_date) q.set('has_no_date', '1');
  if (filters.has_untagged_faces) q.set('has_untagged_faces', '1');
  if (filters.has_unknown_faces) q.set('has_unknown_faces', '1');
  return q;
}

function browseTitle(filters) {
  if (filters.has_no_date) return 'Photos with no date';
  if (filters.has_untagged_faces) return 'Photos with untagged faces';
  if (filters.has_unknown_faces) return 'Photos with faces nobody has named';
  return 'Photos';
}

module.exports = function pageRoutes({ pool }) {
  const router = express.Router();

  router.get('/', async (req, res, next) => {
    if (!req.user) return res.render('home');
    try {
      const { scope } = await getScope(req, pool);
      const all = parseFilters(req.query);
      const filters = {
        has_no_date: all.has_no_date,
        has_untagged_faces: all.has_untagged_faces,
        has_unknown_faces: all.has_unknown_faces,
      };
      const sort = BROWSE_SORTS.includes(req.query.sort) ? req.query.sort : 'recent';
      const cursor = req.query.cursor || null;
      const filtered = filters.has_no_date || filters.has_untagged_faces || filters.has_unknown_faces;

      const [photos, attention] = await Promise.all([
        listPhotos(pool, req.user, { filters, sort, cursor, limit: PAGE_SIZE, scope }),
        !filtered && !cursor ? attentionCounts(pool, req.user, scope) : Promise.resolve(null),
      ]);

      const q = listQuery(filters, sort);
      const apiQ = new URLSearchParams(q);
      apiQ.set('limit', String(PAGE_SIZE));
      const moreQ = new URLSearchParams(q);
      if (photos.next) moreQ.set('cursor', photos.next);

      res.render('browse', {
        photos,
        attention,
        filters,
        filtered,
        sort,
        sorts: BROWSE_SORTS.map((k) => ({ key: k, label: SORTS[k].label })),
        from: listKeyForBrowse(filters, sort),
        apiUrl: `/api/photos?${apiQ}`,
        moreUrl: `/?${moreQ}`,
        isContinuation: Boolean(cursor),
        heading: browseTitle(filters),
        groupScope: scope,
      });
    } catch (err) { next(err); }
  });

  // Group switcher. Stored in the session; every list page filters by it.
  router.post('/scope', requireUser, async (req, res, next) => {
    try {
      const groups = await groupsForSwitcher(pool, req.user);
      const scope = resolveScope(req.user, groups, req.body && req.body.scope);
      req.session.scope = scope.key;
      // Continuation cursors belong to the old scope — drop them.
      let back = safeReturnTo(req.body && req.body.return_to);
      back = back.replace(/([?&])cursor=[^&]*&?/, '$1').replace(/[?&]$/, '');
      if (req.get('accept') && req.get('accept').includes('application/json')) {
        return res.json({ ok: true, scope });
      }
      res.redirect(303, back || '/');
    } catch (err) { next(err); }
  });

  return router;
};
