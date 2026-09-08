const test = require('node:test');
const { after, before, beforeEach } = require('node:test');
const request = require('supertest');
const {
  pool, makeApp, truncateAll, email, auditRows, assert,
} = require('./helpers');

let app;
before(() => { app = makeApp(); });
beforeEach(async () => { await truncateAll(); });
after(async () => { await pool.end(); });

async function createAccessRequest() {
  await request(app)
    .post('/request-access')
    .type('form')
    .send({ email: 'newperson@example.com', display_name: 'New Person' });
  const { rows } = await pool.query(`select id, token from access_requests order by id desc limit 1`);
  return rows[0];
}

test('GET on approve token renders a confirm page and does not change anything', async () => {
  const ar = await createAccessRequest();
  const outboxBefore = email.outbox.length;

  const res = await request(app).get(`/admin/access/${ar.token}/approve`);
  assert.equal(res.status, 200);
  assert.match(res.text, /Approve/);
  assert.match(res.text, /newperson@example\.com/);

  // The request is still pending; no user was created; no new emails were sent.
  const rowNow = (await pool.query(`select status from access_requests where id = $1`, [ar.id])).rows[0];
  assert.equal(rowNow.status, 'pending');
  const userCount = (await pool.query(`select count(*)::int as n from users`)).rows[0].n;
  assert.equal(userCount, 0);
  assert.equal(email.outbox.length, outboxBefore);
});

test('POST on approve token creates the user, marks approved, and emails a first magic link', async () => {
  const ar = await createAccessRequest();

  const res = await request(app).post(`/admin/access/${ar.token}/approve`);
  assert.equal(res.status, 200);
  assert.match(res.text, /Approved/);

  const arNow = (await pool.query(`select status from access_requests where id = $1`, [ar.id])).rows[0];
  assert.equal(arNow.status, 'approved');

  const { rows: users } = await pool.query(
    `select id, email, display_name, role, status from users`,
  );
  assert.equal(users.length, 1);
  assert.equal(users[0].email, 'newperson@example.com');
  assert.equal(users[0].display_name, 'New Person');
  assert.equal(users[0].role, 'contributor');
  assert.equal(users[0].status, 'active');

  // Magic link was created and welcome email sent.
  const mlCount = (await pool.query(`select count(*)::int as n from magic_links`)).rows[0].n;
  assert.equal(mlCount, 1);
  const welcome = email.outbox.filter((m) => m.to === 'newperson@example.com');
  assert.equal(welcome.length, 1);
  assert.match(welcome[0].subject, /You now have access/);
  assert.match(welcome[0].text, /\/a\/[a-f0-9]{64}/);

  // Audit: approve + user.create + magic_link.sent for that user.
  const arAudit = await auditRows('access_request', ar.id);
  assert.deepEqual(arAudit.map((r) => r.action), ['auth.request_access', 'auth.approve']);
  const userAudit = await auditRows('users', users[0].id);
  const kinds = userAudit.map((r) => r.action);
  assert.ok(kinds.includes('user.create'));
  assert.ok(kinds.includes('auth.magic_link.sent'));
});

test('POST on deny token marks denied and does not email the requester', async () => {
  const ar = await createAccessRequest();
  const before = email.outbox.length;

  const res = await request(app).post(`/admin/access/${ar.token}/deny`);
  assert.equal(res.status, 200);

  const arNow = (await pool.query(`select status from access_requests where id = $1`, [ar.id])).rows[0];
  assert.equal(arNow.status, 'denied');
  const userCount = (await pool.query(`select count(*)::int as n from users`)).rows[0].n;
  assert.equal(userCount, 0);

  // No new email besides the earlier admin notification.
  const toRequester = email.outbox.filter((m) => m.to === 'newperson@example.com');
  assert.equal(toRequester.length, 0);
  assert.equal(email.outbox.length, before);
});

test('replaying an approve token after use renders the expired page', async () => {
  const ar = await createAccessRequest();
  const first = await request(app).post(`/admin/access/${ar.token}/approve`);
  assert.equal(first.status, 200);
  const replay = await request(app).post(`/admin/access/${ar.token}/approve`);
  assert.equal(replay.status, 410);
  assert.match(replay.text, /expired/i);
});
