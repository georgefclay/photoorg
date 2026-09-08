const rateLimit = require('express-rate-limit');

// 5 requests per 15 min per IP. Applied to POST /request-access and POST /login.
// The `trust proxy: 1` in server startup means req.ip is the real client IP
// behind Caddy, so the default keyGenerator does the right thing.
function makeAuthLimiter() {
  return rateLimit({
    windowMs: 15 * 60 * 1000,
    max: 5,
    standardHeaders: true,
    legacyHeaders: false,
    handler: (req, res /*, next, options */) => {
      res.status(429).render('error', {
        title: 'Too many attempts',
        message: 'You have tried this too many times. Wait 15 minutes and try again.',
      });
    },
  });
}

module.exports = { makeAuthLimiter };
