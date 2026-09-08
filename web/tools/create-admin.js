#!/usr/bin/env node
// One-time bootstrap: create the first admin.
// Usage:  node tools/create-admin.js <email> [--name "Display Name"]
//
// - Refuses if a user already exists with that email.
// - Does NOT send an email; the new admin signs in via /login like anyone
//   else once the server is up.
require('dotenv').config();

const { makePool } = require('../db');

function parseArgs(argv) {
  const args = { email: null, name: null };
  const rest = argv.slice(2);
  for (let i = 0; i < rest.length; i++) {
    const a = rest[i];
    if (a === '--name') { args.name = rest[++i]; continue; }
    if (!args.email) { args.email = a; continue; }
  }
  return args;
}

async function main() {
  const { email: raw, name } = parseArgs(process.argv);
  if (!raw) {
    console.error('Usage: node tools/create-admin.js <email> [--name "Display Name"]');
    process.exit(2);
  }
  const email = raw.trim().toLowerCase();
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
    console.error(`Not a valid email: ${raw}`);
    process.exit(2);
  }
  const displayName = (name && name.trim()) || email.split('@')[0];

  const pool = makePool();
  try {
    const existing = await pool.query(`select id, role, status from users where email = $1`, [email]);
    if (existing.rowCount > 0) {
      console.error(`User already exists for ${email}: id=${existing.rows[0].id} role=${existing.rows[0].role} status=${existing.rows[0].status}`);
      console.error('Refusing to touch it. Promote via the admin UI if needed.');
      process.exit(1);
    }
    const client = await pool.connect();
    try {
      await client.query('begin');
      const { rows } = await client.query(
        `insert into users (email, display_name, role, status)
         values ($1, $2, 'admin', 'active')
         returning id, email, role, status`,
        [email, displayName],
      );
      const user = rows[0];
      await client.query(
        `insert into audit_log (user_id, actor, action, entity_type, entity_id, new_value)
         values ($1, $2, $3, $4, $5, $6)`,
        [null, 'bootstrap', 'user.create', 'users', user.id, { email: user.email, role: user.role, via: 'create-admin.js' }],
      );
      await client.query('commit');
      console.log(`Created admin user id=${user.id} email=${user.email}`);
      console.log('Now start the server and sign in via /login.');
    } catch (err) {
      await client.query('rollback').catch(() => {});
      throw err;
    } finally {
      client.release();
    }
  } finally {
    await pool.end();
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
