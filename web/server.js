require('dotenv').config();

const { createApp } = require('./app');
const { makePool } = require('./db');
const { checkIdFloor } = require('./services/id-floor');
const downloads = require('./services/downloads');

const PORT = process.env.PORT || 8090;

const pool = makePool();

(async () => {
  // Phase 9 fix-up 1: a web DB whose sequences sit below the web-origin
  // floor would hand out ids that collide with desktop-pushed rows.
  // Production refuses to start; dev/test only warn (the desktop pytest
  // fixture deliberately runs the web on a desktop-shaped test DB).
  try {
    const r = await checkIdFloor(pool);
    if (!r.ok) {
      const low = r.sequences.filter((s) => !s.ok).map((s) => s.table).join(', ');
      const msg = `[web] id floor NOT applied (${low}). Run: node tools/id-floor.js --apply (web DB only).`;
      if (process.env.NODE_ENV === 'production') {
        console.error(msg);
        process.exit(1);
      }
      console.warn(msg);
    }
  } catch (err) {
    console.warn('[web] id floor check failed:', err.message || err);
    if (process.env.NODE_ENV === 'production') process.exit(1);
  }

  const app = createApp({ pool });
  app.listen(PORT, () => {
    console.log(`Photo Archive web listening on http://localhost:${PORT}`);
  });

  // Phase 12: this process's zip workers died with the last one, so any
  // `running` download is an orphan — the sweep fails it and tells its
  // owner (the same sweep cron runs hourly). Then start whatever is
  // queued, and look again every few minutes in case a kick was missed.
  try {
    const r = await downloads.sweep(pool, { startup: true });
    if (r.failed || r.expired) console.log(`[downloads] startup sweep ${JSON.stringify(r)}`);
    await downloads.kick(pool);
  } catch (err) {
    console.error('[downloads] startup sweep failed', err);
  }
  setInterval(() => {
    downloads.kick(pool).catch((e) => console.error('[downloads] kick', e));
  }, 5 * 60 * 1000).unref();
})();
