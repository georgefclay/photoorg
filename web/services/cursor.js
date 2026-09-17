// Composite keyset cursors: `(sort_value, id)` for every sort.
//
// Encoded as a short URL-safe string: `<value>~<id>` (`~<id>` when the
// sort value is null; a bare `<id>` is accepted for id-only orderings and
// for old clients). Values are the literal text of the sort key: an
// integer, a `YYYY-MM-DD` date, or a lower-cased name.
//
//   const c = decodeCursor(req.query.cursor)      // { v, id } | null
//   const { sql, params } = keysetWhere(sortDef, c, params)
//   next = rows.length === limit ? encodeCursor(last.sv, last.id) : null
//
// A sort definition is `{ col, dir: 'asc'|'desc', cast }` where `col` is
// the SQL expression the rows are ordered by (NULLS LAST) and ties break
// on `id desc`. `cast` is the Postgres type for the cursor value
// (`int`, `date`, `text`); id-only sorts use `col: null`.

function encodeCursor(value, id) {
  if (id == null) return null;
  if (value === undefined) return String(id);
  if (value === null) return `~${id}`;
  let v = value;
  if (v instanceof Date) v = v.toISOString().slice(0, 10);
  return `${encodeURIComponent(String(v))}~${id}`;
}

function decodeCursor(raw) {
  if (raw == null) return null;
  const s = String(raw).trim();
  if (!s) return null;
  if (/^\d+$/.test(s)) return { v: undefined, id: Number(s) };
  const i = s.lastIndexOf('~');
  if (i < 0) return null;
  const id = Number(s.slice(i + 1));
  if (!Number.isSafeInteger(id) || id <= 0) return null;
  const rawV = s.slice(0, i);
  let v;
  try { v = rawV === '' ? null : decodeURIComponent(rawV); } catch { return null; }
  return { v, id };
}

// ORDER BY for a sort, forward or reversed (reversed is used to find the
// previous item for prev/next navigation).
function orderBy(sort, { reverse = false, idCol = 'id' } = {}) {
  if (!sort.col) return `order by ${idCol} ${reverse ? 'asc' : 'desc'}`;
  const dir = reverse ? (sort.dir === 'asc' ? 'desc' : 'asc') : sort.dir;
  const nulls = reverse ? 'nulls first' : 'nulls last';
  return `order by ${sort.col} ${dir} ${nulls}, ${idCol} ${reverse ? 'asc' : 'desc'}`;
}

// WHERE fragment selecting rows strictly after (or, with reverse, strictly
// before) the cursor in `orderBy(sort)` order. Pushes onto `params`.
function keysetWhere(sort, cursor, params, { reverse = false, idCol = 'id' } = {}) {
  if (!cursor) return null;
  params.push(cursor.id);
  const pid = `$${params.length}`;
  if (!sort.col || cursor.v === undefined) {
    return reverse ? `${idCol} > ${pid}` : `${idCol} < ${pid}`;
  }
  const col = sort.col;
  if (cursor.v === null) {
    // Cursor sits in the trailing NULL block.
    return reverse
      ? `(${col} is not null or (${col} is null and ${idCol} > ${pid}))`
      : `(${col} is null and ${idCol} < ${pid})`;
  }
  params.push(cursor.v);
  const pv = `$${params.length}::${sort.cast || 'text'}`;
  const beyond = sort.dir === 'asc' ? '>' : '<';
  const before = sort.dir === 'asc' ? '<' : '>';
  if (reverse) {
    return `(${col} ${before} ${pv} or (${col} = ${pv} and ${idCol} > ${pid}))`;
  }
  return `(${col} ${beyond} ${pv} or (${col} = ${pv} and ${idCol} < ${pid}) or ${col} is null)`;
}

module.exports = { encodeCursor, decodeCursor, orderBy, keysetWhere };
