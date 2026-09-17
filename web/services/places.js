// Places autocomplete (for "Suggest a place"). Places are site-wide.

async function autocompletePlaces(pool, q) {
  const text = String(q || '').trim().slice(0, 100);
  if (!text) return [];
  const prefix = `${text.toLowerCase().replace(/[%_\\]/g, '')}%`;
  const contains = `%${text.toLowerCase().replace(/[%_\\]/g, '')}%`;
  const { rows } = await pool.query(
    `select id, name,
            case when lower(name) like $1 then 0
                 when lower(name) like $2 then 1
                 else 2 end as rank
       from places
      where is_deleted = false
        and (lower(name) like $2 or lower(name) % $3)
      order by rank, similarity(lower(name), $3) desc, name
      limit 10`,
    [prefix, contains, text.toLowerCase()],
  );
  return rows.map((r) => ({ id: Number(r.id), name: r.name }));
}

module.exports = { autocompletePlaces };
