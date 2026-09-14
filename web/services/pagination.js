// Keyset pagination — pass the last-seen id as `cursor` to `parseCursor`,
// then use `cursorWhere` in the WHERE clause. Ordering is `id desc` (the
// natural "newest first" for photos, comments, etc.).
//
//   const { limit, cursor } = parseListQuery(req.query);
//   const params = [];
//   let where = ' where 1 = 1 ';
//   if (cursor != null) { params.push(cursor); where += ` and id < $${params.length}`; }
//   params.push(limit);
//   const rows = (await pool.query(`select ... ${where} order by id desc limit $${params.length}`, params)).rows;
//   return { items: rows, next: rows.length === limit ? rows.at(-1).id : null };

function parseListQuery(query = {}, { max = 100, def = 40 } = {}) {
  let limit = parseInt(query.limit, 10);
  if (!Number.isInteger(limit) || limit <= 0) limit = def;
  if (limit > max) limit = max;
  let cursor = null;
  if (query.cursor != null && String(query.cursor).length > 0) {
    const n = parseInt(query.cursor, 10);
    if (Number.isInteger(n) && n > 0) cursor = n;
  }
  return { limit, cursor };
}

module.exports = { parseListQuery };
