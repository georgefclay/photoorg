// Admin-area queries shared by the admin pages (routes/pages-admin.js) and
// the admin JSON API (routes/api-admin.js, routes/api-groups.js).
//
// Admin-only data (suggestions, disputes, unfiled, rescan, audit, report)
// is never visibility-filtered: the caller has already passed requireAdmin.
// Moderator-reachable data (contributions, groups) takes the user and
// scopes itself to the groups they moderate.

const { userModeratorGroupIds } = require('../middleware/visibility');

const SUGGESTION_KINDS = ['date', 'person', 'place', 'relationship', 'description', 'transcription', 'classification'];
const SUGGESTION_SOURCES = ['human', 'ai', 'import'];
const KIND_LABELS = {
  date: 'Dates', person: 'People', place: 'Places', relationship: 'Relationships',
  description: 'Descriptions', transcription: 'Transcriptions', classification: 'Classifications',
};
const SOURCE_LABELS = { human: 'From family', ai: 'From AI', import: 'From import', all: 'All sources' };
const BULK_CAP = 20000;

function toInt(v) {
  if (v == null || v === '') return null;
  const n = Number(v);
  return Number.isSafeInteger(n) ? n : null;
}
const num = (v) => (v == null ? null : Number(v));
const likeEscape = (s) => String(s).replace(/[%_\\]/g, (m) => `\\${m}`);

function personName(np) {
  if (!np) return null;
  const main = [np.given_name, np.middle_name, np.surname].filter(Boolean).join(' ');
  const nick = np.nickname ? `“${np.nickname}”` : '';
  return [main, nick].filter(Boolean).join(' ') || np.display_name || 'someone new';
}

// JSON → one short line (audit browser, fallbacks).
function compactJson(v, max = 140) {
  if (v == null) return '';
  let s;
  try { s = JSON.stringify(v); } catch { s = String(v); }
  s = s.replace(/"([a-z_][a-z0-9_]*)":/gi, '$1: ');
  return s.length > max ? `${s.slice(0, max - 1)}…` : s;
}

// ---------------------------------------------------------------------------
//  Dashboard
// ---------------------------------------------------------------------------

async function moderatorGroups(pool, user) {
  if (user.role === 'admin') {
    const { rows } = await pool.query(
      `select g.id, g.name, g.description,
              (select count(*)::int from group_members gm where gm.group_id = g.id and gm.is_deleted = false) as member_count,
              (select count(*)::int from photo_groups pg where pg.group_id = g.id and pg.is_deleted = false) as photo_count
         from groups g where g.is_deleted = false order by lower(g.name)`,
    );
    return rows.map((r) => ({ ...r, id: Number(r.id) }));
  }
  const { rows } = await pool.query(
    `select g.id, g.name, g.description,
            (select count(*)::int from group_members gm2 where gm2.group_id = g.id and gm2.is_deleted = false) as member_count,
            (select count(*)::int from photo_groups pg where pg.group_id = g.id and pg.is_deleted = false) as photo_count
       from group_members gm join groups g on g.id = gm.group_id
      where gm.user_id = $1 and gm.role = 'moderator' and gm.is_deleted = false and g.is_deleted = false
      order by lower(g.name)`,
    [user.id],
  );
  return rows.map((r) => ({ ...r, id: Number(r.id) }));
}

// Pending contribution counts visible to this user (admin: all).
async function pendingContributionCounts(pool, user) {
  const params = [];
  let scope = '';
  if (user.role !== 'admin') {
    const mods = await userModeratorGroupIds(pool, user);
    if (!mods.length) return { contributions: 0, files: 0 };
    params.push(mods);
    scope = `and c.group_ids && $1::bigint[]`;
  }
  const { rows } = await pool.query(
    `select count(distinct c.id)::int as contributions, count(cf.id)::int as files
       from contributions c join contribution_files cf on cf.contribution_id = c.id
      where cf.status = 'pending' ${scope}`,
    params,
  );
  return rows[0];
}

async function dashboard(pool, user) {
  const contributions = await pendingContributionCounts(pool, user);
  const groups = await moderatorGroups(pool, user);
  if (user.role !== 'admin') return { contributions, groups };

  const [sug, disputes, access, users, unfiled, rescan, audit] = await Promise.all([
    pool.query(`select kind, source, count(*)::int as n from suggestions where status = 'pending' group by kind, source`),
    pool.query(`select count(*)::int as n from faces where is_disputed = true and is_deleted = false`),
    pool.query(`select count(*)::int as n from access_requests where status = 'pending' and token_expires_at > now()`),
    pool.query(`select count(*) filter (where status = 'active')::int as active,
                       count(*) filter (where status = 'suspended')::int as suspended,
                       count(*) filter (where role = 'admin')::int as admins
                  from users where is_service = false`),
    pool.query(`select count(*)::int as n from photos p
                 where p.is_deleted = false and p.is_private = false
                   and not exists (select 1 from photo_groups pg where pg.photo_id = p.id and pg.is_deleted = false)`),
    pool.query(`select count(*)::int as n from photos where rescan_wanted = true and is_deleted = false`),
    pool.query(`select max(created_at) as last from audit_log`),
  ]);
  const byKind = {};
  const bySource = { human: 0, ai: 0, import: 0 };
  let total = 0;
  for (const r of sug.rows) {
    byKind[r.kind] = (byKind[r.kind] || 0) + r.n;
    bySource[r.source] = (bySource[r.source] || 0) + r.n;
    total += r.n;
  }
  return {
    contributions,
    groups,
    suggestions: {
      total,
      bySource,
      byKind: SUGGESTION_KINDS.filter((k) => byKind[k]).map((k) => ({ kind: k, label: KIND_LABELS[k], n: byKind[k] })),
    },
    disputes: disputes.rows[0].n,
    access: access.rows[0].n,
    users: users.rows[0],
    unfiled: unfiled.rows[0].n,
    rescan: rescan.rows[0].n,
    auditLast: audit.rows[0].last,
  };
}

// ---------------------------------------------------------------------------
//  Suggestions queue
// ---------------------------------------------------------------------------

async function suggestionCounts(pool) {
  const { rows } = await pool.query(
    `select kind, source, count(*)::int as n from suggestions where status = 'pending' group by kind, source`,
  );
  return rows;
}

// Human first: the family's suggestions are few and valuable; the AI's are
// many. Pick the first source that has anything pending.
function defaultSource(counts) {
  for (const s of SUGGESTION_SOURCES) if (counts.some((c) => c.source === s && c.n > 0)) return s;
  return 'human';
}

async function listSuggestions(pool, { source = 'human', kind = null, cursor = null, limit = 40 } = {}) {
  const params = [];
  const where = [`s.status = 'pending'`];
  if (SUGGESTION_SOURCES.includes(source)) { params.push(source); where.push(`s.source = $${params.length}`); }
  if (SUGGESTION_KINDS.includes(kind)) { params.push(kind); where.push(`s.kind = $${params.length}`); }
  if (cursor) { params.push(cursor); where.push(`s.id < $${params.length}`); }
  params.push(limit);
  const { rows } = await pool.query(
    `select s.id, s.photo_id, s.kind, s.payload, s.confidence, s.source, s.model, s.created_at,
            u.display_name as user_name, u.email as user_email,
            p.id as p_id, to_char(p.capture_date, 'YYYY-MM-DD') as p_date, p.capture_date_precision as p_precision,
            p.capture_date_confirmed as p_confirmed, p.description_ai as p_description,
            p.is_deleted as p_deleted, p.is_private as p_private
       from suggestions s
  left join users u on u.id = s.user_id
  left join photos p on p.id = s.photo_id
      where ${where.join(' and ')}
      order by s.id desc
      limit $${params.length}`,
    params,
  );

  // Batch lookups for names shown on the cards.
  const personIds = new Set();
  const faceIds = new Set();
  const placeIds = new Set();
  const placePhotoIds = new Set();
  for (const r of rows) {
    const pl = r.payload || {};
    if (r.kind === 'person') {
      if (toInt(pl.person_id)) personIds.add(toInt(pl.person_id));
      if (toInt(pl.face_id)) faceIds.add(toInt(pl.face_id));
    } else if (r.kind === 'relationship') {
      if (toInt(pl.person_a_id)) personIds.add(toInt(pl.person_a_id));
      if (toInt(pl.person_b_id)) personIds.add(toInt(pl.person_b_id));
    } else if (r.kind === 'place') {
      if (toInt(pl.place_id)) placeIds.add(toInt(pl.place_id));
      if (r.photo_id) placePhotoIds.add(Number(r.photo_id));
    }
  }
  const faces = new Map();
  if (faceIds.size) {
    const f = await pool.query(
      `select f.id, f.person_id, f.is_deleted, pe.display_name
         from faces f left join people pe on pe.id = f.person_id
        where f.id = any($1::bigint[])`, [[...faceIds]],
    );
    for (const x of f.rows) {
      faces.set(Number(x.id), x);
      if (x.person_id) personIds.add(Number(x.person_id));
    }
  }
  const people = new Map();
  if (personIds.size) {
    const pr = await pool.query(`select id, display_name from people where id = any($1::bigint[])`, [[...personIds]]);
    for (const x of pr.rows) people.set(Number(x.id), x.display_name);
  }
  const places = new Map();
  if (placeIds.size) {
    const pr = await pool.query(`select id, name from places where id = any($1::bigint[])`, [[...placeIds]]);
    for (const x of pr.rows) places.set(Number(x.id), x.name);
  }
  const photoPlaces = new Map();
  if (placePhotoIds.size) {
    const pr = await pool.query(
      `select pp.photo_id, string_agg(pl.name, ', ' order by pl.name) as names
         from photo_places pp join places pl on pl.id = pp.place_id
        where pp.photo_id = any($1::bigint[]) and pp.confirmed = true and pl.is_deleted = false
        group by pp.photo_id`, [[...placePhotoIds]],
    );
    for (const x of pr.rows) photoPlaces.set(Number(x.photo_id), x.names);
  }

  const { dateLabel } = require('./format');
  const items = rows.map((r) => {
    const pl = r.payload || {};
    const conf = r.confidence != null ? r.confidence : (typeof pl.confidence === 'number' ? pl.confidence : null);
    const card = {
      id: Number(r.id),
      kind: r.kind,
      kindLabel: KIND_LABELS[r.kind] || r.kind,
      source: r.source,
      photo_id: num(r.photo_id),
      photo_gone: r.photo_id != null && (!r.p_id || r.p_deleted || r.p_private),
      who: r.source === 'ai' ? 'AI' : (r.user_name || (r.user_email ? r.user_email.replace(/@.*/, '') : (r.source === 'import' ? 'Import' : 'someone'))),
      who_email: r.user_email,
      created_at: r.created_at,
      model: r.model || pl.model || null,
      prompt_version: pl.prompt_version || null,
      confidence: conf,
      proposed: null,
      current: null,
      evidence: null,
      crop_url: null,
      links: [],
    };
    switch (r.kind) {
      case 'date':
        card.proposed = dateLabel(pl.date, pl.precision) || String(pl.date || '?');
        if (pl.precision && pl.precision !== 'exact') card.proposed += ` (${pl.precision})`;
        if (r.p_date) card.current = `${dateLabel(r.p_date, r.p_precision)}${r.p_confirmed ? ' (confirmed)' : ' (a guess)'}`;
        card.evidence = pl.evidence || pl.text || null;
        card.conflict = !!r.p_confirmed;
        break;
      case 'person': {
        const pid = toInt(pl.person_id);
        card.proposed = pid ? (people.get(pid) || `Person #${pid}`) : `${personName(pl.new_person)} (new person)`;
        if (pid) card.links.push({ href: `/people/${pid}`, label: card.proposed });
        const face = toInt(pl.face_id) ? faces.get(toInt(pl.face_id)) : null;
        if (toInt(pl.face_id)) card.crop_url = `/media/faces/${toInt(pl.face_id)}`;
        if (face && face.person_id) {
          card.current = face.display_name || people.get(Number(face.person_id)) || `Person #${face.person_id}`;
          card.conflict = Number(face.person_id) !== pid;
        }
        if (face && face.is_deleted) card.evidence = 'That face box has since been removed.';
        break;
      }
      case 'place': {
        const plid = toInt(pl.place_id);
        card.proposed = plid ? (places.get(plid) || `Place #${plid}`) : `${(pl.new_place && pl.new_place.name) || '?'} (new place)`;
        card.current = r.photo_id ? (photoPlaces.get(Number(r.photo_id)) || null) : null;
        break;
      }
      case 'relationship': {
        const a = toInt(pl.person_a_id);
        const b = toInt(pl.person_b_id);
        const an = people.get(a) || `Person #${a}`;
        const bn = people.get(b) || `Person #${b}`;
        const phrase = pl.type === 'parent' ? 'is a parent of' : pl.type === 'spouse' ? 'is married to' : pl.type === 'sibling' ? 'is a sibling of' : String(pl.type || 'related to');
        card.proposed = `${an} ${phrase} ${bn}`;
        if (a) card.links.push({ href: `/people/${a}`, label: an });
        if (b) card.links.push({ href: `/people/${b}`, label: bn });
        break;
      }
      case 'description':
        card.proposed = String(pl.text || '');
        card.current = r.p_description || null;
        card.conflict = !!r.p_description && r.p_description !== pl.text;
        break;
      case 'transcription':
        card.proposed = String(pl.text || '');
        card.evidence = [pl.parsed_date && `Date: ${pl.parsed_date}`, Array.isArray(pl.names) && pl.names.length && `Names: ${pl.names.join(', ')}`]
          .filter(Boolean).join(' · ') || null;
        break;
      case 'classification':
        card.proposed = String(pl.label || '?').replace(/_/g, ' ');
        break;
      default:
        card.proposed = compactJson(pl);
    }
    return card;
  });
  return { items, next: rows.length === limit ? Number(rows[rows.length - 1].id) : null };
}

// ---------------------------------------------------------------------------
//  Disputes
// ---------------------------------------------------------------------------

async function listDisputes(pool) {
  const { rows } = await pool.query(
    `select f.id, f.photo_id, f.person_id, f.dispute_note, f.source,
            pe.display_name as person_name,
            u.display_name as by_name, u.email as by_email,
            (select max(a.created_at) from audit_log a
              where a.entity_type = 'face' and a.entity_id = f.id and a.action = 'face.dispute') as disputed_at
       from faces f
  left join people pe on pe.id = f.person_id
  left join users u on u.id = f.disputed_by
      where f.is_disputed = true and f.is_deleted = false
      order by f.id desc
      limit 500`,
  );
  return rows.map((r) => ({
    face_id: Number(r.id),
    photo_id: Number(r.photo_id),
    person_id: num(r.person_id),
    person_name: r.person_name,
    note: r.dispute_note,
    by: r.by_name || (r.by_email ? r.by_email.replace(/@.*/, '') : 'someone'),
    by_email: r.by_email,
    disputed_at: r.disputed_at,
  }));
}

// ---------------------------------------------------------------------------
//  Contributions
// ---------------------------------------------------------------------------

const CONTRIB_FILTERS = {
  review: 'Needs review',
  partial: 'Partly decided',
  approved: 'Approved',
  rejected: 'Rejected',
  all: 'All',
};

async function listContributions(pool, user, { filter = 'review', cursor = null, limit = 20 } = {}) {
  const isAdmin = user.role === 'admin';
  const mods = isAdmin ? null : await userModeratorGroupIds(pool, user);
  if (!isAdmin && !mods.length) return { items: [], next: null, groupNames: new Map() };
  const params = [];
  // A contribution with no files (an upload that found everything already on
  // the server) has nothing to review.
  const where = [`exists (select 1 from contribution_files x where x.contribution_id = c.id)`];
  if (!isAdmin) { params.push(mods); where.push(`c.group_ids && $${params.length}::bigint[]`); }
  if (filter === 'review') where.push(`exists (select 1 from contribution_files x where x.contribution_id = c.id and x.status = 'pending')`);
  else if (['partial', 'approved', 'rejected'].includes(filter)) { params.push(filter); where.push(`c.status = $${params.length}::contribution_status`); }
  if (cursor) { params.push(cursor); where.push(`c.id < $${params.length}`); }
  params.push(limit);
  const { rows } = await pool.query(
    `select c.id, c.user_id, c.status, c.note, c.group_ids, c.created_at, c.finished_at, c.pulled_at,
            u.display_name as user_name, u.email as user_email
       from contributions c left join users u on u.id = c.user_id
      ${where.length ? `where ${where.join(' and ')}` : ''}
      order by c.id desc
      limit $${params.length}`,
    params,
  );
  const ids = rows.map((r) => Number(r.id));
  const files = ids.length ? (await pool.query(
    `select cf.id, cf.contribution_id, cf.original_filename, cf.size, cf.mime, cf.width, cf.height,
            cf.exif_taken_at, cf.status, cf.is_video, cf.approved_group_ids,
            cf.duplicate_of_photo_id, cf.duplicate_distance,
            case when cf.duplicate_of_photo_id is null then null
                 when exists (select 1 from photos p where p.id = cf.duplicate_of_photo_id and p.sha256 = cf.sha256)
                   or exists (select 1 from photo_masters pm where pm.photo_id = cf.duplicate_of_photo_id and pm.sha256 = cf.sha256)
                 then 'exact' else 'near' end as dup_kind
       from contribution_files cf
      where cf.contribution_id = any($1::bigint[])
      order by cf.id`, [ids],
  )).rows : [];
  const groupIds = new Set();
  rows.forEach((r) => (r.group_ids || []).forEach((g) => groupIds.add(Number(g))));
  const groupNames = new Map();
  if (groupIds.size) {
    const g = await pool.query(`select id, name, is_deleted from groups where id = any($1::bigint[])`, [[...groupIds]]);
    g.rows.forEach((x) => groupNames.set(Number(x.id), x.is_deleted ? `${x.name} (deleted)` : x.name));
  }
  const byC = new Map(ids.map((id) => [id, []]));
  for (const f of files) {
    byC.get(Number(f.contribution_id)).push({
      id: Number(f.id),
      original_filename: f.original_filename,
      size: num(f.size),
      mime: f.mime,
      width: f.width,
      height: f.height,
      exif_taken_at: f.exif_taken_at,
      status: f.status,
      is_video: f.is_video,
      approved_groups: (f.approved_group_ids || []).map((g) => groupNames.get(Number(g)) || `#${g}`),
      duplicate_of_photo_id: num(f.duplicate_of_photo_id),
      duplicate_distance: f.duplicate_distance,
      dup_kind: f.dup_kind,
    });
  }
  const items = rows.map((r) => {
    const targets = (r.group_ids || []).map(Number);
    const fs = byC.get(Number(r.id));
    return {
      id: Number(r.id),
      status: r.status,
      note: r.note,
      uploader: r.user_name || r.user_email || 'unknown',
      uploader_email: r.user_email,
      created_at: r.created_at,
      finished_at: r.finished_at,
      pulled_at: r.pulled_at,
      targets: targets.map((g) => ({ id: g, name: groupNames.get(g) || `#${g}`, mine: isAdmin || mods.includes(g) })),
      files: fs,
      counts: {
        total: fs.length,
        pending: fs.filter((f) => f.status === 'pending').length,
        approved: fs.filter((f) => f.status === 'approved').length,
        rejected: fs.filter((f) => f.status === 'rejected').length,
        dups: fs.filter((f) => f.duplicate_of_photo_id).length,
      },
    };
  });
  return { items, next: rows.length === limit ? ids[ids.length - 1] : null };
}

// ---------------------------------------------------------------------------
//  Groups
// ---------------------------------------------------------------------------

// null when the group doesn't exist or this user may not manage it.
async function groupDetail(pool, user, groupId) {
  const g = (await pool.query(
    `select g.id, g.name, g.description, g.created_at,
            (select count(*)::int from photo_groups pg where pg.group_id = g.id and pg.is_deleted = false) as photo_count
       from groups g where g.id = $1 and g.is_deleted = false`, [groupId],
  )).rows[0];
  if (!g) return null;
  if (user.role !== 'admin') {
    const mods = await userModeratorGroupIds(pool, user);
    if (!mods.includes(Number(g.id))) return null;
  }
  const members = (await pool.query(
    `select u.id, u.email, u.display_name, u.status, gm.role, gm.added_at
       from group_members gm join users u on u.id = gm.user_id
      where gm.group_id = $1 and gm.is_deleted = false
      order by gm.role desc, lower(coalesce(u.display_name, u.email))`, [groupId],
  )).rows;
  return {
    ...g,
    id: Number(g.id),
    members: members.map((m) => ({ ...m, id: Number(m.id) })),
  };
}

async function lookupUsers(pool, q, groupId) {
  const text = String(q || '').trim().slice(0, 100);
  if (text.length < 2) return [];
  const like = `%${likeEscape(text.toLowerCase())}%`;
  const prefix = `${likeEscape(text.toLowerCase())}%`;
  const { rows } = await pool.query(
    `select u.id, u.email, u.display_name,
            exists (select 1 from group_members gm where gm.group_id = $3 and gm.user_id = u.id and gm.is_deleted = false) as is_member
       from users u
      where u.status = 'active' and u.is_service = false
        and (lower(u.email) like $1 or lower(coalesce(u.display_name, '')) like $2)
      order by (lower(u.email) like $1) desc, lower(coalesce(u.display_name, u.email))
      limit 10`,
    [prefix, like, groupId],
  );
  return rows.map((r) => ({ id: Number(r.id), email: r.email, display_name: r.display_name || r.email, is_member: r.is_member }));
}

// ---------------------------------------------------------------------------
//  Bulk assign filters (shared by /admin/unfiled and bulk-assign-groups)
// ---------------------------------------------------------------------------

// Filter shape: { album_id, scan_batch, person_id, year, decade, source_folder, only_unfiled }.
// Returns SQL clauses on `photos p`, pushing params.
function bulkFilterClauses(b, params) {
  const clauses = ['p.is_deleted = false'];
  const albumId = toInt(b.album_id);
  if (albumId != null) {
    params.push(albumId);
    clauses.push(`exists (select 1 from album_photos ap where ap.photo_id = p.id and ap.album_id = $${params.length})`);
  }
  if (b.scan_batch) { params.push(String(b.scan_batch)); clauses.push(`p.scan_batch = $${params.length}`); }
  const personId = toInt(b.person_id);
  if (personId != null) {
    params.push(personId);
    clauses.push(`exists (select 1 from faces f where f.photo_id = p.id and f.person_id = $${params.length} and f.is_deleted = false and f.is_disputed = false)`);
  }
  const year = toInt(b.year);
  if (year != null) { params.push(year); clauses.push(`extract(year from p.capture_date) = $${params.length}`); }
  const decade = toInt(b.decade);
  if (decade != null) { params.push(decade, decade + 9); clauses.push(`extract(year from p.capture_date) between $${params.length - 1} and $${params.length}`); }
  if (b.source_folder) { params.push(String(b.source_folder)); clauses.push(`p.source_folder = $${params.length}`); }
  if (b.only_unfiled === true || b.only_unfiled === 'true' || b.only_unfiled === '1') {
    clauses.push('p.is_private = false');
    clauses.push('not exists (select 1 from photo_groups ug where ug.photo_id = p.id and ug.is_deleted = false)');
  }
  return clauses;
}

function parseBulkFilter(q = {}) {
  return {
    album_id: toInt(q.album_id),
    scan_batch: String(q.scan_batch || '').trim() || null,
    person_id: toInt(q.person_id),
    year: toInt(q.year),
    decade: toInt(q.decade),
    source_folder: String(q.source_folder || '').trim() || null,
  };
}

function hasBulkFilter(f) {
  return ['album_id', 'scan_batch', 'person_id', 'year', 'decade', 'source_folder'].some((k) => f[k] != null);
}

async function unfiledPage(pool, filter, { cursor = null, limit = 60 } = {}) {
  const params = [];
  const clauses = bulkFilterClauses({ ...filter, only_unfiled: true }, params);
  const countParams = [...params];
  if (cursor) { params.push(cursor); clauses.push(`p.id < $${params.length}`); }
  params.push(limit);
  const [list, count] = await Promise.all([
    pool.query(
      `select p.id, to_char(p.capture_date, 'YYYY-MM-DD') as capture_date, p.capture_date_precision,
              p.capture_date_confirmed, p.scan_batch, p.scan_sequence, p.source_folder,
              0 as like_count
         from photos p where ${clauses.join(' and ')}
        order by p.id desc limit $${params.length}`,
      params,
    ),
    pool.query(
      `select count(*)::int as n from photos p where ${bulkFilterClauses({ ...filter, only_unfiled: true }, []).join(' and ')}`,
      countParams,
    ),
  ]);
  return {
    items: list.rows.map((r) => ({ ...r, id: Number(r.id), thumb_url: `/media/thumbs/${r.id}` })),
    next: list.rows.length === limit ? Number(list.rows[list.rows.length - 1].id) : null,
    total: count.rows[0].n,
  };
}

async function unfiledFilterOptions(pool) {
  const unfiled = `p.is_deleted = false and p.is_private = false
                   and not exists (select 1 from photo_groups pg where pg.photo_id = p.id and pg.is_deleted = false)`;
  const [albums, batches, folders, decades, groups] = await Promise.all([
    pool.query(`select id, name from albums where is_deleted = false order by lower(name) limit 1000`),
    pool.query(`select scan_batch as v, count(*)::int as n from photos p where ${unfiled} and scan_batch is not null
                 group by scan_batch order by scan_batch limit 500`),
    pool.query(`select source_folder as v, count(*)::int as n from photos p where ${unfiled} and source_folder is not null
                 group by source_folder order by source_folder limit 500`),
    pool.query(`select (floor(extract(year from capture_date) / 10) * 10)::int as v, count(*)::int as n
                  from photos p where ${unfiled} and capture_date is not null group by 1 order by 1`),
    pool.query(`select id, name from groups where is_deleted = false order by lower(name)`),
  ]);
  return {
    albums: albums.rows.map((r) => ({ id: Number(r.id), name: r.name })),
    batches: batches.rows,
    folders: folders.rows,
    decades: decades.rows,
    groups: groups.rows.map((r) => ({ id: Number(r.id), name: r.name })),
  };
}

// ---------------------------------------------------------------------------
//  Rescan list, monthly report, audit
// ---------------------------------------------------------------------------

async function rescanBatches(pool) {
  const { rows } = await pool.query(
    `select id, scan_batch, scan_sequence, source_folder, source_filename, physical_ref_note
       from photos
      where rescan_wanted = true and is_deleted = false
      order by scan_batch nulls last, scan_sequence nulls last, id`,
  );
  const batches = [];
  let cur = null;
  for (const r of rows) {
    const key = r.scan_batch || '(no batch)';
    if (!cur || cur.batch !== key) { cur = { batch: key, items: [] }; batches.push(cur); }
    cur.items.push({ ...r, id: Number(r.id) });
  }
  return { batches, total: rows.length };
}

async function currentMonth(pool) {
  return (await pool.query(`select to_char(now(), 'YYYY-MM') as m`)).rows[0].m;
}

async function monthlyReport(pool, month) {
  const start = `${month}-01`;
  const perUserRes = await pool.query(
    `with in_month as (
       select * from audit_log
        where created_at >= $1::date and created_at < ($1::date + interval '1 month')
     )
     select
       u.id as user_id,
       u.display_name, u.email,
       count(*) filter (where a.action = 'auth.login')            as logins,
       count(*) filter (where a.action = 'face.create')           as faces_tagged,
       count(*) filter (where a.action = 'comment.create')        as comments,
       count(*) filter (where a.action in ('like.create','like.delete')) as likes_toggled,
       count(*) filter (where a.action = 'suggestion.create')     as suggestions_made,
       count(*) filter (where a.action = 'suggestion.accept')     as suggestions_accepted,
       count(*) filter (where a.action = 'suggestion.reject')     as suggestions_rejected,
       count(*) filter (where a.action = 'photo.capture_date.set') as dates_confirmed
     from users u
left join in_month a on a.user_id = u.id
    group by u.id, u.display_name, u.email
    having count(a.*) > 0
    order by u.display_name`,
    [start],
  );
  const totalLikes = (await pool.query(
    `select count(*)::int as n from likes
      where created_at >= $1::date and created_at < ($1::date + interval '1 month')`,
    [start],
  )).rows[0].n;
  return {
    month,
    per_user: perUserRes.rows.map((r) => ({
      user_id: r.user_id != null ? Number(r.user_id) : null,
      display_name: r.display_name || r.email,
      logins: Number(r.logins),
      faces_tagged: Number(r.faces_tagged),
      comments: Number(r.comments),
      likes_toggled: Number(r.likes_toggled),
      suggestions_made: Number(r.suggestions_made),
      suggestions_accepted: Number(r.suggestions_accepted),
      suggestions_rejected: Number(r.suggestions_rejected),
      dates_confirmed: Number(r.dates_confirmed),
    })),
    total_likes_in_month: totalLikes,
  };
}

// Extra site-wide totals for the report page (not part of the JSON API).
async function monthlyExtras(pool, month) {
  const { rows } = await pool.query(
    `select
       (select count(*)::int from contribution_files cf join contributions c on c.id = cf.contribution_id
         where c.created_at >= $1::date and c.created_at < ($1::date + interval '1 month')) as files_uploaded,
       (select count(*)::int from access_requests
         where status = 'approved' and decided_at >= $1::date and decided_at < ($1::date + interval '1 month')) as people_approved,
       (select count(*)::int from comments
         where created_at >= $1::date and created_at < ($1::date + interval '1 month')) as comments,
       (select count(*)::int from suggestions
         where source = 'human' and created_at >= $1::date and created_at < ($1::date + interval '1 month')) as suggestions`,
    [`${month}-01`],
  );
  return rows[0];
}

function shiftMonth(month, delta) {
  const [y, m] = month.split('-').map(Number);
  const t = y * 12 + (m - 1) + delta;
  return `${Math.floor(t / 12)}-${String((t % 12) + 1).padStart(2, '0')}`;
}

// Audit filters: entity_type / action / actor exact; action ending in '*'
// is a prefix match (e.g. `auth.*`).
function auditWhere(q, params) {
  const clauses = [];
  if (q.entity_type) { params.push(String(q.entity_type)); clauses.push(`entity_type = $${params.length}`); }
  const eid = toInt(q.entity_id);
  if (eid != null) { params.push(eid); clauses.push(`entity_id = $${params.length}`); }
  if (q.action) {
    const a = String(q.action).trim();
    if (a.endsWith('*')) { params.push(`${likeEscape(a.slice(0, -1))}%`); clauses.push(`action like $${params.length}`); }
    else { params.push(a); clauses.push(`action = $${params.length}`); }
  }
  if (q.actor) { params.push(String(q.actor).trim().toLowerCase()); clauses.push(`lower(actor) = $${params.length}`); }
  return clauses;
}

async function auditPage(pool, q, { cursor = null, limit = 50 } = {}) {
  const params = [];
  const clauses = auditWhere(q, params);
  if (cursor) { params.push(cursor); clauses.push(`id < $${params.length}`); }
  params.push(limit);
  const { rows } = await pool.query(
    `select id, user_id, actor, action, entity_type, entity_id, previous_value, new_value, created_at
       from audit_log
      ${clauses.length ? `where ${clauses.join(' and ')}` : ''}
      order by id desc limit $${params.length}`,
    params,
  );
  return {
    items: rows.map((r) => ({ ...r, id: Number(r.id), entity_id: num(r.entity_id) })),
    next: rows.length === limit ? Number(rows[rows.length - 1].id) : null,
  };
}

// Distinct entity types via a loose index scan on (entity_type, entity_id).
async function auditEntityTypes(pool) {
  const { rows } = await pool.query(
    `with recursive t as (
       (select entity_type from audit_log order by entity_type limit 1)
       union all
       select (select entity_type from audit_log where entity_type > t.entity_type order by entity_type limit 1)
         from t where t.entity_type is not null
     )
     select entity_type from t where entity_type is not null`,
  );
  return rows.map((r) => r.entity_type);
}

function auditEntityHref(row) {
  if (row.entity_id == null) return null;
  switch (row.entity_type) {
    case 'photo': return `/photos/${row.entity_id}`;
    case 'person': return `/people/${row.entity_id}`;
    case 'group': return `/admin/groups/${row.entity_id}`;
    default: return null;
  }
}

module.exports = {
  SUGGESTION_KINDS, SUGGESTION_SOURCES, KIND_LABELS, SOURCE_LABELS, CONTRIB_FILTERS, BULK_CAP,
  toInt, compactJson, personName,
  dashboard, moderatorGroups, pendingContributionCounts,
  suggestionCounts, defaultSource, listSuggestions,
  listDisputes,
  listContributions,
  groupDetail, lookupUsers,
  bulkFilterClauses, parseBulkFilter, hasBulkFilter, unfiledPage, unfiledFilterOptions,
  rescanBatches, currentMonth, monthlyReport, monthlyExtras, shiftMonth,
  auditWhere, auditPage, auditEntityTypes, auditEntityHref,
};
