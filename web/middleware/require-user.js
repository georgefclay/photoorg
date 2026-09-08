function requireUser(req, res, next) {
  if (req.user) return next();
  if (req.method === 'GET') {
    return res.redirect('/login');
  }
  return res.status(401).render('error', {
    title: 'Signed out',
    message: 'You need to be signed in to do that.',
  });
}

function requireAdmin(req, res, next) {
  if (req.user && req.user.role === 'admin') return next();
  if (!req.user) return res.redirect('/login');
  return res.status(403).render('error', {
    title: 'Not allowed',
    message: 'Admin access is required for this page.',
  });
}

module.exports = { requireUser, requireAdmin };
