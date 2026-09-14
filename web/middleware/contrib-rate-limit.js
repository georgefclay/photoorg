// Per-user rate limiter for contributor write endpoints (answer #12):
// 300 requests / hour combined on suggestions / comments / likes / face
// tags / disputes. Admins are exempt. In-process counters — sufficient
// for a single-node deploy.
//
// This is not a strict-security tool; it's a courtesy cap so a runaway
// script from a signed-in family member doesn't fill the DB. If we ever
// scale beyond one Node process, replace the in-memory map with Redis.

const WINDOW_MS = 60 * 60 * 1000;
const DEFAULT_LIMIT = 300;

class UserCounter {
  constructor() { this.slots = new Map(); }
  hit(userId, limit) {
    const now = Date.now();
    const cutoff = now - WINDOW_MS;
    const arr = this.slots.get(userId) || [];
    while (arr.length && arr[0] < cutoff) arr.shift();
    if (arr.length >= limit) {
      const retryMs = arr[0] + WINDOW_MS - now;
      return { ok: false, retryMs };
    }
    arr.push(now);
    this.slots.set(userId, arr);
    return { ok: true };
  }
}

function makeContribLimiter({ limit = DEFAULT_LIMIT } = {}) {
  const counter = new UserCounter();
  return function contribLimit(req, res, next) {
    if (!req.user) return res.status(401).json({ error: 'sign in required' });
    if (req.user.role === 'admin') return next();
    const { ok, retryMs } = counter.hit(String(req.user.id), limit);
    if (!ok) {
      const retryS = Math.max(1, Math.ceil(retryMs / 1000));
      res.set('Retry-After', String(retryS));
      return res.status(429).json({ error: 'too many contributions this hour, try again later' });
    }
    next();
  };
}

module.exports = { makeContribLimiter };
