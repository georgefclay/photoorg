const { safeEqual } = require('../services/tokens');

// Bearer-token gate for the desktop app's sync endpoints (Phase 9 onwards).
// The token lives in SERVICE_TOKEN in the web/.env. There is no users row.
function requireService(req, res, next) {
  const expected = process.env.SERVICE_TOKEN;
  if (!expected) return res.status(500).json({ error: 'SERVICE_TOKEN not configured' });

  const header = req.get('authorization') || '';
  const m = /^Bearer\s+(.+)$/i.exec(header);
  if (!m) return res.status(401).json({ error: 'missing bearer token' });

  if (!safeEqual(m[1], expected)) {
    return res.status(401).json({ error: 'bad service token' });
  }
  req.service = true;
  next();
}

module.exports = { requireService };
