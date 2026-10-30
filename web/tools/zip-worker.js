#!/usr/bin/env node
// Phase 12 — builds one background download: `node tools/zip-worker.js <job id>`.
//
// Spawned by services/downloads.js kick() once the job row is `running`,
// so the API never blocks on a 28 GB zip. Writes
// PHOTO_DIR/downloads/<id>.zip via a .part file, marks the row done with
// `expires_at = now() + interval '72 hours'` (SQL), audits and emails.
// Any failure marks the row failed and emails the reason.
require('dotenv').config();
const { makePool } = require('../db');
const dl = require('../services/downloads');

const id = Number(process.argv[2]);
if (!Number.isSafeInteger(id) || id <= 0) {
  console.error('usage: node tools/zip-worker.js <job id>');
  process.exit(2);
}

const pool = makePool(process.env.DOWNLOAD_WORKER_DATABASE_URL || process.env.DATABASE_URL);
// Tests only: wait until this file exists before starting, so a test can
// prove the API answers a page request while a job is genuinely running.
async function holdForTests() {
  const f = process.env.DOWNLOAD_WORKER_HOLD_FILE;
  if (!f) return;
  const fs = require('fs');
  const until = Date.now() + 30000;
  while (!fs.existsSync(f) && Date.now() < until) await new Promise((r) => setTimeout(r, 50));
}

(async () => {
  await holdForTests();
  const started = Date.now();
  const r = await dl.runJob(pool, id);
  console.log(`[zip-worker] job ${id} ${JSON.stringify(r)} in ${((Date.now() - started) / 1000).toFixed(1)} s`);
  await pool.end();
  process.exit(r.failed ? 1 : 0);
})().catch(async (err) => {
  console.error(`[zip-worker] job ${id} crashed`, err);
  try { await dl.failJob(pool, { id }, err.message || String(err)); } catch { /* the sweep will */ }
  process.exit(1);
});
