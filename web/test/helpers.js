// Common test setup. Loads .env, forces test-safe env vars, creates a
// Postgres pool against TEST_DATABASE_URL, and exposes a factory to build
// a fresh app + supertest agent for each test file.
require('dotenv').config();

// Force test-safe environment BEFORE any app module is required.
process.env.NODE_ENV = 'test';
process.env.SESSION_SECRET = process.env.SESSION_SECRET || 'test-secret-do-not-use-in-prod';
process.env.BASE_URL = 'http://127.0.0.1';
process.env.ADMIN_EMAIL = 'admin@example.com';
process.env.SERVICE_TOKEN = 'test-service-token';
delete process.env.POSTMARK_API_KEY; // ensure the in-memory sink is used

const assert = require('node:assert/strict');
const { makePool } = require('../db');

const TEST_URL = process.env.TEST_DATABASE_URL;
const MAIN_URL = process.env.DATABASE_URL;

if (!TEST_URL) {
  throw new Error('TEST_DATABASE_URL not set. Run `npm run test:setup` after creating the DB.');
}
if (TEST_URL === MAIN_URL) {
  throw new Error('TEST_DATABASE_URL must not equal DATABASE_URL. Refusing.');
}

// One pool per Node process. Individual tests share it via the exports.
const pool = makePool(TEST_URL);

// Late require: services and app modules read env at load, so we import
// them AFTER forcing NODE_ENV=test etc. above.
const { createApp } = require('../app');
const email = require('../services/email');
const { sha256Hex, newToken } = require('../services/tokens');

function makeApp() {
  return createApp({ pool });
}

async function truncateAll() {
  await pool.query(
    `truncate table users, access_requests, magic_links, audit_log, "session"
     restart identity cascade`,
  );
  email.clearOutbox();
}

async function insertUser({ email: e, role = 'contributor', status = 'active', displayName = null }) {
  const { rows } = await pool.query(
    `insert into users (email, display_name, role, status)
     values (lower($1), $2, $3, $4)
     returning id, email, display_name, role, status`,
    [e, displayName, role, status],
  );
  return rows[0];
}

async function insertMagicLink({ userId, token, expiresSql = "now() + interval '15 minutes'" }) {
  await pool.query(
    `insert into magic_links (user_id, token_hash, expires_at)
     values ($1, $2, ${expiresSql})`,
    [userId, sha256Hex(token)],
  );
}

async function auditRows(entityType, entityId) {
  const { rows } = await pool.query(
    `select actor, action, previous_value, new_value, created_at
       from audit_log
      where entity_type = $1
        ${entityId != null ? 'and entity_id = $2' : ''}
      order by id asc`,
    entityId != null ? [entityType, entityId] : [entityType],
  );
  return rows;
}

// Grab the _csrf hidden input out of an HTML response body. Returns null
// if the page has no form.
function extractCsrf(body) {
  const m = /name="_csrf"\s+value="([a-f0-9]+)"/i.exec(body);
  return m ? m[1] : null;
}

module.exports = {
  pool,
  makeApp,
  truncateAll,
  insertUser,
  insertMagicLink,
  auditRows,
  extractCsrf,
  email,
  newToken,
  sha256Hex,
  assert,
};
