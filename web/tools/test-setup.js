#!/usr/bin/env node
// Migrates TEST_DATABASE_URL up to head using the shared/ migrations.
// Refuses if TEST_DATABASE_URL is unset or equals DATABASE_URL.
//
// Run once before `npm test`; individual tests then truncate between cases.
//
// TEST_DB_SCHEMA=<name> (optional) resets and migrates that schema instead
// of `public`, so several test runs can share photoorg_test in parallel
// (helpers.js points the pool's search_path at the same schema).
require('dotenv').config();

const path = require('path');
const { spawnSync } = require('child_process');
const { Client } = require('pg');

const TEST_URL = process.env.TEST_DATABASE_URL;
const MAIN_URL = process.env.DATABASE_URL;

if (!TEST_URL) {
  console.error('TEST_DATABASE_URL not set. See web/.env.example.');
  process.exit(1);
}
if (TEST_URL === MAIN_URL) {
  console.error('TEST_DATABASE_URL must not equal DATABASE_URL. Refusing.');
  process.exit(1);
}

const sharedRoot = path.resolve(__dirname, '..', '..', 'shared');
const migrateBin = path.resolve(
  sharedRoot,
  'node_modules',
  'node-pg-migrate',
  'bin',
  'node-pg-migrate.js',
);

const SCHEMA = (process.env.TEST_DB_SCHEMA || '').trim();
if (SCHEMA && !/^[a-z_][a-z0-9_]*$/.test(SCHEMA)) {
  console.error('TEST_DB_SCHEMA must match [a-z_][a-z0-9_]*');
  process.exit(1);
}

function migrate(direction, target) {
  const args = [migrateBin, direction];
  if (target !== undefined) args.push(String(target));
  if (SCHEMA) args.push('-s', SCHEMA, '-s', 'public', '--migrations-schema', SCHEMA);
  const result = spawnSync(process.execPath, args, {
    cwd: sharedRoot,
    env: { ...process.env, DATABASE_URL: TEST_URL, PHOTOORG_DB_ROLE: 'web' },
    encoding: 'utf8',
    stdio: 'inherit',
  });
  if (result.status !== 0) {
    console.error(`migrate ${direction} ${target ?? ''} failed (exit ${result.status})`);
    process.exit(result.status || 1);
  }
}

// Reset by dropping the public schema outright. Cheaper and more robust
// than `down 0` (which fails if stale data leaves a column NOT NULL happy
// but a downgrade unhappy). Then migrate up to head.
async function resetPublic() {
  const client = new Client({ connectionString: TEST_URL });
  await client.connect();
  try {
    if (SCHEMA) {
      await client.query(`drop schema if exists ${SCHEMA} cascade`);
      await client.query(`create schema ${SCHEMA}`);
      // Extensions live in public; make sure they exist there first.
      await client.query('create extension if not exists pg_trgm with schema public');
      await client.query('create extension if not exists fuzzystrmatch with schema public');
      return;
    }
    await client.query('drop schema if exists public cascade');
    await client.query('create schema public');
    // photo_user owns the schema so migrations can create objects.
    await client.query(`grant all on schema public to public`);
  } finally {
    await client.end();
  }
}

(async () => {
  console.log(`Resetting ${SCHEMA || 'public'} schema on ${TEST_URL}…`);
  await resetPublic();
  console.log('Migrating up to head…');
  migrate('up');
  console.log('Test DB ready.');
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
