// Web-origin id range (Phase 9 fix-up 1).
//
// Desktop-born rows use low ids; rows the web creates in any table
// listed in shared/id-ranges.json get ids ≥ WEB_ID_FLOOR (the web DB's
// sequences start there — see the shared migration
// `phase-9-fixup-1-web-origin-ids`). The sync layer never lets the two
// ranges cross: a desktop push carrying an id ≥ floor is refused, and an
// existing row ≥ floor is never overwritten by a push.

const path = require('path');
const ranges = require(path.join(__dirname, '..', '..', 'shared', 'id-ranges.json'));

const WEB_ID_FLOOR = Number(ranges.web_id_floor);
const WEB_ORIGIN_TABLES = Object.freeze([...ranges.web_origin_tables]);

function isWebOriginId(id) {
  const n = Number(id);
  return Number.isFinite(n) && n >= WEB_ID_FLOOR;
}

// Ids in `items` (by `key`, default 'id') at or above the floor.
function idsAtOrAboveFloor(items, key = 'id') {
  const out = [];
  for (const r of items || []) {
    if (r && isWebOriginId(r[key])) out.push(Number(r[key]));
  }
  return out;
}

// The next value each sequence will hand out. `last_value` + `is_called`
// is how Postgres reports it (is_called=false → last_value is next).
async function sequencePositions(pool) {
  const out = [];
  for (const table of WEB_ORIGIN_TABLES) {
    const seq = (await pool.query(`select pg_get_serial_sequence($1, 'id') as s`, [table])).rows[0].s;
    const { rows } = await pool.query(`select last_value, is_called from ${seq}`);
    const last = Number(rows[0].last_value);
    const next = rows[0].is_called ? last + 1 : last;
    out.push({ table, sequence: seq, next, ok: next >= WEB_ID_FLOOR });
  }
  return out;
}

async function checkIdFloor(pool) {
  const sequences = await sequencePositions(pool);
  return { floor: WEB_ID_FLOOR, ok: sequences.every((s) => s.ok), sequences };
}

// Same SQL as the migration's `up`. Only ever raises a sequence.
async function applyIdFloor(pool) {
  for (const table of WEB_ORIGIN_TABLES) {
    await pool.query(`
      do $$
      declare
        seq text := pg_get_serial_sequence('${table}', 'id');
        nxt bigint;
      begin
        select greatest(${WEB_ID_FLOOR}, coalesce(max(id), 0) + 1) into nxt from ${table};
        execute format('alter sequence %s start with %s restart with %s', seq, ${WEB_ID_FLOOR}, nxt);
      end
      $$;
    `);
  }
}

module.exports = {
  WEB_ID_FLOOR,
  WEB_ORIGIN_TABLES,
  isWebOriginId,
  idsAtOrAboveFloor,
  sequencePositions,
  checkIdFloor,
  applyIdFloor,
};
