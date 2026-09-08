const test = require('node:test');
const { after, before, beforeEach } = require('node:test');
const request = require('supertest');
const {
  pool, makeApp, truncateAll, email, insertUser, insertMagicLink, newToken, assert,
} = require('./helpers');

let app;
before(() => { app = makeApp(); });
beforeEach(async () => { await truncateAll(); });
after(async () => { await pool.end(); });

async function seedUserAndLink({ tokenExpiresSql = "now() + interval '15 minutes'", status = 'active' } = {}) {
  const user = await insertUser({ email: 'linkuser@example.com', status });
  const token = newToken();
  await insertMagicLink({ userId: user.id, token, expiresSql: tokenExpiresSql });
  return { user, token };
}

test('POST /login for an active user creates one magic_link and emails it', async () => {
  await insertUser({ email: 'known@example.com' });
  await request(app).post('/login').type('form').send({ email: 'known@example.com' });

  const { rows } = await pool.query(`select count(*)::int as n from magic_links`);
  assert.equal(rows[0].n, 1);
  const mail = email.outbox.filter((m) => m.to === 'known@example.com');
  assert.equal(mail.length, 1);
  assert.match(mail[0].subject, /Your sign-in link/);
  assert.match(mail[0].text, /\/a\/[a-f0-9]{64}/);
});

test('POST /login for an unknown email is indistinguishable but never emails', async () => {
  const res = await request(app).post('/login').type('form').send({ email: 'stranger@example.com' });
  assert.equal(res.status, 200);
  assert.match(res.text, /Check your inbox/);
  assert.equal(email.outbox.length, 0);
  const { rows } = await pool.query(`select count(*)::int as n from magic_links`);
  assert.equal(rows[0].n, 0);
});

test('GET /a/:token renders confirm-token and does not sign in', async () => {
  const { token } = await seedUserAndLink();
  const res = await request(app).get(`/a/${token}`);
  assert.equal(res.status, 200);
  assert.match(res.text, /linkuser@example\.com/);
  assert.match(res.text, /Continue/);

  const { rows } = await pool.query(`select used_at from magic_links order by id desc limit 1`);
  assert.equal(rows[0].used_at, null, 'GET does not mark used');
});

test('POST /a/:token signs in, sets last_login_at, and marks the link used', async () => {
  const { user, token } = await seedUserAndLink();
  const agent = request.agent(app);

  const res = await agent.post(`/a/${token}`);
  assert.equal(res.status, 302);
  assert.equal(res.headers.location, '/');

  // Session cookie was set; hitting / now shows the signed-in view.
  const home = await agent.get('/');
  assert.match(home.text, /Welcome/);

  const ml = (await pool.query(`select used_at from magic_links order by id desc limit 1`)).rows[0];
  assert.ok(ml.used_at, 'used_at set on POST');

  const u = (await pool.query(`select last_login_at from users where id = $1`, [user.id])).rows[0];
  assert.ok(u.last_login_at, 'last_login_at updated');
});

test('a second POST /a/:token fails with the expired page', async () => {
  const { token } = await seedUserAndLink();
  const agent = request.agent(app);

  const first = await agent.post(`/a/${token}`);
  assert.equal(first.status, 302);

  const second = await request(app).post(`/a/${token}`); // fresh agent, no session
  assert.equal(second.status, 410);
  assert.match(second.text, /expired/i);
});

test('an expired token fails on both GET and POST', async () => {
  const { token } = await seedUserAndLink({ tokenExpiresSql: "now() - interval '1 minute'" });
  const get = await request(app).get(`/a/${token}`);
  assert.equal(get.status, 410);
  const post = await request(app).post(`/a/${token}`);
  assert.equal(post.status, 410);
});

test('a random unknown token also fails, cleanly', async () => {
  const res = await request(app).get(`/a/${newToken()}`);
  assert.equal(res.status, 410);
});
