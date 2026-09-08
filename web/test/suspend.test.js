const test = require('node:test');
const { after, before, beforeEach } = require('node:test');
const request = require('supertest');
const {
  pool, makeApp, truncateAll, insertUser, insertMagicLink, newToken, assert,
} = require('./helpers');

let app;
before(() => { app = makeApp(); });
beforeEach(async () => { await truncateAll(); });
after(async () => { await pool.end(); });

async function signInFreshly(email) {
  await insertUser({ email });
  const { rows } = await pool.query(`select id from users where email = $1`, [email]);
  const uid = rows[0].id;
  const token = newToken();
  await insertMagicLink({ userId: uid, token });
  const agent = request.agent(app);
  await agent.post(`/a/${token}`);
  return { agent, userId: uid };
}

test('a suspended user is bounced from authed pages and their session is gone', async () => {
  const { agent, userId } = await signInFreshly('suspend-me@example.com');

  // Confirm we can see /admin/access with an admin cookie: use a separate
  // admin user to do the suspending.
  await insertUser({ email: 'ops@example.com', role: 'admin' });
  const adminAgent = request.agent(app);
  {
    const token = newToken();
    await insertMagicLink({ userId: (await pool.query(`select id from users where email='ops@example.com'`)).rows[0].id, token });
    await adminAgent.post(`/a/${token}`);
  }
  // Load the users page to get a CSRF token bound to the admin session.
  const list = await adminAgent.get('/admin/users');
  const csrf = /name="_csrf"\s+value="([a-f0-9]+)"/.exec(list.text)[1];
  const susp = await adminAgent
    .post(`/users/${userId}/suspend`.replace(/^/, '/admin'))
    .type('form')
    .send({ _csrf: csrf });
  assert.equal(susp.status, 302);

  // Their session rows were purged.
  const { rows: sessRows } = await pool.query(
    `select count(*)::int as n from "session" where (sess->>'userId')::bigint = $1`,
    [userId],
  );
  assert.equal(sessRows[0].n, 0);

  // Even if a stale cookie remained (they didn't sign out), loadUser
  // refuses inactive users. We can prove this by trying a page as the
  // original (now suspended) user's agent — it should behave as anonymous.
  const anon = await agent.get('/');
  assert.doesNotMatch(anon.text, /Welcome/);

  // A fresh login attempt yields no email and no new magic_links.
  const before = (await pool.query(`select count(*)::int as n from magic_links where user_id = $1`, [userId])).rows[0].n;
  await request(app).post('/login').type('form').send({ email: 'suspend-me@example.com' });
  const after = (await pool.query(`select count(*)::int as n from magic_links where user_id = $1`, [userId])).rows[0].n;
  assert.equal(before, after, 'no new magic link issued for suspended user');
});
