// Helpers for the People / Albums / Search pages (Phase 10).
//
// Counts go through the same visibility + scope + filter SQL as
// services/photos.listPhotos, so a count never disagrees with its grid.
// Relationship suggestions are filed here for both the JSON API
// (POST /api/relationships) and the no-JS page form.

const { visibleSql, filtersSql } = require('./photos');
const { scopeSql } = require('./scope');
const { autocompletePeople } = require('./people');
const { autocompletePlaces } = require('./places');
const { audit } = require('./audit');

// Number of visible, in-scope photos matching `filters`.
async function countPhotos(pool, user, { filters = {}, scope = null } = {}) {
  const params = [];
  const clauses = [visibleSql(user, params, 'p')];
  const sc = scopeSql(scope, params, 'p');
  if (sc) clauses.push(sc);
  clauses.push(...filtersSql(filters, params, 'p'));
  const { rows } = await pool.query(
    `select count(*)::int as n from photos p where ${clauses.join(' and ')}`, params,
  );
  return rows[0].n;
}

// Plain-words relationship → stored row. The OTHER person is the subject:
// "<other> is their parent" → parent row (a = other, b = this person);
// "<other> is their child"  → parent row (a = this person, b = other).
const RELATIONS = {
  parent: { label: 'is their parent', type: 'parent', otherIsA: true },
  child: { label: 'is their child', type: 'parent', otherIsA: false },
  spouse: { label: 'is their spouse', type: 'spouse', otherIsA: false },
  sibling: { label: 'is their brother or sister', type: 'sibling', otherIsA: false },
};

function relationToRow(personId, otherId, relation) {
  const r = RELATIONS[relation];
  if (!r) return null;
  return r.otherIsA
    ? { person_a_id: otherId, person_b_id: personId, type: r.type }
    : { person_a_id: personId, person_b_id: otherId, type: r.type };
}

// Files a pending `relationship` suggestion. Returns { id }, { id, duplicate }
// (an identical suggestion is already pending), { already } (the
// relationship is already recorded), or { error }.
async function suggestRelationship(pool, user, { person_a_id: a, person_b_id: b, type }) {
  if (!Number.isSafeInteger(a) || !Number.isSafeInteger(b) || a <= 0 || b <= 0 || a === b) {
    return { error: 'bad person ids' };
  }
  if (!['parent', 'spouse', 'sibling'].includes(type)) return { error: 'bad type' };
  const client = await pool.connect();
  try {
    await client.query('begin');
    const check = await client.query(
      `select count(*)::int as n from people where id = any($1::bigint[]) and is_deleted = false`,
      [[a, b]],
    );
    if (check.rows[0].n !== 2) { await client.query('rollback'); return { error: 'person not found' }; }
    // Parent rows are directional; spouse/sibling match either order.
    const symmetric = type !== 'parent';
    const known = await client.query(
      `select 1 from relationships
        where type = $3 and ((person_a_id = $1 and person_b_id = $2)
                             or ($4 and person_a_id = $2 and person_b_id = $1))
        limit 1`, [a, b, type, symmetric],
    );
    if (known.rows.length) { await client.query('rollback'); return { already: true }; }
    const dup = await client.query(
      `select id from suggestions
        where kind = 'relationship' and status = 'pending' and payload->>'type' = $3
          and (((payload->>'person_a_id')::bigint = $1 and (payload->>'person_b_id')::bigint = $2)
               or ($4 and (payload->>'person_a_id')::bigint = $2 and (payload->>'person_b_id')::bigint = $1))
        limit 1`, [a, b, type, symmetric],
    );
    if (dup.rows.length) { await client.query('rollback'); return { id: Number(dup.rows[0].id), duplicate: true }; }
    const ins = await client.query(
      `insert into suggestions (photo_id, user_id, kind, payload, source, status)
       values (null, $1, 'relationship', $2::jsonb, 'human', 'pending')
       returning id`,
      [user.id, { person_a_id: a, person_b_id: b, type }],
    );
    await audit(client, {
      actor: user.email, action: 'suggestion.create',
      entityType: 'suggestion', entityId: ins.rows[0].id, userId: user.id,
      newValue: { kind: 'relationship', person_a_id: a, person_b_id: b, type },
    });
    await client.query('commit');
    return { id: Number(ins.rows[0].id) };
  } catch (err) {
    await client.query('rollback').catch(() => {});
    throw err;
  } finally {
    client.release();
  }
}

// Pending suggestions (from getPerson) → sentences from this person's side:
// [{ id, other: { id, display_name }, text: 'parent' | 'child' | 'spouse' | 'brother or sister' }]
async function describePendingRelationships(pool, personId, pending) {
  const me = Number(personId);
  const ids = [...new Set(pending.flatMap((s) => [Number(s.person_a_id), Number(s.person_b_id)]))]
    .filter((id) => Number.isSafeInteger(id) && id !== me);
  if (!ids.length) return [];
  const { rows } = await pool.query(
    `select id, display_name from people where id = any($1::bigint[]) and is_deleted = false`, [ids],
  );
  const names = new Map(rows.map((r) => [Number(r.id), r.display_name]));
  const out = [];
  for (const s of pending) {
    const a = Number(s.person_a_id), b = Number(s.person_b_id);
    const otherId = a === me ? b : a;
    if (!names.has(otherId)) continue;
    let role;
    if (s.type === 'parent') role = b === me ? 'parent' : 'child';
    else if (s.type === 'spouse') role = 'spouse';
    else if (s.type === 'sibling') role = 'brother or sister';
    else continue;
    out.push({ id: s.id, other: { id: otherId, display_name: names.get(otherId) }, role });
  }
  return out;
}

// Resolve a typed name to one person (no-JS forms): an exact
// (case-insensitive) display-name match, or the only autocomplete hit.
// Returns { person, candidates }.
async function resolvePersonByName(pool, name) {
  const text = String(name || '').trim();
  if (!text) return { person: null, candidates: [] };
  const hits = await autocompletePeople(pool, text);
  if (!hits.length) return { person: null, candidates: [] };
  // "Charles Clay", "Chuck Clay", "Chuck", 'Charles "Chuck" Clay' all name one person.
  const { rows } = await pool.query(
    `select id, display_name, given_name, middle_name, surname, maiden_name, nickname
       from people where id = any($1::bigint[])`, [hits.map((h) => h.id)],
  );
  const norm = (s) => String(s || '').toLowerCase().replace(/["“”]/g, '').replace(/\s+/g, ' ').trim();
  const want = norm(text);
  const join = (...parts) => parts.filter(Boolean).join(' ');
  const exactIds = new Set(rows.filter((r) => [
    r.display_name,
    join(r.given_name, r.surname),
    join(r.given_name, r.middle_name, r.surname),
    join(r.nickname, r.surname),
    join(r.given_name, r.maiden_name),
    r.nickname,
  ].filter(Boolean).map(norm).includes(want)).map((r) => Number(r.id)));
  const exact = hits.filter((h) => exactIds.has(h.id));
  if (exact.length === 1) return { person: exact[0], candidates: hits };
  if (hits.length === 1) return { person: hits[0], candidates: hits };
  return { person: null, candidates: hits };
}

async function resolvePlaceByName(pool, name) {
  const text = String(name || '').trim();
  if (!text) return null;
  const hits = await autocompletePlaces(pool, text);
  return hits.find((h) => h.name.toLowerCase() === text.toLowerCase())
    || (hits.length === 1 ? hits[0] : null);
}

async function personName(pool, id) {
  const { rows } = await pool.query(
    `select id, display_name from people where id = $1 and is_deleted = false`, [id],
  );
  return rows[0] ? { id: Number(rows[0].id), display_name: rows[0].display_name } : null;
}

async function placeName(pool, id) {
  const { rows } = await pool.query(
    `select id, name from places where id = $1 and is_deleted = false`, [id],
  );
  return rows[0] ? { id: Number(rows[0].id), name: rows[0].name } : null;
}

// "1931–2004" / "born 1931" / "died 2004" / null.
function lifeSpan(p) {
  if (p.birth_year != null && p.death_year != null) return `${p.birth_year}–${p.death_year}`;
  if (p.birth_year != null) return `born ${p.birth_year}`;
  if (p.death_year != null) return `died ${p.death_year}`;
  return null;
}

module.exports = {
  countPhotos, RELATIONS, relationToRow, suggestRelationship, describePendingRelationships,
  resolvePersonByName, resolvePlaceByName, personName, placeName, lifeSpan,
};
