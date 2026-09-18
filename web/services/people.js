// People queries. People are site-wide; every count or photo derived from
// them is visibility- and scope-filtered.

const { visibleSql } = require('./photos');
const { resolvePeopleIds } = require('./search');
const { scopeSql } = require('./scope');
const { encodeCursor, decodeCursor, keysetWhere, orderBy } = require('./cursor');

function toInt(v) { const n = Number(v); return Number.isSafeInteger(n) ? n : null; }

const NAME_SORT = { col: 'sort_name', dir: 'asc', cast: 'text' };

// Searchable list: name, visible face count, year span of visible photos.
async function listPeople(pool, user, { q = '', cursor = null, limit = 100, scope = null } = {}) {
  const lim = Math.max(1, Math.min(Number(limit) || 100, 500));
  const params = [];
  const inner = ['pe.is_deleted = false'];
  const text = String(q || '').trim().slice(0, 100);
  if (text) {
    // The typed text as a substring, OR anything the Phase 11 name
    // resolver says this is: a nickname, a curated variant, a
    // misspelling. "Peggy" finds Margaret on this page too.
    const ids = await resolvePeopleIds(pool, text);
    params.push(`%${text.toLowerCase().replace(/[%_\\]/g, (m) => `\\${m}`)}%`);
    const i = params.length;
    params.push(ids);
    const idsP = params.length;
    inner.push(`(lower(pe.display_name) like $${i}
                 or lower(coalesce(pe.maiden_name, '')) like $${i}
                 or lower(coalesce(pe.nickname, '')) like $${i}
                 or pe.id = any($${idsP}::bigint[])
                 or exists (select 1 from person_name_variants v
                             where v.person_id = pe.id and lower(v.variant) like $${i}))`);
  }
  const vis = visibleSql(user, params, 'ph');
  const sc = scopeSql(scope, params, 'ph');
  const photoWhere = [vis, sc].filter(Boolean).join(' and ');
  const outer = [];
  const kw = keysetWhere(NAME_SORT, decodeCursor(cursor), params);
  if (kw) outer.push(kw);
  params.push(lim);
  const { rows } = await pool.query(
    `select * from (
       select pe.id, pe.display_name, pe.given_name, pe.surname, pe.maiden_name, pe.nickname,
              pe.suffix, pe.birth_year, pe.death_year,
              lower(coalesce(pe.display_name, '')) as sort_name,
              st.face_count, st.photo_count, st.year_min, st.year_max
         from people pe
         cross join lateral (
           select count(*)::int as face_count,
                  count(distinct ph.id)::int as photo_count,
                  min(extract(year from ph.capture_date))::int as year_min,
                  max(extract(year from ph.capture_date))::int as year_max
             from faces f
             join photos ph on ph.id = f.photo_id
            where f.person_id = pe.id and f.is_deleted = false and f.is_disputed = false
              and ${photoWhere}
         ) st
        where ${inner.join(' and ')}
     ) x
     ${outer.length ? `where ${outer.join(' and ')}` : ''}
     ${orderBy(NAME_SORT)}
     limit $${params.length}`,
    params,
  );
  const items = rows.map((r) => ({
    id: Number(r.id),
    display_name: r.display_name,
    given_name: r.given_name,
    surname: r.surname,
    maiden_name: r.maiden_name,
    nickname: r.nickname,
    suffix: r.suffix,
    birth_year: r.birth_year,
    death_year: r.death_year,
    face_count: r.face_count,
    photo_count: r.photo_count,
    year_min: r.year_min,
    year_max: r.year_max,
  }));
  const last = rows[rows.length - 1];
  return { items, next: rows.length === lim ? encodeCursor(last.sort_name, last.id) : null };
}

// Prefix on any name field first, then the Phase 11 name index
// (nicknames both ways, curated variants, guarded phonetics), then
// trigram on display_name (≤ 10). "Peggy" offers Margaret here too.
async function autocompletePeople(pool, q) {
  const text = String(q || '').trim().slice(0, 100);
  if (!text) return [];
  const prefix = `${text.toLowerCase().replace(/[%_\\]/g, '')}%`;
  const { rows } = await pool.query(
    `with tok as (
       select search_token($2) as tok,
              case when search_token($2) !~ ' ' then dmetaphone(search_token($2)) end as ph
     ),
     token_hits as (
       select p.id, p.display_name, p.birth_year, p.death_year,
              min(case ps.kind when 'exact' then 0.1 when 'variant' then 0.2
                               when 'nickname' then 0.3 else 0.6 end)::float as rank
         from person_search ps
         join people p on p.id = ps.person_id and p.is_deleted = false
        cross join tok
        where ps.token = tok.tok
           or (tok.ph is not null and ps.phonetic = tok.ph
               and (similarity(ps.token, tok.tok) >= 0.3
                    or (length(ps.token) >= 5 and length(tok.tok) >= 5
                        and left(ps.token, 4) = left(tok.tok, 4))))
        group by p.id, p.display_name, p.birth_year, p.death_year
        limit 10
     ),
     prefix_hits as (
       select p.id, p.display_name, p.birth_year, p.death_year, 0::float as rank
         from people p
        where p.is_deleted = false
          and (lower(p.display_name) like $1
               or lower(coalesce(p.given_name, '')) like $1
               or lower(coalesce(p.surname, '')) like $1
               or lower(coalesce(p.nickname, '')) like $1
               or lower(coalesce(p.maiden_name, '')) like $1
               or exists (select 1 from person_name_variants v
                           where v.person_id = p.id and lower(v.variant) like $1))
        limit 10
     ),
     trigram_hits as (
       select p.id, p.display_name, p.birth_year, p.death_year,
              (1 - similarity(lower(p.display_name), $2))::float as rank
         from people p
        where p.is_deleted = false and lower(p.display_name) % $2
        order by rank asc
        limit 10
     )
     select id, display_name, birth_year, death_year, min(rank) as rank
       from (select * from prefix_hits
             union all select * from token_hits
             union all select * from trigram_hits) u
      group by id, display_name, birth_year, death_year
      order by min(rank) asc, display_name asc
      limit 10`,
    [prefix, text.toLowerCase()],
  );
  return rows.map((r) => ({
    id: Number(r.id), display_name: r.display_name, birth_year: r.birth_year, death_year: r.death_year,
  }));
}

// Person page data minus the photo grid (use photos.listPhotos with
// person_id for that). Relationships: parents, children, spouses, and
// siblings (stored + derived from shared parents). `parent` rows mean
// person_a is a parent of person_b.
async function getPerson(pool, id) {
  const person = (await pool.query(
    `select id, given_name, middle_name, surname, maiden_name, nickname, suffix,
            display_name, birth_year, death_year, notes
       from people where id = $1 and is_deleted = false`, [id],
  )).rows[0];
  if (!person) return null;
  const [variants, rels] = await Promise.all([
    pool.query(`select variant, kind from person_name_variants where person_id = $1 order by lower(variant)`, [id]),
    pool.query(
      `select r.id, r.person_a_id, r.person_b_id, r.type, r.confirmed,
              pa.display_name as a_name, pb.display_name as b_name
         from relationships r
         join people pa on pa.id = r.person_a_id and pa.is_deleted = false
         join people pb on pb.id = r.person_b_id and pb.is_deleted = false
        where r.person_a_id = $1 or r.person_b_id = $1`, [id]),
  ]);
  const me = Number(id);
  const out = { parents: [], children: [], spouses: [], siblings: [] };
  const seen = { parents: new Set(), children: new Set(), spouses: new Set(), siblings: new Set() };
  const add = (bucket, pid, name, confirmed, derived = false) => {
    if (pid === me || seen[bucket].has(pid)) return;
    seen[bucket].add(pid);
    out[bucket].push({ id: pid, display_name: name, confirmed, derived });
  };
  for (const r of rels.rows) {
    const a = Number(r.person_a_id), b = Number(r.person_b_id);
    if (r.type === 'parent') {
      if (b === me) add('parents', a, r.a_name, r.confirmed);
      else add('children', b, r.b_name, r.confirmed);
    } else if (r.type === 'spouse') {
      add('spouses', a === me ? b : a, a === me ? r.b_name : r.a_name, r.confirmed);
    } else if (r.type === 'sibling') {
      add('siblings', a === me ? b : a, a === me ? r.b_name : r.a_name, r.confirmed);
    }
  }
  const parentIds = out.parents.map((p) => p.id);
  if (parentIds.length) {
    const { rows } = await pool.query(
      `select distinct pb.id, pb.display_name
         from relationships r join people pb on pb.id = r.person_b_id and pb.is_deleted = false
        where r.type = 'parent' and r.person_a_id = any($1::bigint[]) and r.person_b_id <> $2`,
      [parentIds, me],
    );
    for (const s of rows) add('siblings', Number(s.id), s.display_name, true, true);
  }
  const pendingRels = (await pool.query(
    `select s.id, s.payload from suggestions s
      where s.kind = 'relationship' and s.status = 'pending'
        and ((s.payload->>'person_a_id')::bigint = $1 or (s.payload->>'person_b_id')::bigint = $1)
      order by s.id desc limit 20`, [me],
  )).rows;
  return {
    ...person,
    id: me,
    variants: variants.rows,
    relationships: out,
    pending_relationship_suggestions: pendingRels.map((r) => ({ id: Number(r.id), ...r.payload })),
  };
}

module.exports = { listPeople, autocompletePeople, getPerson, toInt };
