const test = require('node:test');
const { after, before, beforeEach } = require('node:test');
const request = require('supertest');
const { pool, makeApp, truncateAll, assert } = require('./helpers');

// Fresh app per test file so the express-rate-limit in-memory store starts empty.
let app;
before(() => { app = makeApp(); });
beforeEach(async () => { await truncateAll(); });
after(async () => { await pool.end(); });

test('the 6th POST /login within the window is 429', async () => {
  for (let i = 0; i < 5; i++) {
    const res = await request(app)
      .post('/login')
      .type('form')
      .send({ email: `rl${i}@example.com` });
    assert.notEqual(res.status, 429, `hit ${i + 1} should be allowed`);
  }
  const sixth = await request(app)
    .post('/login')
    .type('form')
    .send({ email: 'rl6@example.com' });
  assert.equal(sixth.status, 429);
  assert.match(sixth.text, /Too many/i);
});

test('the 6th POST /request-access within the window is 429', async () => {
  for (let i = 0; i < 5; i++) {
    const res = await request(app)
      .post('/request-access')
      .type('form')
      .send({ email: `ra${i}@example.com` });
    assert.notEqual(res.status, 429);
  }
  const sixth = await request(app)
    .post('/request-access')
    .type('form')
    .send({ email: 'ra6@example.com' });
  assert.equal(sixth.status, 429);
});
