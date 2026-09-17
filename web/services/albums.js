// Albums are site-wide and read-only on the web in Phase 10 (answer C5).
// Counts are visibility- and scope-filtered; the album's photo grid is
// photos.listPhotos({ filters: { album_id }, sort: 'position' }).

const { visibleSql } = require('./photos');
const { scopeSql } = require('./scope');

async function listAlbums(pool, user, scope) {
  const params = [];
  const where = [visibleSql(user, params, 'ph'), scopeSql(scope, params, 'ph')].filter(Boolean).join(' and ');
  const { rows } = await pool.query(
    `select a.id, a.name, a.description, a.source,
            st.photo_count, st.cover_id
       from albums a
       cross join lateral (
         select count(*)::int as photo_count,
                (array_agg(ph.id order by ap.position asc nulls last, ph.id desc)
                  filter (where ph.synced_file_version is not null))[1] as cover_id
           from album_photos ap join photos ph on ph.id = ap.photo_id
          where ap.album_id = a.id and ${where}
       ) st
      where a.is_deleted = false
      order by lower(a.name)`,
    params,
  );
  return rows.map((r) => ({
    id: Number(r.id), name: r.name, description: r.description, source: r.source,
    photo_count: r.photo_count,
    cover_thumb_url: r.cover_id ? `/media/thumbs/${r.cover_id}` : null,
  }));
}

async function getAlbum(pool, id) {
  const { rows } = await pool.query(
    `select id, name, description, source, created_at from albums where id = $1 and is_deleted = false`, [id],
  );
  return rows[0] ? { ...rows[0], id: Number(rows[0].id) } : null;
}

module.exports = { listAlbums, getAlbum };
