// GET /api/csrf → { csrfToken } for authed users. Session-tied token
// mirrors what EJS pages get in `res.locals.csrfToken`. JS clients send
// it back on POST/PUT/PATCH/DELETE as the `X-CSRF-Token` header.
const express = require('express');
const { requireUser } = require('../middleware/require-user');

module.exports = function apiCsrfRoutes() {
  const router = express.Router();
  router.get('/', requireUser, (req, res) => {
    res.json({ csrfToken: res.locals.csrfToken || '' });
  });
  return router;
};
