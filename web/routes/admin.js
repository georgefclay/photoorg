const express = require('express');
const { newToken, sha256Hex } = require('../services/tokens');
const { audit } = require('../services/audit');
const { send } = require('../services/email');
const { requireAdmin } = require('../middleware/require-user');

function baseUrl(req) {
  return process.env.BASE_URL || `${req.protocol}://${req.get('host')}`;
}

function emailNameFallback(email) {
  return String(email || '').split('@')[0] || null;
}

// Who is deciding? The token-link flow may fire before the admin ever
// signs in. We prefer the signed-in user, else the user record for
// ADMIN_EMAIL, else NULL. Also returns the string to record as actor.
async function resolveAdminIdentity(pool, req) {
  if (req.user && req.user.role === 'admin') {
    return { id: req.user.id, email: req.user.email };
  }
  const adminEmail = (process.env.ADMIN_EMAIL || '').toLowerCase();
  if (!adminEmail) return { id: null, email: 'system' };
  const { rows } = await pool.query(
    `select id, email from users where email = $1 and role = 'admin' limit 1`,
    [adminEmail],
  );
  if (rows[0]) return rows[0];
  return { id: null, email: adminEmail };
}

async function findLiveRequestByToken(client, token) {
  const { rows } = await client.query(
    `select id, email, display_name, message, status, token_expires_at
       from access_requests
      where token = $1
      limit 1`,
    [token],
  );
  const row = rows[0];
  if (!row) return { row: null, live: false };
  const live = row.status === 'pending' && new Date(row.token_expires_at) > new Date();
  return { row, live };
}

async function upsertUserFromRequest(client, req) {
  const email = req.email.toLowerCase();
  const existing = await client.query(
    `select id, email, display_name, role, status from users where email = $1`,
    [email],
  );
  if (existing.rows[0]) {
    return { user: existing.rows[0], created: false };
  }
  const displayName = req.display_name || emailNameFallback(email);
  const inserted = await client.query(
    `insert into users (email, display_name, role, status)
     values ($1, $2, 'contributor', 'active')
     returning id, email, display_name, role, status`,
    [email, displayName],
  );
  return { user: inserted.rows[0], created: true };
}

async function issueMagicLink(client, userId) {
  const token = newToken();
  const tokenHash = sha256Hex(token);
  await client.query(
    `insert into magic_links (user_id, token_hash, expires_at)
     values ($1, $2, now() + interval '15 minutes')`,
    [userId, tokenHash],
  );
  return token;
}

module.exports = function adminRoutes({ pool }) {
  const router = express.Router();

  // ---------- Token-link decisions (approve / deny) ----------
  // These MUST work without a session (the unguessable token is the auth).
  // GET renders a confirmation page. POST performs the action.

  router.get('/access/:token/approve', async (req, res, next) => {
    try {
      const { row, live } = await findLiveRequestByToken(pool, req.params.token);
      if (!row || !live) return res.status(410).render('expired');
      res.render('confirm-approve', { request: row, token: req.params.token });
    } catch (err) { next(err); }
  });

  router.post('/access/:token/approve', async (req, res, next) => {
    const client = await pool.connect();
    try {
      await client.query('begin');
      const { row, live } = await findLiveRequestByToken(client, req.params.token);
      if (!row || !live) {
        await client.query('rollback');
        return res.status(410).render('expired');
      }
      const admin = await resolveAdminIdentity(pool, req);
      const { user, created } = await upsertUserFromRequest(client, row);
      await client.query(
        `update access_requests
            set status = 'approved',
                decided_by = $1,
                decided_at = now()
          where id = $2`,
        [admin.id, row.id],
      );
      await audit(client, {
        actor: admin.email,
        action: 'auth.approve',
        entityType: 'access_request',
        entityId: row.id,
        userId: admin.id,
        previousValue: { status: 'pending' },
        newValue: { status: 'approved', user_id: user.id, user_email: user.email },
      });
      if (created) {
        await audit(client, {
          actor: admin.email,
          action: 'user.create',
          entityType: 'users',
          entityId: user.id,
          userId: admin.id,
          newValue: { email: user.email, role: user.role, status: user.status, via: 'access_request' },
        });
      }
      const linkToken = await issueMagicLink(client, user.id);
      await audit(client, {
        actor: 'system',
        action: 'auth.magic_link.sent',
        entityType: 'users',
        entityId: user.id,
        userId: user.id,
        newValue: { reason: 'welcome_after_approval' },
      });
      await client.query('commit');

      await send({
        to: user.email,
        subject: 'You now have access',
        template: 'welcome-first-link',
        vars: {
          displayName: user.display_name || user.email,
          signInUrl: `${baseUrl(req)}/a/${linkToken}`,
          adminEmail: process.env.ADMIN_EMAIL || 'the archive admin',
        },
      });

      res.render('decision-done', {
        title: 'Approved',
        message: `${user.email} has been approved and their sign-in link is on the way.`,
      });
    } catch (err) {
      await client.query('rollback').catch(() => {});
      next(err);
    } finally {
      client.release();
    }
  });

  router.get('/access/:token/deny', async (req, res, next) => {
    try {
      const { row, live } = await findLiveRequestByToken(pool, req.params.token);
      if (!row || !live) return res.status(410).render('expired');
      res.render('confirm-deny', { request: row, token: req.params.token });
    } catch (err) { next(err); }
  });

  router.post('/access/:token/deny', async (req, res, next) => {
    const client = await pool.connect();
    try {
      await client.query('begin');
      const { row, live } = await findLiveRequestByToken(client, req.params.token);
      if (!row || !live) {
        await client.query('rollback');
        return res.status(410).render('expired');
      }
      const admin = await resolveAdminIdentity(pool, req);
      await client.query(
        `update access_requests
            set status = 'denied',
                decided_by = $1,
                decided_at = now()
          where id = $2`,
        [admin.id, row.id],
      );
      await audit(client, {
        actor: admin.email,
        action: 'auth.deny',
        entityType: 'access_request',
        entityId: row.id,
        userId: admin.id,
        previousValue: { status: 'pending' },
        newValue: { status: 'denied' },
      });
      await client.query('commit');

      res.render('decision-done', {
        title: 'Denied',
        message: `The request from ${row.email} has been denied. No email was sent to them.`,
      });
    } catch (err) {
      await client.query('rollback').catch(() => {});
      next(err);
    } finally {
      client.release();
    }
  });

  // ---------- Admin list of pending requests ----------
  // Session-authed. Uses per-session CSRF (form buttons carry _csrf).

  router.get('/access', requireAdmin, async (req, res, next) => {
    try {
      const pending = await pool.query(
        `select id, email, display_name, message, token, created_at, token_expires_at
           from access_requests
          where status = 'pending'
            and token_expires_at > now()
          order by created_at desc`,
      );
      res.render('admin/access', { requests: pending.rows });
    } catch (err) { next(err); }
  });

  // Session-authed shortcut: approve/deny by request id (from the list),
  // rather than by token. Same logic underneath.
  router.post('/access/:id(\\d+)/approve', requireAdmin, async (req, res, next) => {
    const client = await pool.connect();
    try {
      await client.query('begin');
      const { rows } = await client.query(
        `select id, email, display_name, message, status, token_expires_at
           from access_requests
          where id = $1`,
        [req.params.id],
      );
      const row = rows[0];
      const live = row && row.status === 'pending' && new Date(row.token_expires_at) > new Date();
      if (!live) { await client.query('rollback'); return res.redirect('/admin/access'); }

      const { user, created } = await upsertUserFromRequest(client, row);
      await client.query(
        `update access_requests
            set status = 'approved',
                decided_by = $1,
                decided_at = now()
          where id = $2`,
        [req.user.id, row.id],
      );
      await audit(client, {
        actor: req.user.email,
        action: 'auth.approve',
        entityType: 'access_request',
        entityId: row.id,
        userId: req.user.id,
        previousValue: { status: 'pending' },
        newValue: { status: 'approved', user_id: user.id, user_email: user.email },
      });
      if (created) {
        await audit(client, {
          actor: req.user.email,
          action: 'user.create',
          entityType: 'users',
          entityId: user.id,
          userId: req.user.id,
          newValue: { email: user.email, role: user.role, status: user.status, via: 'access_request' },
        });
      }
      const linkToken = await issueMagicLink(client, user.id);
      await audit(client, {
        actor: 'system',
        action: 'auth.magic_link.sent',
        entityType: 'users',
        entityId: user.id,
        userId: user.id,
        newValue: { reason: 'welcome_after_approval' },
      });
      await client.query('commit');

      await send({
        to: user.email,
        subject: 'You now have access',
        template: 'welcome-first-link',
        vars: {
          displayName: user.display_name || user.email,
          signInUrl: `${baseUrl(req)}/a/${linkToken}`,
          adminEmail: process.env.ADMIN_EMAIL || 'the archive admin',
        },
      });
      res.redirect('/admin/access');
    } catch (err) {
      await client.query('rollback').catch(() => {});
      next(err);
    } finally {
      client.release();
    }
  });

  router.post('/access/:id(\\d+)/deny', requireAdmin, async (req, res, next) => {
    const client = await pool.connect();
    try {
      await client.query('begin');
      const { rows } = await client.query(
        `select id, email, status, token_expires_at
           from access_requests
          where id = $1`,
        [req.params.id],
      );
      const row = rows[0];
      const live = row && row.status === 'pending' && new Date(row.token_expires_at) > new Date();
      if (!live) { await client.query('rollback'); return res.redirect('/admin/access'); }

      await client.query(
        `update access_requests
            set status = 'denied',
                decided_by = $1,
                decided_at = now()
          where id = $2`,
        [req.user.id, row.id],
      );
      await audit(client, {
        actor: req.user.email,
        action: 'auth.deny',
        entityType: 'access_request',
        entityId: row.id,
        userId: req.user.id,
        previousValue: { status: 'pending' },
        newValue: { status: 'denied' },
      });
      await client.query('commit');
      res.redirect('/admin/access');
    } catch (err) {
      await client.query('rollback').catch(() => {});
      next(err);
    } finally {
      client.release();
    }
  });

  // ---------- Admin users list + actions ----------

  router.get('/users', requireAdmin, async (req, res, next) => {
    try {
      const { rows } = await pool.query(
        `select id, email, display_name, role, status, last_login_at, created_at
           from users
          where is_service = false
          order by created_at desc`,
      );
      res.render('admin/users', { users: rows });
    } catch (err) { next(err); }
  });

  async function actOnUser(req, res, next, { action, updates, checkPre, deleteSessions }) {
    const targetId = Number(req.params.id);
    if (!Number.isInteger(targetId)) return res.redirect('/admin/users');
    const client = await pool.connect();
    try {
      await client.query('begin');
      const { rows } = await client.query(
        `select id, email, role, status from users where id = $1 for update`,
        [targetId],
      );
      const target = rows[0];
      if (!target) { await client.query('rollback'); return res.redirect('/admin/users'); }
      if (checkPre && !checkPre(target)) {
        await client.query('rollback');
        return res.redirect('/admin/users');
      }
      // Never let an admin lock themselves out.
      if (target.id === req.user.id && (updates.status === 'suspended' || updates.role === 'contributor')) {
        await client.query('rollback');
        return res.status(400).render('error', {
          title: 'Not allowed',
          message: 'Refusing to demote or suspend yourself.',
        });
      }
      const prev = { role: target.role, status: target.status };
      const next = { ...prev, ...updates };
      await client.query(
        `update users set role = $1, status = $2 where id = $3`,
        [next.role, next.status, target.id],
      );
      if (deleteSessions) {
        // connect-pg-simple stores session JSON with our userId under sess->>'userId'.
        await client.query(
          `delete from "session" where (sess->>'userId')::bigint = $1`,
          [target.id],
        );
      }
      await audit(client, {
        actor: req.user.email,
        action,
        entityType: 'users',
        entityId: target.id,
        userId: req.user.id,
        previousValue: prev,
        newValue: next,
      });
      await client.query('commit');
      res.redirect('/admin/users');
    } catch (err) {
      await client.query('rollback').catch(() => {});
      next(err);
    } finally {
      client.release();
    }
  }

  router.post('/users/:id/suspend', requireAdmin, (req, res, next) =>
    actOnUser(req, res, next, {
      action: 'auth.suspend',
      updates: { status: 'suspended' },
      checkPre: (u) => u.status === 'active',
      deleteSessions: true,
    }));

  router.post('/users/:id/reactivate', requireAdmin, (req, res, next) =>
    actOnUser(req, res, next, {
      action: 'auth.reactivate',
      updates: { status: 'active' },
      checkPre: (u) => u.status === 'suspended',
    }));

  router.post('/users/:id/promote', requireAdmin, (req, res, next) =>
    actOnUser(req, res, next, {
      action: 'auth.role_change',
      updates: { role: 'admin' },
      checkPre: (u) => u.role === 'contributor',
    }));

  router.post('/users/:id/demote', requireAdmin, (req, res, next) =>
    actOnUser(req, res, next, {
      action: 'auth.role_change',
      updates: { role: 'contributor' },
      checkPre: (u) => u.role === 'admin',
    }));

  return router;
};
