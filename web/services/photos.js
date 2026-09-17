// Photo queries shared by the JSON API and the server-rendered pages.
// Visibility (middleware/visibility.js) and the group scope
// (services/scope.js) are applied HERE, and only here — routes and views
// never query photos themselves.

const { photoVisibleSql, assertPhotoVisible } = require('../middleware/visibility');
const { scopeSql } = require('./scope');
const { encodeCursor, decodeCursor, orderBy, keysetWhere } = require('./cursor');

const SORTS = {
  recent:     { col: null,                 label: 'Recently added' },
  liked:      { col: 'like_count',         dir: 'desc', cast: 'int',  label: 'Most liked' },
  incomplete: { col: 'completeness_score', dir: 'asc',  cast: 'int',  label: 'Least complete' },
  oldest:     { col: 'capture_date',       dir: 'asc',  cast: 'date', label: 'Oldest' },
  newest:     { col: 'capture_date',       dir: 'desc', cast: 'date', label: 'Newest by date' },
  // Album order; only meaningful with an album_id filter.
  position:   { col: 'album_position',     dir: 'asc',  cast: 'int',  label: 'Album order' },
};
const BROWSE_SORTS = ['recent', 'liked', 'incomplete', 'oldest', 'newest'];

function toInt(v) {
  if (v == null || v === '') return null;
  const n = Number(v);
  return Number.isSafeInteger(n) ? n : null;
}
function truthy(v) { return v === true || v === 'true' || v === '1' || v === 'on'; }

function visibleSql(user, params, alias = 'p') {
  if (user.role === 'admin') return photoVisibleSql(user, { alias, paramIndex: 0 });
  params.push(user.id);
  return photoVisibleSql(user, { alias, paramIndex: params.length });
}

// Query-string → filter object. Unknown keys are ignored.
function parseFilters(q = {}) {
  const text = String(q.q || '').trim().slice(0, 200);
  return {
    year: toInt(q.year),
    decade: toInt(q.decade),
    year_from: toInt(q.year_from),
    year_to: toInt(q.year_to),
    person_id: toInt(q.person_id),
    place_id: toInt(q.place_id),
    album_id: toInt(q.album_id),
    has_no_date: truthy(q.has_no_date),
    has_untagged_faces: truthy(q.has_untagged_faces),
    has_unknown_faces: truthy(q.has_unknown_faces),
    low_completeness: truthy(q.low_completeness),
    q: text || null,
  };
}

function filtersSql(f, params, a = 'p') {
  const c = [];
  const year = `extract(year from ${a}.capture_date)`;
  if (f.year != null) { params.push(f.year); c.push(`${year} = $${params.length}`); }
  if (f.decade != null) {
    params.push(f.decade, f.decade + 9);
    c.push(`${year} between $${params.length - 1} and $${params.length}`);
  }
  if (f.year_from != null) { params.push(f.year_from); c.push(`${year} >= $${params.length}`); }
  if (f.year_to != null) { params.push(f.year_to); c.push(`${year} <= $${params.length}`); }
  if (f.person_id != null) {
    params.push(f.person_id);
    c.push(`exists (select 1 from faces f where f.photo_id = ${a}.id and f.person_id = $${params.length}
                      and f.is_deleted = false and f.is_disputed = false)`);
  }
  if (f.place_id != null) {
    params.push(f.place_id);
    c.push(`exists (select 1 from photo_places pp where pp.photo_id = ${a}.id and pp.place_id = $${params.length})`);
  }
  if (f.album_id != null) {
    params.push(f.album_id);
    c.push(`exists (select 1 from album_photos ap where ap.photo_id = ${a}.id and ap.album_id = $${params.length})`);
  }
  if (f.has_no_date) c.push(`${a}.capture_date_confirmed = false`);
  if (f.has_untagged_faces) {
    c.push(`exists (select 1 from faces f where f.photo_id = ${a}.id and f.person_id is null
                      and f.is_deleted = false and coalesce(f.review_status, 'pending') <> 'ignore')`);
  }
  if (f.has_unknown_faces) {
    c.push(`exists (select 1 from faces f where f.photo_id = ${a}.id and f.person_id is null
                      and f.is_deleted = false and f.review_status = 'unknown')`);
  }
  if (f.low_completeness) c.push(`${a}.completeness_score < 60`);
  if (f.q) {
    params.push(f.q);
    const qi = params.length;
    params.push(`%${f.q.replace(/[%_\\]/g, (m) => `\\${m}`)}%`);
    const li = params.length;
    c.push(`(${a}.search_tsv @@ websearch_to_tsquery('english', $${qi})
             or ${a}.scan_batch ilike $${li} or ${a}.source_folder ilike $${li}
             or exists (select 1 from faces f join people pe on pe.id = f.person_id
                         where f.photo_id = ${a}.id and f.is_deleted = false and f.is_disputed = false
                           and (pe.display_name ilike $${li}
                                or exists (select 1 from person_name_variants v
                                            where v.person_id = pe.id and v.variant ilike $${li}))))`);
  }
  return c;
}

function rowToItem(r) {
  return {
    id: Number(r.id),
    thumb_url: `/media/thumbs/${r.id}`,
    working_url: `/media/working/${r.id}`,
    capture_date: r.capture_date_str,
    capture_date_precision: r.capture_date_precision,
    capture_date_confirmed: r.capture_date_confirmed,
    scan_batch: r.scan_batch,
    scan_sequence: r.scan_sequence,
    physical_ref_note: r.physical_ref_note,
    completeness_score: r.completeness_score,
    rescan_wanted: r.rescan_wanted,
    width: r.width,
    height: r.height,
    like_count: r.like_count,
  };
}

// Core list query. Returns { items, next } — `next` is a composite cursor.
async function listPhotos(pool, user, {
  filters = {}, sort = 'recent', cursor = null, limit = 40, scope = null, reverse = false,
} = {}) {
  let sortKey = SORTS[sort] ? sort : 'recent';
  if (sortKey === 'position' && filters.album_id == null) sortKey = 'recent';
  const sortDef = SORTS[sortKey];
  const lim = Math.max(1, Math.min(Number(limit) || 40, 200));

  const params = [];
  const clauses = [visibleSql(user, params, 'p')];
  const sc = scopeSql(scope, params, 'p');
  if (sc) clauses.push(sc);
  clauses.push(...filtersSql(filters, params, 'p'));

  let albumPos = '';
  if (sortKey === 'position') {
    params.push(filters.album_id);
    albumPos = `, (select ap.position from album_photos ap
                   where ap.album_id = $${params.length} and ap.photo_id = p.id) as album_position`;
  }
  const c = typeof cursor === 'string' || cursor == null ? decodeCursor(cursor) : cursor;
  const outer = [];
  const kw = keysetWhere(sortDef, c, params, { reverse });
  if (kw) outer.push(kw);
  params.push(lim);

  // Sorting by likes needs the count for every candidate row: join the
  // pre-aggregated likes (one hash join) instead of a subquery per photo.
  // Other sorts only compute it for the rows actually returned.
  const likeCol = sortKey === 'liked'
    ? 'coalesce(lc.n, 0)::int as like_count'
    : '(select count(*) from likes l where l.photo_id = p.id)::int as like_count';
  const likeJoin = sortKey === 'liked'
    ? 'left join (select photo_id, count(*) as n from likes group by photo_id) lc on lc.photo_id = p.id'
    : '';
  const sql = `
    select * from (
      select p.id, p.capture_date, to_char(p.capture_date, 'YYYY-MM-DD') as capture_date_str,
             p.capture_date_precision, p.capture_date_confirmed,
             p.scan_batch, p.scan_sequence, p.physical_ref_note,
             p.completeness_score, p.rescan_wanted, p.width, p.height,
             ${likeCol}
             ${albumPos}
        from photos p
        ${likeJoin}
       where ${clauses.join('\n         and ')}
    ) x
    ${outer.length ? `where ${outer.join(' and ')}` : ''}
    ${orderBy(sortDef, { reverse })}
    limit $${params.length}`;
  const { rows } = await pool.query(sql, params);
  const items = rows.map(rowToItem);
  let next = null;
  if (rows.length === lim) {
    const last = rows[rows.length - 1];
    const v = sortDef.col === 'capture_date' ? last.capture_date_str
      : sortDef.col ? last[sortDef.col] : undefined;
    next = encodeCursor(v, last.id);
  }
  return { items, next, sort: sortKey };
}

// ---- list keys (prev/next context, answer D9) --------------------------
// `b.<sort>` browse · `nd.<sort>` no date · `ut.<sort>` untagged faces ·
// `wi.<sort>` unknown faces · `p.<personId>` · `a.<albumId>`.
function parseListKey(key) {
  const parts = String(key || '').split('.');
  const sortOf = (s) => (BROWSE_SORTS.includes(s) ? s : 'recent');
  switch (parts[0]) {
    case 'nd': return { key: `nd.${sortOf(parts[1])}`, filters: { has_no_date: true }, sort: sortOf(parts[1]) };
    case 'ut': return { key: `ut.${sortOf(parts[1])}`, filters: { has_untagged_faces: true }, sort: sortOf(parts[1]) };
    case 'wi': return { key: `wi.${sortOf(parts[1])}`, filters: { has_unknown_faces: true }, sort: sortOf(parts[1]) };
    case 'p': {
      const id = toInt(parts[1]);
      if (id) return { key: `p.${id}`, filters: { person_id: id }, sort: 'recent' };
      break;
    }
    case 'a': {
      const id = toInt(parts[1]);
      if (id) return { key: `a.${id}`, filters: { album_id: id }, sort: 'position' };
      break;
    }
    case 'b': return { key: `b.${sortOf(parts[1])}`, filters: {}, sort: sortOf(parts[1]) };
    default: break;
  }
  return { key: 'b.recent', filters: {}, sort: 'recent' };
}

// Browse-page query → list key.
function listKeyForBrowse(filters, sort) {
  const s = BROWSE_SORTS.includes(sort) ? sort : 'recent';
  if (filters.has_no_date) return `nd.${s}`;
  if (filters.has_untagged_faces) return `ut.${s}`;
  if (filters.has_unknown_faces) return `wi.${s}`;
  return `b.${s}`;
}

// { prev, next } photo ids around `photoId` in the list named by `from`.
// Falls back to Browse order when the photo is not in that list.
async function photoNeighbours(pool, user, photoId, from, scope) {
  let lk = parseListKey(from);
  // Sort value of this photo inside the list (null if not in it).
  const probe = async (key) => {
    const params = [];
    const clauses = [visibleSql(user, params, 'p')];
    const sc = scopeSql(scope, params, 'p');
    if (sc) clauses.push(sc);
    clauses.push(...filtersSql(key.filters, params, 'p'));
    let albumPos = '';
    if (key.sort === 'position') {
      params.push(key.filters.album_id);
      albumPos = `, (select ap.position from album_photos ap
                     where ap.album_id = $${params.length} and ap.photo_id = p.id) as album_position`;
    }
    params.push(photoId);
    const { rows } = await pool.query(
      `select p.id, to_char(p.capture_date, 'YYYY-MM-DD') as capture_date, p.completeness_score,
              (select count(*) from likes l where l.photo_id = p.id)::int as like_count ${albumPos}
         from photos p
        where ${clauses.join(' and ')} and p.id = $${params.length}`,
      params,
    );
    return rows[0] || null;
  };
  let here = await probe(lk);
  if (!here && lk.key !== 'b.recent') {
    lk = parseListKey('b.recent');
    here = await probe(lk);
  }
  if (!here) return { prev: null, next: null, from: lk.key };
  const def = SORTS[lk.sort];
  const cur = { v: def.col ? (here[def.col] ?? null) : undefined, id: Number(here.id) };
  const [n, p] = await Promise.all([
    listPhotos(pool, user, { filters: lk.filters, sort: lk.sort, scope, limit: 1, cursor: cur }),
    listPhotos(pool, user, { filters: lk.filters, sort: lk.sort, scope, limit: 1, cursor: cur, reverse: true }),
  ]);
  return {
    next: n.items[0] ? n.items[0].id : null,
    prev: p.items[0] ? p.items[0].id : null,
    from: lk.key,
  };
}

// ---- detail -------------------------------------------------------------

async function getPhotoDetail(pool, user, id) {
  if (!(await assertPhotoVisible(pool, user, id))) return null;
  const photo = (await pool.query(
    `select id, to_char(capture_date, 'YYYY-MM-DD') as capture_date,
            capture_date_precision, capture_date_confirmed,
            scan_batch, scan_sequence, source_folder, source_filename, physical_ref_note,
            rescan_wanted, completeness_score, description_ai, width, height, orientation,
            has_no_people, is_scan, exif_taken_at, exif_camera, exif_gps_lat, exif_gps_lon
       from photos where id = $1`, [id],
  )).rows[0];

  const [faces, comments, places, likes, backs, suggestions, albums, groups] = await Promise.all([
    pool.query(
      `select f.id, f.person_id, f.bbox, f.is_disputed, f.dispute_note, f.source, f.review_status,
              p.display_name
         from faces f left join people p on p.id = f.person_id
        where f.photo_id = $1 and f.is_deleted = false and coalesce(f.review_status, 'pending') <> 'ignore'
        order by f.id`, [id]),
    pool.query(
      `select c.id, c.body, c.created_at, c.is_hidden, u.display_name, u.email, u.id as user_id
         from comments c left join users u on u.id = c.user_id
        where c.photo_id = $1
        order by c.created_at asc, c.id asc`, [id]),
    pool.query(
      `select pl.id, pl.name, pl.latitude, pl.longitude, pp.confirmed
         from photo_places pp join places pl on pl.id = pp.place_id
        where pp.photo_id = $1 and pl.is_deleted = false
        order by pl.name`, [id]),
    pool.query(
      `select count(*)::int as n, coalesce(bool_or(user_id = $2), false) as me
         from likes where photo_id = $1`, [id, user.id]),
    pool.query(
      `select id, transcribed_text, transcription_confidence, transcription_confirmed
         from photo_backs where photo_id = $1 order by id`, [id]),
    pool.query(
      `select s.id, s.kind, s.payload, s.confidence, s.source, s.created_at,
              u.id as user_id, u.display_name, u.email
         from suggestions s left join users u on u.id = s.user_id
        where s.photo_id = $1 and s.status = 'pending'
        order by s.id desc`, [id]),
    pool.query(
      `select a.id, a.name from album_photos ap join albums a on a.id = ap.album_id
        where ap.photo_id = $1 and a.is_deleted = false order by a.name`, [id]),
    pool.query(
      `select g.id, g.name,
              coalesce(gm.role = 'moderator' and gm.is_deleted = false, false) as i_moderate
         from photo_groups pg
         join groups g on g.id = pg.group_id
    left join group_members gm on gm.group_id = g.id and gm.user_id = $2
        where pg.photo_id = $1 and pg.is_deleted = false and g.is_deleted = false
        order by lower(g.name)`, [id, user.id]),
  ]);

  const isAdmin = user.role === 'admin';
  const moderatesHere = groups.rows.some((g) => g.i_moderate);
  const canSeeSuggester = isAdmin || moderatesHere;
  const canModerate = isAdmin || moderatesHere;

  const faceItems = faces.rows.map((f) => ({
    id: Number(f.id),
    crop_url: `/media/faces/${f.id}`,
    person_id: f.person_id != null ? Number(f.person_id) : null,
    person_display_name: f.display_name,
    bbox: f.bbox,
    source: f.source,
    is_disputed: f.is_disputed,
    dispute_note: f.dispute_note,
    review_status: f.review_status,
  }));
  const peopleSeen = new Map();
  for (const f of faceItems) {
    if (f.person_id && !f.is_disputed && !peopleSeen.has(f.person_id)) {
      peopleSeen.set(f.person_id, { id: f.person_id, display_name: f.person_display_name });
    }
  }

  return {
    ...photo,
    id: Number(photo.id),
    thumb_url: `/media/thumbs/${photo.id}`,
    working_url: `/media/working/${photo.id}`,
    exif: {
      taken_at: photo.exif_taken_at, camera: photo.exif_camera,
      gps_lat: photo.exif_gps_lat, gps_lon: photo.exif_gps_lon,
    },
    faces: faceItems,
    people: [...peopleSeen.values()],
    unknown_face_count: faceItems.filter((f) => !f.person_id && f.review_status === 'unknown').length,
    comments: comments.rows
      .filter((c) => canModerate || !c.is_hidden)
      .map((c) => ({
        id: Number(c.id),
        body: c.body,
        created_at: c.created_at,
        is_hidden: c.is_hidden,
        author_display_name: c.display_name || (c.email ? c.email.replace(/@.*/, '') : 'someone'),
        user_id: c.user_id != null ? Number(c.user_id) : null,
      })),
    places: places.rows.map((p) => ({ ...p, id: Number(p.id) })),
    likes: { count: likes.rows[0].n, me: likes.rows[0].me },
    backs: backs.rows.map((b) => ({
      id: Number(b.id),
      image_url: `/media/backs/${b.id}`,
      transcribed_text: b.transcribed_text,
      transcription_confidence: b.transcription_confidence,
      transcription_confirmed: b.transcription_confirmed,
    })),
    suggestions_pending: suggestions.rows.map((s) => ({
      id: Number(s.id),
      kind: s.kind,
      payload: s.payload,
      confidence: s.confidence,
      source: s.source,
      created_at: s.created_at,
      mine: s.user_id != null && Number(s.user_id) === Number(user.id),
      author_display_name: canSeeSuggester
        ? (s.source === 'ai' ? 'AI' : (s.display_name || (s.email ? s.email.replace(/@.*/, '') : 'someone')))
        : (s.source === 'ai' ? 'AI' : 'someone'),
    })),
    albums: albums.rows.map((a) => ({ id: Number(a.id), name: a.name })),
    // Groups strip: admins and moderators of one of the photo's groups only.
    groups: canModerate
      ? groups.rows.map((g) => ({ id: Number(g.id), name: g.name, can_remove: isAdmin || g.i_moderate }))
      : null,
    can_moderate: canModerate,
    can_see_suggester: canSeeSuggester,
  };
}

// ---- needs-attention strip --------------------------------------------

async function attentionCounts(pool, user, scope) {
  const params = [];
  const clauses = [visibleSql(user, params, 'p')];
  const sc = scopeSql(scope, params, 'p');
  if (sc) clauses.push(sc);
  const where = clauses.join(' and ');
  // Faces aggregated once per photo and hash-joined — a per-photo subquery
  // costs ~0.5 s for an admin over the whole archive.
  const { rows } = await pool.query(
    `select
       count(*) filter (where p.capture_date_confirmed = false)::int as no_date,
       count(*) filter (where fc.untagged > 0)::int as untagged_faces,
       coalesce(sum(fc.unknown), 0)::int as unknown_faces,
       count(*)::int as total
       from photos p
  left join (select f.photo_id,
                    count(*) filter (where coalesce(f.review_status, 'pending') <> 'ignore') as untagged,
                    count(*) filter (where f.review_status = 'unknown') as unknown
               from faces f
              where f.person_id is null and f.is_deleted = false
              group by f.photo_id) fc on fc.photo_id = p.id
      where ${where}`,
    params,
  );
  return rows[0];
}

module.exports = {
  SORTS, BROWSE_SORTS, parseFilters, filtersSql, visibleSql, listPhotos,
  parseListKey, listKeyForBrowse, photoNeighbours, getPhotoDetail, attentionCounts,
};
