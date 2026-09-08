const express = require('express');
const { newToken, sha256Hex } = require('../services/tokens');
const { audit } = require('../services/audit');
const { send } = require('../services/email');

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function baseUrl(req) {
  return process.env.BASE_URL || `${req.protocol}://${req.get('host')}`;
}

module.exports = function loginRoutes({ pool, authLimiter }) {
  const router = express.Router();

  router.get('/login', (req, res) => {
    res.render('login', { error: null, values: { email: '' } });
  });

  router.post('/login', authLimiter, async (req, res, next) => {
    const email = String(req.body.email || '').trim().toLowerCase();
    const honeypot = String(req.body.website || '').trim();

    if (honeypot) {
      console.log(`[login] honeypot tripped ip=${req.ip}`);
      return res.render('login-sent');
    }

    if (!EMAIL_RE.test(email)) {
      return res.status(400).render('login', {
        error: 'Please enter a valid email address.',
        values: { email },
      });
    }

    const client = await pool.connect();
    try {
      await client.query('begin');
      const { rows } = await client.query(
        `select id, email, status from users where email = $1`,
        [email],
      );
      const user = rows[0];

      if (user && user.status === 'active') {
        const token = newToken();
        const tokenHash = sha256Hex(token);
        await client.query(
          `insert into magic_links (user_id, token_hash, expires_at)
           values ($1, $2, now() + interval '15 minutes')`,
          [user.id, tokenHash],
        );
        await audit(client, {
          actor: 'system',
          action: 'auth.magic_link.sent',
          entityType: 'users',
          entityId: user.id,
          userId: user.id,
          newValue: { reason: 'login_request', ip: req.ip },
        });
        await client.query('commit');

        await send({
          to: user.email,
          subject: 'Your sign-in link',
          template: 'login-link',
          vars: {
            displayName: user.email,
            signInUrl: `${baseUrl(req)}/a/${token}`,
          },
        });
      } else {
        // Don't leak whether the email is known or is suspended. Log for us
        // but say nothing different to the caller.
        await client.query('rollback');
        console.log(`[login] no active user for email=${email} ip=${req.ip}`);
      }

      return res.render('login-sent');
    } catch (err) {
      await client.query('rollback').catch(() => {});
      return next(err);
    } finally {
      client.release();
    }
  });

  return router;
};
