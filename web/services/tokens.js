const crypto = require('crypto');

// 32 bytes → 64 hex chars. URL-safe, unambiguous.
function newToken() {
  return crypto.randomBytes(32).toString('hex');
}

function sha256Hex(input) {
  return crypto.createHash('sha256').update(input).digest('hex');
}

// Compare two strings in constant time. Returns false on any length mismatch
// (which is fine — that's what timingSafeEqual would do too, minus the throw).
function safeEqual(a, b) {
  const ab = Buffer.from(String(a || ''), 'utf8');
  const bb = Buffer.from(String(b || ''), 'utf8');
  if (ab.length !== bb.length) return false;
  return crypto.timingSafeEqual(ab, bb);
}

module.exports = { newToken, sha256Hex, safeEqual };
