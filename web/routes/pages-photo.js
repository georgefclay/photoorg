// Photo detail page (/photos/:id). OWNER: photo-page agent.
//
// Server-rendered and readable without JavaScript. Everything the page
// shows comes from services/photos.getPhotoDetail (visibility applied
// there: null → 404, never 403) and photoNeighbours (prev/next inside the
// list named by ?from=, falling back to Browse order). Writes go through
// the JSON API from public/js/{photo,tagger,like,datefield}.js.

const express = require('express');
const { requireUser } = require('../middleware/require-user');
const { getScope } = require('../services/scope');
const { getPhotoDetail, photoNeighbours } = require('../services/photos');
const {
  describeSuggestions, faceBoxStyle, backLinkFor, altText,
} = require('../services/photo-page');

module.exports = function photoPageRoutes({ pool }) {
  const router = express.Router();

  router.get('/photos/:id(\\d+)', requireUser, async (req, res, next) => {
    try {
      const id = Number(req.params.id);
      if (!Number.isSafeInteger(id)) return notFound(res);
      const photo = await getPhotoDetail(pool, req.user, id);
      if (!photo) return notFound(res);

      const { scope } = await getScope(req, pool);
      const fromRaw = typeof req.query.from === 'string' ? req.query.from.slice(0, 40) : null;
      const [neighbours, suggestions] = await Promise.all([
        photoNeighbours(pool, req.user, id, fromRaw, scope),
        describeSuggestions(pool, photo.suggestions_pending),
      ]);

      const suggestedFaces = new Set(suggestions.filter((s) => s.kind === 'person' && s.mine && s.face_id).map((s) => s.face_id));
      const faces = photo.faces.map((f) => ({
        ...f,
        style: faceBoxStyle(f.bbox, photo.width, photo.height),
        suggested_by_me: suggestedFaces.has(f.id),
      }));

      res.render('photo', {
        photo,
        faces,
        suggestions,
        neighbours,
        // Only echo ?from= back into links when the viewer arrived with one.
        fromQ: fromRaw ? `?from=${encodeURIComponent(neighbours.from)}` : '',
        back: fromRaw ? backLinkFor(neighbours.from) : { href: '/', label: 'Photos' },
        alt: altText(photo),
        isAdmin: req.user.role === 'admin',
      });
    } catch (err) { next(err); }
  });

  function notFound(res) {
    return res.status(404).render('error', {
      title: 'Not found',
      message: "We couldn't find that photo. It may have been moved, or it isn't shared with you.",
    });
  }

  return router;
};
