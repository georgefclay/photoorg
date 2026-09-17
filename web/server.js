require('dotenv').config();

const { createApp } = require('./app');
const { makePool } = require('./db');
const { checkIdFloor } = require('./services/id-floor');

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
})();
