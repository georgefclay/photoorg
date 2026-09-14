// Photo visibility helpers — used everywhere a photo, face, back, thumb,
// comment, like, suggestion, person-page photo list, search result, or
// count is produced. The rule:
//
//   * admins see all non-private, non-deleted photos (including unfiled);
//   * everyone else sees photos that share at least one live group with
//     the user (photo_groups + group_members, both not is_deleted);
//   * photos with no live group are admin-only;
//   * is_private overrides everything.
//
// A non-member of a photo's groups must get 404 (not 403 — don't confirm
// the photo's existence). Lists and counts must never include invisible
// rows.
//
// Two ways to use this:
//
//   1. photoVisibleWhere(user, { alias }) — returns a SQL fragment and a
//      $-index-agnostic params array to append to your query's params.
//      Assumes there's exactly one placeholder slot needed ($USER_ID),
//      which is passed as the last param by helpers.paramize().
//      Prefer the compact `photoVisibleSql(user, { alias, paramIndex })`
//      when you already know your parameter index.
//
//   2. photoVisibleSql(user, { alias, paramIndex }) — returns just the
//      SQL fragment, using $paramIndex as the user-id placeholder. You
//      push the user id into the params array yourself.
//
// A `user` object shape is expected: `{ id, role }`. Admins skip group
// checks; contributors get the exists(...) group intersect. There is no
// "anonymous" case — every caller of these helpers has already gone
// through requireUser or requireService (service is handled separately
// and does not use visibility).

function photoVisibleSql(user, { alias = 'photos', paramIndex } = {}) {
  if (!user || !user.role) throw new Error('photoVisibleSql: user required');
  if (paramIndex == null) throw new Error('photoVisibleSql: paramIndex required');

  // Always-on hard gates: not deleted, not private.
  const base = `${alias}.is_deleted = false and ${alias}.is_private = false`;

  if (user.role === 'admin') return base;

  // Contributor / moderator: needs a shared live group.
  const groupExists = `
    exists (
      select 1
        from photo_groups pg
        join group_members gm using (group_id)
       where pg.photo_id = ${alias}.id
         and pg.is_deleted = false
         and gm.is_deleted = false
         and gm.user_id = $${paramIndex}
    )
  `;
  return `(${base}) and (${groupExists})`;
}

// Convenience — for callers that use tagged-template-ish query building.
// Appends the user id to the params and returns { sql, params }.
function photoVisibleWhere(user, params, { alias = 'photos' } = {}) {
  if (user.role === 'admin') {
    return {
      sql: photoVisibleSql(user, { alias, paramIndex: 0 }),
      params,
    };
  }
  const nextParams = [...params, user.id];
  return {
    sql: photoVisibleSql(user, { alias, paramIndex: nextParams.length }),
    params: nextParams,
  };
}

// SQL fragment for "is this photo (by id) visible to this user?" — a
// full standalone check that can be dropped into any query. Same rules.
// Returns { sql, params }. `photoIdParamIndex` is the $-index of the
// photo id in your caller's params.
function photoIsVisibleCheck(user, photoIdParamIndex) {
  if (!user || !user.role) throw new Error('photoIsVisibleCheck: user required');
  if (paramMissing(photoIdParamIndex)) throw new Error('photoIsVisibleCheck: photoIdParamIndex required');
  if (user.role === 'admin') {
    return {
      sql: `select 1 from photos p
             where p.id = $${photoIdParamIndex}
               and p.is_deleted = false
               and p.is_private = false`,
      extraParams: [],
    };
  }
  return {
    sql: `select 1 from photos p
           where p.id = $${photoIdParamIndex}
             and p.is_deleted = false
             and p.is_private = false
             and exists (
               select 1 from photo_groups pg
                 join group_members gm using (group_id)
                where pg.photo_id = p.id
                  and pg.is_deleted = false
                  and gm.is_deleted = false
                  and gm.user_id = $${photoIdParamIndex + 1}
             )`,
    extraParams: 'user_id',
  };
}

function paramMissing(x) {
  return x === undefined || x === null;
}

// Assert a photo is visible to a user. Returns true/false. Never leaks
// the photo's existence: the caller should respond 404 on false.
async function assertPhotoVisible(pool, user, photoId) {
  if (!user || !photoId) return false;
  if (user.role === 'admin') {
    const { rows } = await pool.query(
      `select 1
         from photos
        where id = $1
          and is_deleted = false
          and is_private = false`,
      [photoId],
    );
    return rows.length > 0;
  }
  const { rows } = await pool.query(
    `select 1
       from photos p
      where p.id = $1
        and p.is_deleted = false
        and p.is_private = false
        and exists (
          select 1 from photo_groups pg
            join group_members gm using (group_id)
           where pg.photo_id = p.id
             and pg.is_deleted = false
             and gm.is_deleted = false
             and gm.user_id = $2
        )`,
    [photoId, user.id],
  );
  return rows.length > 0;
}

// Is this user a moderator (or admin) for this group? Admins are yes
// implicitly. Suspension is handled by loadUser upstream.
async function isModerator(pool, user, groupId) {
  if (!user) return false;
  if (user.role === 'admin') return true;
  const { rows } = await pool.query(
    `select 1
       from group_members
      where group_id = $1
        and user_id  = $2
        and role     = 'moderator'
        and is_deleted = false`,
    [groupId, user.id],
  );
  return rows.length > 0;
}

// Group ids the user is a live member of (any role). Empty array for
// admins is misleading, so admins get all-live group ids.
async function userGroupIds(pool, user) {
  if (!user) return [];
  if (user.role === 'admin') {
    const { rows } = await pool.query(
      `select id from groups where is_deleted = false order by id`,
    );
    return rows.map((r) => Number(r.id));
  }
  const { rows } = await pool.query(
    `select gm.group_id
       from group_members gm
       join groups g on g.id = gm.group_id
      where gm.user_id = $1
        and gm.is_deleted = false
        and g.is_deleted = false
      order by gm.group_id`,
    [user.id],
  );
  return rows.map((r) => Number(r.group_id));
}

// Group ids the user is a moderator of. Admins → all live groups.
async function userModeratorGroupIds(pool, user) {
  if (!user) return [];
  if (user.role === 'admin') {
    const { rows } = await pool.query(
      `select id from groups where is_deleted = false order by id`,
    );
    return rows.map((r) => Number(r.id));
  }
  const { rows } = await pool.query(
    `select gm.group_id
       from group_members gm
       join groups g on g.id = gm.group_id
      where gm.user_id = $1
        and gm.role = 'moderator'
        and gm.is_deleted = false
        and g.is_deleted = false
      order by gm.group_id`,
    [user.id],
  );
  return rows.map((r) => Number(r.group_id));
}

module.exports = {
  photoVisibleSql,
  photoVisibleWhere,
  photoIsVisibleCheck,
  assertPhotoVisible,
  isModerator,
  userGroupIds,
  userModeratorGroupIds,
};
