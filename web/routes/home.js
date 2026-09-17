const express = require('express');

// eslint-disable-next-line no-unused-vars
// GET / lives in routes/pages.js (Browse / landing).
module.exports = function homeRoutes({ pool }) {
  const router = express.Router();

  router.post('/logout', (req, res) => {
    if (!req.session) return res.redirect('/');
    req.session.destroy(() => {
      res.clearCookie('photoarchive.sid');
      res.redirect('/');
    });
  });

  return router;
};
