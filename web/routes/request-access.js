const express = require('express');
const { newToken } = require('../services/tokens');
const { audit } = require('../services/audit');
const { send } = require('../services/email');

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function baseUrl(req) {
  return process.env.BASE_URL || `${req.protocol}://${req.get('host')}`;
}

module.exports = function requestAccessRoutes({ pool, authLimiter }) {
  const router = express.Router();

  router.get('/request-access', (req, res) => {
    res.render('request-access', { error: null, values: { email: '', display_name: '', message: '' } });
  });

  router.post('/request-access', authLimiter, async (req, res, next) => {
    const email = String(req.body.email || '').trim().toLowerCase();
    const displayName = String(req.body.display_name || '').trim() || null;
    const message = String(req.body.message || '').trim() || null;
    const honeypot = String(req.body.website || '').trim();

    // Honeypot filled → pretend to succeed. Do not enumerate.
    if (honeypot) {
      console.log(`[request-access] honeypot tripped ip=${req.ip}`);
      return res.render('request-access-sent');
    }

    if (!EMAIL_RE.test(email)) {
      return res.status(400).render('request-access', {
        error: 'Please enter a valid email address.',
        values: { email, display_name: displayName || '', message: message || '' },
      });
    }

    const client = await pool.connect();
    try {
      await client.query('begin');

      // Any live pending request for this email? Live = still within the
      // 72h token window. If yes, silently accept — never send a second mail.
      const dup = await client.query(
        `select id from access_requests
          where lower(email) = $1
            and status = 'pending'
            and token_expires_at > now()
          limit 1`,
        [email],
      );
      if (dup.rowCount > 0) {
        await audit(client, {
          actor: email,
          action: 'auth.request_access.duplicate',
          entityType: 'access_request',
          entityId: dup.rows[0].id,
          newValue: { ip: req.ip },
        });
        await client.query('commit');
        console.log(`[request-access] duplicate for ${email} ip=${req.ip}`);
        return res.render('request-access-sent');
      }

      const token = newToken();
      const { rows } = await client.query(
        `insert into access_requests
           (email, display_name, message, status, token, token_expires_at)
         values ($1, $2, $3, 'pending', $4, now() + interval '72 hours')
         returning id`,
        [email, displayName, message, token],
      );
      const reqId = rows[0].id;

      await audit(client, {
        actor: email,
        action: 'auth.request_access',
        entityType: 'access_request',
        entityId: reqId,
        newValue: { email, display_name: displayName, has_message: !!message, ip: req.ip },
      });

      await client.query('commit');

      const admin = process.env.ADMIN_EMAIL;
      if (admin) {
        const base = baseUrl(req);
        await send({
          to: admin,
          subject: `Access request from ${email}`,
          template: 'access-request-admin',
          vars: {
            requesterEmail: email,
            requesterName: displayName || '',
            message: message || '',
            approveUrl: `${base}/admin/access/${token}/approve`,
            denyUrl: `${base}/admin/access/${token}/deny`,
            adminListUrl: `${base}/admin/access`,
          },
        });
      } else {
        console.warn('[request-access] ADMIN_EMAIL is not set — nobody was notified');
      }

      return res.render('request-access-sent');
    } catch (err) {
      await client.query('rollback').catch(() => {});
      return next(err);
    } finally {
      client.release();
    }
  });

  return router;
};
