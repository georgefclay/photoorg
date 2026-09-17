#!/usr/bin/env node
// Check (default) or apply the web-origin id floor on DATABASE_URL.
//
//   node tools/id-floor.js           # report sequence positions
//   node tools/id-floor.js --apply   # raise any sequence below the floor
//
// Only for a WEB database (the VM's photoorg, the laptop's photoorg_web).
// Never run --apply against the desktop's photoorg: new desktop faces and
// people would get ids above the floor and the push would refuse them.
require('dotenv').config();
const { makePool } = require('../db');
const { checkIdFloor, applyIdFloor } = require('../services/id-floor');

(async () => {
  const pool = makePool();
  try {
    const db = (await pool.query('select current_database() as name')).rows[0].name;
    if (db === 'photoorg' && !process.env.PHOTOORG_DB_ROLE) {
      // The laptop desktop DB is also named photoorg; the VM sets the role.
      console.warn(`DB is named 'photoorg'. If this is the laptop desktop DB, stop. Set PHOTOORG_DB_ROLE=web to confirm a web DB.`);
      if (process.argv.includes('--apply')) process.exit(2);
    }
    if (process.argv.includes('--apply')) {
      if ((process.env.PHOTOORG_DB_ROLE || '').toLowerCase() === 'desktop') {
        console.error('PHOTOORG_DB_ROLE=desktop — refusing to apply the web id floor.');
        process.exit(2);
      }
      await applyIdFloor(pool);
      console.log(`Applied on ${db}.`);
    }
    const r = await checkIdFloor(pool);
    console.log(`${db}: floor ${r.floor} — ${r.ok ? 'OK' : 'BELOW FLOOR'}`);
    for (const s of r.sequences) console.log(`  ${s.ok ? 'ok ' : 'LOW'} ${s.table.padEnd(22)} next=${s.next}`);
    process.exit(r.ok ? 0 : 1);
  } finally {
    await pool.end().catch(() => {});
  }
})().catch((err) => { console.error(err); process.exit(1); });
