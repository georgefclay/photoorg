const test = require('node:test');
const { after, before, beforeEach } = require('node:test');
const request = require('supertest');
const { pool, makeApp, truncateAll, assert } = require('./helpers');

let app;
before(() => { app = makeApp(); });
beforeEach(async () => { await truncateAll(); });
after(async () => { await pool.end(); });

test('GET /service/ping rejects a missing bearer token with 401', async () => {
  const res = await request(app).get('/service/ping');
  assert.equal(res.status, 401);
});

test('GET /service/ping rejects a wrong bearer token with 401', async () => {
  const res = await request(app).get('/service/ping').set('Authorization', 'Bearer wrong-token');
  assert.equal(res.status, 401);
});

test('GET /service/ping accepts the configured SERVICE_TOKEN', async () => {
  const res = await request(app)
    .get('/service/ping')
    .set('Authorization', `Bearer ${process.env.SERVICE_TOKEN}`);
  assert.equal(res.status, 200);
  assert.deepEqual(res.body, { ok: true, service: true });
});
