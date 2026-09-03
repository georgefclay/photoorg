// Smoke test for the Phase 1 schema.
//
// Runs against TEST_DATABASE_URL (a separate DB from DATABASE_URL). The DB
// must already exist and be reachable; the test refuses to start if
// TEST_DATABASE_URL is missing or equals DATABASE_URL. It then:
//   1. Rolls the DB down to zero, then applies every migration.
//   2. Inserts one row into every top-level table.
//   3. Confirms compute_completeness scores are 0 → 40 → 100.
//   4. Rolls all the way down and back up again cleanly.
//
// Run:  npm test

import 'dotenv/config';
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import pg from 'pg';

const { Client } = pg;
const here = dirname(fileURLToPath(import.meta.url));
const sharedRoot = resolve(here, '..');

const TEST_URL = process.env.TEST_DATABASE_URL;
const MAIN_URL = process.env.DATABASE_URL;

if (!TEST_URL) {
  console.error('TEST_DATABASE_URL not set. See shared/.env.example.');
  process.exit(1);
}
if (TEST_URL === MAIN_URL) {
  console.error('TEST_DATABASE_URL must not equal DATABASE_URL. Refusing to run.');
  process.exit(1);
}

const migrateBin = resolve(sharedRoot, 'node_modules/node-pg-migrate/bin/node-pg-migrate.js');

// direction: 'up' | 'down'. target: undefined (one step) | '0' (all the way).
function migrate(direction, target) {
  const args = [migrateBin, direction];
  if (target !== undefined) args.push(String(target));
  const result = spawnSync(process.execPath, args, {
    cwd: sharedRoot,
    env: { ...process.env, DATABASE_URL: TEST_URL },
    encoding: 'utf8',
  });
  if (result.status !== 0) {
    console.error(result.stdout);
    console.error(result.stderr);
    throw new Error(`migrate ${direction} ${target ?? ''} failed (exit ${result.status})`);
  }
  return result.stdout;
}

async function connect() {
  const c = new Client({ connectionString: TEST_URL });
  await c.connect();
  return c;
}

test('smoke: fresh migrate up applies all migrations', async () => {
  migrate('down', 0); // start from empty
  migrate('up');

  const c = await connect();
  try {
    const { rows } = await c.query(
      `select count(*)::int as n from pgmigrations`,
    );
    assert.ok(rows[0].n >= 10, `expected at least 10 migrations, got ${rows[0].n}`);
  } finally {
    await c.end();
  }
});

test('smoke: insert set + completeness 0 → 40 → 100', async () => {
  const c = await connect();
  try {
    await c.query('begin');

    const { rows: [u] } = await c.query(
      `insert into users (email, display_name, role) values ($1, $2, 'admin') returning id`,
      ['smoke@example.com', 'Smoke Test'],
    );

    const { rows: [p] } = await c.query(
      `insert into photos (
         sha256, mime, source_root, source_folder, source_filename
       ) values ('sha-photo-1', 'image/jpeg', 'photos', '_2005-06', 'f0001.jpg')
       returning id`,
    );

    await c.query(
      `insert into photo_masters (photo_id, master_path, sha256, mime, is_preferred)
       values ($1, 'D:/Photos/_2005-06/f0001.jpg', 'sha-master-1', 'image/jpeg', true)`,
      [p.id],
    );

    const { rows: [person] } = await c.query(
      `insert into people (given_name, surname, nickname, maiden_name)
       values ('Margaret', 'Clay', 'Peggy', 'Schmidt') returning id, display_name`,
    );
    assert.equal(
      person.display_name,
      'Margaret "Peggy" Clay (née Schmidt)',
      'display_name trigger should format nickname and maiden name',
    );

    await c.query(
      `insert into faces (photo_id, person_id, bbox, source)
       values ($1, $2, '{"x":10,"y":20,"w":50,"h":50}'::jsonb, 'human')`,
      [p.id, person.id],
    );

    const { rows: [place] } = await c.query(
      `insert into places (name) values ('Ashland, OR') returning id`,
    );
    await c.query(
      `insert into photo_places (photo_id, place_id, confirmed)
       values ($1, $2, true)`,
      [p.id, place.id],
    );

    const { rows: [album] } = await c.query(
      `insert into albums (name, source) values ('Family Reunion 2005', 'manual') returning id`,
    );
    await c.query(
      `insert into album_photos (album_id, photo_id, position) values ($1, $2, 1)`,
      [album.id, p.id],
    );

    await c.query(
      `insert into comments (photo_id, user_id, body) values ($1, $2, 'That is Peggy on the porch.')`,
      [p.id, u.id],
    );
    await c.query(
      `insert into likes (user_id, photo_id) values ($1, $2)`,
      [u.id, p.id],
    );
    await c.query(
      `insert into suggestions (photo_id, user_id, kind, payload, source)
       values ($1, $2, 'description', '{"text":"a woman on a porch"}'::jsonb, 'human')`,
      [p.id, u.id],
    );
    await c.query(
      `insert into audit_log (user_id, actor, action, entity_type, entity_id, new_value)
       values ($1, $2, 'create', 'photo', $3, '{"note":"smoke"}'::jsonb)`,
      [u.id, 'smoke@example.com', p.id],
    );

    // Completeness before confirming the date: no confirmed date → face and
    // place contribute 60.
    const { rows: [c1] } = await c.query(
      `select refresh_completeness($1)::int as score`, [p.id],
    );
    assert.equal(c1.score, 60, 'face(40) + place(20) = 60 before date confirmed');

    // Confirming the date lifts to 100.
    await c.query(
      `update photos set capture_date = '2005-06-15',
                          capture_date_precision = 'exact',
                          capture_date_confirmed = true
        where id = $1`,
      [p.id],
    );
    const { rows: [c2] } = await c.query(
      `select refresh_completeness($1)::int as score`, [p.id],
    );
    assert.equal(c2.score, 100, 'date(40) + face(40) + place(20) = 100');

    // Removing the place and face drops it back to 40 (date only).
    await c.query(`delete from faces where photo_id = $1`, [p.id]);
    await c.query(`delete from photo_places where photo_id = $1`, [p.id]);
    const { rows: [c3] } = await c.query(
      `select refresh_completeness($1)::int as score`, [p.id],
    );
    assert.equal(c3.score, 40, 'date only after removing face and place');

    await c.query('rollback');
  } catch (err) {
    await c.query('rollback').catch(() => {});
    throw err;
  } finally {
    await c.end();
  }
});

test('smoke: down to zero and back up again', () => {
  migrate('down', 0);
  migrate('up');
});
