#!/usr/bin/env node
// Migrates TEST_DATABASE_URL up to head using the shared/ migrations.
// Refuses if TEST_DATABASE_URL is unset or equals DATABASE_URL.
//
// Run once before `npm test`; individual tests then truncate between cases.
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

function migrate(direction, target) {
  const args = [migrateBin, direction];
  if (target !== undefined) args.push(String(target));
  const result = spawnSync(process.execPath, args, {
    cwd: sharedRoot,
    env: { ...process.env, DATABASE_URL: TEST_URL },
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
    await client.query('drop schema if exists public cascade');
    await client.query('create schema public');
    // photo_user owns the schema so migrations can create objects.
    await client.query(`grant all on schema public to public`);
  } finally {
    await client.end();
  }
}

(async () => {
  console.log(`Resetting public schema on ${TEST_URL}…`);
  await resetPublic();
  console.log('Migrating up to head…');
  migrate('up');
  console.log('Test DB ready.');
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
