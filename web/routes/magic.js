const express = require('express');
const { sha256Hex } = require('../services/tokens');
const { audit } = require('../services/audit');

async function findMagicLink(pg, token) {
  const hash = sha256Hex(token);
  const { rows } = await pg.query(
    `select ml.id            as link_id,
            ml.user_id       as user_id,
            ml.expires_at    as expires_at,
            ml.used_at       as used_at,
            u.email          as email,
            u.status         as status
       from magic_links ml
       join users u on u.id = ml.user_id
      where ml.token_hash = $1
      limit 1`,
    [hash],
  );
  const row = rows[0];
  if (!row) return { row: null, usable: false };
  const usable =
    row.used_at === null &&
    new Date(row.expires_at) > new Date() &&
    row.status === 'active';
  return { row, usable };
}

module.exports = function magicRoutes({ pool }) {
  const router = express.Router();

  router.get('/a/:token', async (req, res, next) => {
    try {
      const { row, usable } = await findMagicLink(pool, req.params.token);
      if (!row || !usable) {
        if (row) {
          await audit(pool, {
            actor: 'system',
            action: 'auth.magic_link.expired_attempt',
            entityType: 'magic_links',
            entityId: row.link_id,
            userId: row.user_id,
            newValue: { stage: 'get', reason: describeReason(row), ip: req.ip },
          });
        }
        return res.status(410).render('expired');
      }
      res.render('confirm-token', { email: row.email, token: req.params.token });
    } catch (err) { next(err); }
  });

  router.post('/a/:token', async (req, res, next) => {
    const client = await pool.connect();
    try {
      await client.query('begin');
      const { row, usable } = await findMagicLink(client, req.params.token);
      if (!row || !usable) {
        if (row) {
          await audit(client, {
            actor: 'system',
            action: 'auth.magic_link.expired_attempt',
            entityType: 'magic_links',
            entityId: row.link_id,
            userId: row.user_id,
            newValue: { stage: 'post', reason: describeReason(row), ip: req.ip },
          });
        }
        await client.query('commit');
        return res.status(410).render('expired');
      }

      // Mark single-use before touching the session.
      await client.query(
        `update magic_links set used_at = now() where id = $1`,
        [row.link_id],
      );
      await client.query(
        `update users set last_login_at = now() where id = $1`,
        [row.user_id],
      );
      await audit(client, {
        actor: row.email,
        action: 'auth.login',
        entityType: 'users',
        entityId: row.user_id,
        userId: row.user_id,
        newValue: { via: 'magic_link', ip: req.ip },
      });
      await client.query('commit');

      // Regenerate the session id (defence against fixation), then persist
      // the userId. express-session's `regenerate` gives us a fresh empty
      // session — the connect-pg-simple store handles the row swap.
      req.session.regenerate((err) => {
        if (err) return next(err);
        req.session.userId = row.user_id;
        req.session.save((err2) => {
          if (err2) return next(err2);
          res.redirect('/');
        });
      });
    } catch (err) {
      await client.query('rollback').catch(() => {});
      next(err);
    } finally {
      client.release();
    }
  });

  return router;
};

function describeReason(row) {
  if (row.used_at) return 'already_used';
  if (new Date(row.expires_at) <= new Date()) return 'expired';
  if (row.status !== 'active') return 'user_not_active';
  return 'unknown';
}
