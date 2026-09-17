// Group scope — the header's group switcher (Phase 10, answer D6).
//
// Stored in the session as `req.session.scope`:
//   'all'      — contributors: "All my groups"; admins: "All photos"
//   'unfiled'  — admins only: photos in no live group
//   '<id>'     — one group (contributors: only a group they belong to)
//
// Every photo grid and every count applies it on top of visibility
// (middleware/visibility.js still decides what the user may see at all).
// People and album LISTS are site-wide; their photo grids and counts
// respect the scope.

const { userGroupIds } = require('../middleware/visibility');

async function groupsForSwitcher(pool, user) {
  if (!user) return [];
  if (user.role === 'admin') {
    const { rows } = await pool.query(
      `select id, name from groups where is_deleted = false order by lower(name)`,
    );
    return rows.map((r) => ({ id: Number(r.id), name: r.name, role: 'admin' }));
  }
  const { rows } = await pool.query(
    `select g.id, g.name, gm.role
       from group_members gm
       join groups g on g.id = gm.group_id
      where gm.user_id = $1 and gm.is_deleted = false and g.is_deleted = false
      order by lower(g.name)`,
    [user.id],
  );
  return rows.map((r) => ({ id: Number(r.id), name: r.name, role: r.role }));
}

// Normalise a raw scope value against what this user may pick.
function resolveScope(user, groups, raw) {
  const s = raw == null ? 'all' : String(raw);
  if (s === 'unfiled' && user && user.role === 'admin') {
    return { kind: 'unfiled', key: 'unfiled', label: 'Unfiled' };
  }
  if (/^\d+$/.test(s)) {
    const g = groups.find((x) => x.id === Number(s));
    if (g) return { kind: 'group', key: String(g.id), groupId: g.id, label: g.name };
  }
  return {
    kind: 'all', key: 'all',
    label: user && user.role === 'admin' ? 'All photos' : 'All my groups',
  };
}

// Memoised per request: `await getScope(req, pool)` → { scope, groups }.
// `req.query.scope` overrides the session for one request (API clients).
async function getScope(req, pool) {
  if (req._scopeCache) return req._scopeCache;
  const groups = await groupsForSwitcher(pool, req.user);
  const raw = req.query && req.query.scope != null
    ? req.query.scope
    : (req.session && req.session.scope);
  const scope = resolveScope(req.user, groups, raw);
  req._scopeCache = { scope, groups };
  return req._scopeCache;
}

// SQL fragment restricting photos (alias) to the scope. Pushes params.
function scopeSql(scope, params, alias = 'p') {
  if (!scope || scope.kind === 'all') return null;
  if (scope.kind === 'unfiled') {
    return `not exists (select 1 from photo_groups spg
                         where spg.photo_id = ${alias}.id and spg.is_deleted = false)`;
  }
  params.push(scope.groupId);
  return `exists (select 1 from photo_groups spg
                   where spg.photo_id = ${alias}.id and spg.group_id = $${params.length}
                     and spg.is_deleted = false)`;
}

// Page middleware: exposes the switcher to the layout.
function scopeLocals({ pool }) {
  return async function (req, res, next) {
    if (!req.user) return next();
    try {
      const { scope, groups } = await getScope(req, pool);
      res.locals.groupScope = scope;
      res.locals.scopeGroups = groups;
      res.locals.isModerator = req.user.role === 'admin' || groups.some((g) => g.role === 'moderator');
      next();
    } catch (err) { next(err); }
  };
}

function safeReturnTo(raw) {
  const s = String(raw || '/');
  return s.startsWith('/') && !s.startsWith('//') && !s.startsWith('/\\') ? s : '/';
}

module.exports = {
  groupsForSwitcher, resolveScope, getScope, scopeSql, scopeLocals, safeReturnTo, userGroupIds,
};
