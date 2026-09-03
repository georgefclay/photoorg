// Loads shared/seed/nicknames.csv into the nickname_dictionary table.
// Idempotent: uses INSERT ... ON CONFLICT (lower(canonical), lower(variant)) DO NOTHING.
// Run:  node seed/nicknames.js
//
// This is a dictionary shared by Phase 11 search. It is NOT per-person data;
// person-specific variants live in person_name_variants.

import 'dotenv/config';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import pg from 'pg';

const { Client } = pg;
const here = dirname(fileURLToPath(import.meta.url));
const csvPath = join(here, 'nicknames.csv');

function parseCsv(text) {
  const rows = [];
  for (const line of text.split(/\r?\n/)) {
    if (!line || line.startsWith('#') || line.startsWith('canonical,')) continue;
    const [canonical, variant] = line.split(',');
    if (canonical && variant) rows.push([canonical.trim(), variant.trim()]);
  }
  return rows;
}

async function main() {
  const url = process.env.DATABASE_URL;
  if (!url) {
    console.error('DATABASE_URL not set. Check shared/.env');
    process.exit(1);
  }
  const rows = parseCsv(readFileSync(csvPath, 'utf8'));
  console.log(`Loading ${rows.length} rows into nickname_dictionary...`);

  const client = new Client({ connectionString: url });
  await client.connect();
  try {
    await client.query('begin');
    let inserted = 0;
    for (const [canonical, variant] of rows) {
      const r = await client.query(
        `insert into nickname_dictionary (canonical, variant)
         values ($1, $2)
         on conflict (lower(canonical), lower(variant)) do nothing`,
        [canonical, variant],
      );
      inserted += r.rowCount ?? 0;
    }
    await client.query('commit');
    const { rows: [{ count }] } =
      await client.query('select count(*)::int as count from nickname_dictionary');
    console.log(`Inserted ${inserted} new rows. Table now holds ${count}.`);
  } catch (err) {
    await client.query('rollback');
    throw err;
  } finally {
    await client.end();
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
