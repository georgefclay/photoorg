// "Who is this?" — faces the desktop marked `unknown` (fix-up 8) on
// photos the viewer can see, within the current group scope.

const { visibleSql } = require('./photos');
const { scopeSql } = require('./scope');
const { encodeCursor, decodeCursor, keysetWhere } = require('./cursor');

async function listUnknownFaces(pool, user, { cursor = null, limit = 24, scope = null } = {}) {
  const lim = Math.max(1, Math.min(Number(limit) || 24, 100));
  const params = [user.id];
  const clauses = [
    `f.is_deleted = false`, `f.person_id is null`, `f.review_status = 'unknown'`,
    visibleSql(user, params, 'p'),
  ];
  const sc = scopeSql(scope, params, 'p');
  if (sc) clauses.push(sc);
  const kw = keysetWhere({ col: null }, decodeCursor(cursor), params, { idCol: 'f.id' });
  if (kw) clauses.push(kw);
  params.push(lim);
  const { rows } = await pool.query(
    `select f.id, f.photo_id, f.bbox, p.width, p.height,
            to_char(p.capture_date, 'YYYY-MM-DD') as capture_date, p.capture_date_precision,
            p.capture_date_confirmed,
            exists (select 1 from suggestions s
                     where s.kind = 'person' and s.status = 'pending' and s.user_id = $1
                       and (s.payload->>'face_id')::bigint = f.id) as suggested_by_me
       from faces f join photos p on p.id = f.photo_id
      where ${clauses.join(' and ')}
      order by f.id desc
      limit $${params.length}`,
    params,
  );
  const items = rows.map((r) => ({
    id: Number(r.id),
    photo_id: Number(r.photo_id),
    crop_url: `/media/faces/${r.id}`,
    thumb_url: `/media/thumbs/${r.photo_id}`,
    working_url: `/media/working/${r.photo_id}`,
    bbox: r.bbox,
    width: r.width,
    height: r.height,
    capture_date: r.capture_date,
    capture_date_precision: r.capture_date_precision,
    capture_date_confirmed: r.capture_date_confirmed,
    suggested_by_me: r.suggested_by_me,
  }));
  const last = rows[rows.length - 1];
  return { items, next: rows.length === lim ? encodeCursor(undefined, last.id) : null };
}

module.exports = { listUnknownFaces };
