// Phase 9 fix-up 1 — web-origin id range.
//
// Desktop-born rows keep the low ids they get on the laptop; the sync
// pushes them to the VM with those ids (explicit ids never advance a
// sequence). Rows the web itself creates (contributor faces, suggestions,
// people created at accept, …) must never collide with a desktop id, so
// on a WEB database the sequences for every table in
// `shared/id-ranges.json` start at `web_id_floor` (1e12 — every one of
// those tables is bigint/bigserial).
//
// The gate is explicit: this migration only moves sequences when
// PHOTOORG_DB_ROLE=web is in the environment of `npm run migrate:up`.
// On the desktop DB (photoorg) the variable is absent and the migration
// is a recorded no-op — it must never raise the laptop's sequences, or
// every new desktop face / person would land above the floor and the
// push would refuse it. The web server checks the sequences on startup
// (services/id-floor.js) and refuses to run in production when a web
// DB was migrated without the gate; `node tools/id-floor.js --apply`
// in web/ applies the same SQL after the fact.
//
// `alter sequence … start with FLOOR restart with …` (not setval) so a
// `truncate … restart identity` in tests also lands back on the floor.

import fs from 'node:fs';

const ranges = JSON.parse(
  fs.readFileSync(new URL('../id-ranges.json', import.meta.url), 'utf8'),
);

export const shorthands = undefined;

function isWebRole() {
  const role = (process.env.PHOTOORG_DB_ROLE || '').trim().toLowerCase();
  if (role && role !== 'web' && role !== 'desktop') {
    throw new Error(`PHOTOORG_DB_ROLE must be 'web' or 'desktop' (got '${role}')`);
  }
  return role === 'web';
}

export const up = (pgm) => {
  if (!isWebRole()) {
    pgm.sql(`select 1 /* phase-9-fixup-1: not a web DB (PHOTOORG_DB_ROLE != web); sequences untouched */`);
    return;
  }
  const floor = ranges.web_id_floor;
  for (const table of ranges.web_origin_tables) {
    pgm.sql(`
      do $$
      declare
        seq text := pg_get_serial_sequence('${table}', 'id');
        nxt bigint;
      begin
        select greatest(${floor}, coalesce(max(id), 0) + 1) into nxt from ${table};
        execute format('alter sequence %s start with %s restart with %s', seq, ${floor}, nxt);
      end
      $$;
    `);
  }
};

export const down = (pgm) => {
  if (!isWebRole()) {
    pgm.sql(`select 1 /* phase-9-fixup-1 down: not a web DB; nothing to undo */`);
    return;
  }
  // Only the START value goes back; the current position is left alone
  // (lowering it could hand out an id that already exists).
  for (const table of ranges.web_origin_tables) {
    pgm.sql(`
      do $$
      begin
        execute format('alter sequence %s start with 1', pg_get_serial_sequence('${table}', 'id'));
      end
      $$;
    `);
  }
};
