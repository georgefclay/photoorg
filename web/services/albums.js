// Albums are site-wide. Counts are visibility- and scope-filtered; the
// album's photo grid is photos.listPhotos({ filters: { album_id }, sort:
// 'position' }).
//
// Phase 12 (closes PROJECT-PLAN §5 item 11): admins create, rename,
// describe, soft-delete, restore and re-order albums, and add and remove
// photos, here. Contributors stay read-only — "this belongs in album X"
// is still a suggestion. How each edit reaches the laptop:
//   * an album born here has a web id (≥ WEB_ID_FLOOR) and comes down,
//     with its memberships, through /sync/pull/web_origin; the desktop
//     shows it read-only (Phase 12 answer Q1);
//   * an edit to a desktop-born album stamps `edited_on_web_at` (album
//     row for name / description / is_deleted, album_photos row for
//     position / is_deleted) and comes down through /sync/pull/web_edits,
//     settled against the desktop's own edits by `sync_web_edit_wins` —
//     the Phase 15 rule, ties to the web.
// Every write is audited with previous and new values. Nothing is ever
// hard-deleted: a removal flips `is_deleted`, and re-adding flips it back.

const { visibleSql } = require('./photos');
const { scopeSql } = require('./scope');
const { audit } = require('./audit');
const { isWebOriginId } = require('./id-floor');

const NAME_MAX = 200;
const DESC_MAX = 4000;

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
          where ap.album_id = a.id and ap.is_deleted = false and ${where}
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
  return rows[0] ? { ...rows[0], id: Number(rows[0].id), web_born: isWebOriginId(rows[0].id) } : null;
}

// Admin-only lists: every live album (for the photo page's "add to
// album"), and the removed ones (for restore).
async function albumChoices(pool) {
  const { rows } = await pool.query(`select id, name from albums where is_deleted = false order by lower(name)`);
  return rows.map((r) => ({ id: Number(r.id), name: r.name }));
}

async function deletedAlbums(pool) {
  const { rows } = await pool.query(
    `select id, name from albums where is_deleted = true order by lower(name)`,
  );
  return rows.map((r) => ({ id: Number(r.id), name: r.name }));
}

// The album's live memberships in order, for the admin edit page.
// Admins see every non-private, non-deleted photo, so no group filter.
async function editRows(pool, albumId) {
  const { rows } = await pool.query(
    `select ap.photo_id, ap.position, to_char(p.capture_date, 'YYYY-MM-DD') as capture_date,
            p.capture_date_precision, p.capture_date_confirmed, p.scan_batch, p.scan_sequence,
            p.synced_file_version
       from album_photos ap join photos p on p.id = ap.photo_id
      where ap.album_id = $1 and ap.is_deleted = false
        and p.is_deleted = false and p.is_private = false
      order by ap.position asc nulls last, ap.photo_id`,
    [albumId],
  );
  return rows.map((r) => ({ ...r, photo_id: Number(r.photo_id), has_file: r.synced_file_version != null }));
}

function cleanName(v) { return String(v == null ? '' : v).replace(/\s+/g, ' ').trim().slice(0, NAME_MAX); }
function cleanDesc(v) {
  const s = String(v == null ? '' : v).replace(/\r\n/g, '\n').trim().slice(0, DESC_MAX);
  return s || null;
}

async function inTx(pool, fn) {
  const client = await pool.connect();
  try {
    await client.query('begin');
    const out = await fn(client);
    await client.query('commit');
    return out;
  } catch (err) {
    await client.query('rollback').catch(() => {});
    throw err;
  } finally {
    client.release();
  }
}

function auditAlbum(client, user, action, albumId, previousValue, newValue) {
  return audit(client, {
    actor: user.email, action, entityType: 'album', entityId: albumId, userId: user.id,
    previousValue, newValue,
  });
}

// → { id } or { error }.
async function createAlbum(pool, user, { name, description }) {
  const n = cleanName(name);
  if (!n) return { error: 'An album needs a name.' };
  return inTx(pool, async (c) => {
    const id = Number((await c.query(
      `insert into albums (name, description, source, created_by, edited_on_web_at)
       values ($1, $2, 'manual', $3, now()) returning id`,
      [n, cleanDesc(description), user.id],
    )).rows[0].id);
    await auditAlbum(c, user, 'album.create', id, null, { name: n, description: cleanDesc(description) });
    return { id };
  });
}

// → { changed } or { error } or null (no such album).
async function updateAlbum(pool, user, id, { name, description }) {
  return inTx(pool, async (c) => {
    const prev = (await c.query(`select name, description from albums where id = $1 and is_deleted = false for update`, [id])).rows[0];
    if (!prev) return null;
    const fields = {};
    if (name !== undefined) {
      const n = cleanName(name);
      if (!n) return { error: 'An album needs a name.' };
      if (n !== prev.name) fields.name = n;
    }
    if (description !== undefined) {
      const d = cleanDesc(description);
      if (d !== prev.description) fields.description = d;
    }
    const keys = Object.keys(fields);
    if (!keys.length) return { changed: false };
    await c.query(
      `update albums set ${keys.map((k, i) => `${k} = $${i + 2}`).join(', ')},
              edited_on_web_at = now(), updated_at = now()
        where id = $1`,
      [id, ...keys.map((k) => fields[k])],
    );
    await auditAlbum(c, user, 'album.update', id,
      Object.fromEntries(keys.map((k) => [k, prev[k]])), fields);
    return { changed: true };
  });
}

async function setAlbumDeleted(pool, user, id, deleted) {
  return inTx(pool, async (c) => {
    const prev = (await c.query(`select name, is_deleted from albums where id = $1 for update`, [id])).rows[0];
    if (!prev || prev.is_deleted === deleted) return false;
    await c.query(
      `update albums set is_deleted = $2, edited_on_web_at = now(), updated_at = now() where id = $1`,
      [id, deleted],
    );
    await auditAlbum(c, user, deleted ? 'album.delete' : 'album.restore', id,
      { name: prev.name, is_deleted: prev.is_deleted }, { name: prev.name, is_deleted: deleted });
    return true;
  });
}

// Add (or re-add — the flag flips back, never a key collision) or remove
// one photo. Adds go to the end of the album.
async function setPhotoInAlbum(pool, user, albumId, photoId, present) {
  return inTx(pool, async (c) => {
    const album = (await c.query(`select id from albums where id = $1 and is_deleted = false`, [albumId])).rows[0];
    if (!album) return false;
    if (present) {
      const ok = (await c.query(
        `select 1 from photos where id = $1 and is_deleted = false and is_private = false`, [photoId],
      )).rows[0];
      if (!ok) return false;
    }
    const prev = (await c.query(
      `select is_deleted, position from album_photos where album_id = $1 and photo_id = $2 for update`,
      [albumId, photoId],
    )).rows[0];
    if (prev && prev.is_deleted === !present) return false;
    if (!prev && !present) return false;
    if (present) {
      const pos = Number((await c.query(
        `select coalesce(max(position), 0) + 1 as p from album_photos where album_id = $1 and is_deleted = false`,
        [albumId],
      )).rows[0].p);
      await c.query(
        `insert into album_photos (album_id, photo_id, position, edited_on_web_at)
         values ($1, $2, $3, now())
         on conflict (album_id, photo_id) do update set
           is_deleted = false, deleted_at = null, deleted_by = null,
           position = excluded.position, edited_on_web_at = now()`,
        [albumId, photoId, pos],
      );
    } else {
      await c.query(
        `update album_photos set is_deleted = true, deleted_at = now(), deleted_by = $3,
                edited_on_web_at = now()
          where album_id = $1 and photo_id = $2`,
        [albumId, photoId, user.id],
      );
    }
    await auditAlbum(c, user, present ? 'album_photo.add' : 'album_photo.remove', albumId,
      { photo_id: photoId, is_deleted: prev ? prev.is_deleted : null },
      { photo_id: photoId, is_deleted: !present });
    return true;
  });
}

// Write positions 1..n in the given order. Only rows that move are
// written (and stamped), so a no-op re-order touches nothing.
async function reorderAlbum(pool, user, albumId, orderedIds) {
  return inTx(pool, async (c) => {
    const cur = (await c.query(
      `select photo_id, position from album_photos
        where album_id = $1 and is_deleted = false
        order by position asc nulls last, photo_id for update`,
      [albumId],
    )).rows.map((r) => ({ photo_id: Number(r.photo_id), position: r.position }));
    const live = new Set(cur.map((r) => r.photo_id));
    const order = [...new Set(orderedIds.map(Number).filter((id) => live.has(id)))];
    for (const r of cur) if (!order.includes(r.photo_id)) order.push(r.photo_id);
    const before = Object.fromEntries(cur.map((r) => [r.photo_id, r.position]));
    let moved = 0;
    for (let i = 0; i < order.length; i += 1) {
      if (before[order[i]] === i + 1) continue;
      await c.query(
        `update album_photos set position = $3, edited_on_web_at = now()
          where album_id = $1 and photo_id = $2`,
        [albumId, order[i], i + 1],
      );
      moved += 1;
    }
    if (moved) {
      await auditAlbum(c, user, 'album.reorder', albumId,
        { positions: before }, { order, moved });
    }
    return moved;
  });
}

// Move one photo a step up (-1) or down (+1): the phone-sized re-order.
async function movePhoto(pool, user, albumId, photoId, delta) {
  const rows = await editRows(pool, albumId);
  const ids = rows.map((r) => r.photo_id);
  const i = ids.indexOf(photoId);
  const j = i + delta;
  if (i < 0 || j < 0 || j >= ids.length) return 0;
  [ids[i], ids[j]] = [ids[j], ids[i]];
  return reorderAlbum(pool, user, albumId, ids);
}

module.exports = {
  listAlbums, getAlbum, albumChoices, deletedAlbums, editRows,
  createAlbum, updateAlbum, setAlbumDeleted, setPhotoInAlbum, reorderAlbum, movePhoto,
};
