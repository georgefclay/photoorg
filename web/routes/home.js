const express = require('express');

// eslint-disable-next-line no-unused-vars
module.exports = function homeRoutes({ pool }) {
  const router = express.Router();

  router.get('/', (req, res) => {
    res.render('home');
  });

  router.post('/logout', (req, res) => {
    if (!req.session) return res.redirect('/');
    req.session.destroy(() => {
      res.clearCookie('photoarchive.sid');
      res.redirect('/');
    });
  });

  return router;
};
