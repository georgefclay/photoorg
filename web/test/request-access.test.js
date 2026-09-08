const test = require('node:test');
const { after, before, beforeEach } = require('node:test');
const request = require('supertest');
const {
  pool, makeApp, truncateAll, email, auditRows, assert, extractCsrf,
} = require('./helpers');

let app;
before(() => { app = makeApp(); });
beforeEach(async () => { await truncateAll(); });
after(async () => { await pool.end(); });

test('POST /request-access inserts a row and emails the admin exactly once', async () => {
  const res = await request(app)
    .post('/request-access')
    .type('form')
    .send({ email: 'Jane@Example.com', display_name: 'Jane', message: 'hi' });
  assert.equal(res.status, 200);
  assert.match(res.text, /Thanks/);

  const { rows } = await pool.query(
    `select id, email, display_name, message, status, token, token_expires_at
       from access_requests order by id`,
  );
  assert.equal(rows.length, 1);
  assert.equal(rows[0].email, 'jane@example.com', 'email is stored lowercased');
  assert.equal(rows[0].display_name, 'Jane');
  assert.equal(rows[0].message, 'hi');
  assert.equal(rows[0].status, 'pending');
  assert.match(rows[0].token, /^[a-f0-9]{64}$/);
  assert.ok(rows[0].token_expires_at > new Date(), 'token_expires_at is in the future');

  // Exactly one email to ADMIN_EMAIL, with both action URLs.
  const admin = email.outbox.filter((m) => m.to === 'admin@example.com');
  assert.equal(admin.length, 1);
  assert.match(admin[0].subject, /^\[Photo Archive\] Access request from jane@example\.com$/);
  assert.match(admin[0].text, /\/admin\/access\/[a-f0-9]{64}\/approve/);
  assert.match(admin[0].text, /\/admin\/access\/[a-f0-9]{64}\/deny/);

  const audit = await auditRows('access_request', rows[0].id);
  assert.equal(audit.length, 1);
  assert.equal(audit[0].action, 'auth.request_access');
  assert.equal(audit[0].actor, 'jane@example.com');
});

test('duplicate pending request is silent — no second admin email', async () => {
  await request(app).post('/request-access').type('form').send({ email: 'dup@example.com' });
  await request(app).post('/request-access').type('form').send({ email: 'dup@example.com' });

  const { rows } = await pool.query(`select count(*)::int as n from access_requests`);
  assert.equal(rows[0].n, 1);
  const admin = email.outbox.filter((m) => m.to === 'admin@example.com');
  assert.equal(admin.length, 1, 'only one admin email for the two requests');
});

test('honeypot triggers a silent fake success and inserts nothing', async () => {
  const res = await request(app)
    .post('/request-access')
    .type('form')
    .send({ email: 'bot@example.com', website: 'http://spam' });
  assert.equal(res.status, 200);
  assert.match(res.text, /Thanks/);

  const { rows } = await pool.query(`select count(*)::int as n from access_requests`);
  assert.equal(rows[0].n, 0);
  assert.equal(email.outbox.length, 0);
});

test('invalid email is rejected with 400 and the form redisplayed', async () => {
  const res = await request(app)
    .post('/request-access')
    .type('form')
    .send({ email: 'not-an-email' });
  assert.equal(res.status, 400);
  assert.match(res.text, /valid email/i);
  const { rows } = await pool.query(`select count(*)::int as n from access_requests`);
  assert.equal(rows[0].n, 0);
});

test('GET /request-access renders a form with a CSRF token', async () => {
  const res = await request(app).get('/request-access');
  assert.equal(res.status, 200);
  const token = extractCsrf(res.text);
  assert.ok(token && /^[a-f0-9]{64}$/.test(token), 'CSRF token present');
});
