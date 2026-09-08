const crypto = require('crypto');

// Per-session CSRF token. Generated lazily on first request that has a
// session, exposed to templates as res.locals.csrfToken, and checked on
// POST/PUT/PATCH/DELETE outside the exempt list.
//
// Exempt paths use an unguessable URL-embedded token as their CSRF defence:
//   POST /a/:token                        (magic-link redemption)
//   POST /admin/access/:token/approve     (admin decision from email)
//   POST /admin/access/:token/deny        (admin decision from email)

const EXEMPT_PATTERNS = [
  /^\/a\/[a-f0-9]{64}$/,
  /^\/admin\/access\/[a-f0-9]{64}\/(approve|deny)$/,
];

function isExempt(pathname) {
  return EXEMPT_PATTERNS.some((re) => re.test(pathname));
}

function ensureToken(req) {
  if (!req.session) return null;
  if (!req.session.csrfToken) {
    req.session.csrfToken = crypto.randomBytes(32).toString('hex');
  }
  return req.session.csrfToken;
}

function csrfMiddleware(req, res, next) {
  const token = ensureToken(req);
  res.locals.csrfToken = token || '';

  const method = req.method.toUpperCase();
  const mutating = method === 'POST' || method === 'PUT' || method === 'PATCH' || method === 'DELETE';
  if (!mutating) return next();
  if (isExempt(req.path)) return next();

  // Only authed POSTs are CSRF-protected. Pre-auth forms (request-access,
  // login) are defended by the honeypot + rate limiter. Runs after loadUser
  // so req.user is populated.
  if (!req.user) return next();

  const supplied = req.body && req.body._csrf;
  if (!token || !supplied || supplied !== token) {
    return res.status(403).render('error', {
      title: 'Forbidden',
      message: 'This form has expired. Reload the page and try again.',
    });
  }
  next();
}

module.exports = { csrfMiddleware };
