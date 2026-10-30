#!/usr/bin/env node
// Phase 12 — hourly from cron (ops/vm/cron-fragment.txt): expire finished
// downloads past their 72 hours (the zip is deleted, the row stays as
// `expired`) and fail any `running` job whose worker process is gone.
// The server runs the same sweep at start (services/downloads.js sweep).
require('dotenv').config();
const { makePool } = require('../db');
const dl = require('../services/downloads');

const pool = makePool();
dl.sweep(pool)
  .then(async (r) => {
    if (r.failed || r.expired) console.log(`[download-sweep] ${new Date().toISOString()} ${JSON.stringify(r)}`);
    await pool.end();
  })
  .catch(async (err) => {
    console.error('[download-sweep] failed', err);
    await pool.end().catch(() => {});
    process.exit(1);
  });
