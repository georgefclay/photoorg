// Search (Phase 11).
//
// One box. The query is split into terms (services/search-parse.js); each
// term is resolved to every layer it could mean; layers are OR-ed inside a
// term and AND-ed across terms; a photo's score is the sum of its best
// layer per term, and the result card explains the match in plain words.
//
// Layers, strongest first — the numbers are the score a term contributes:
//
//   person (exact / variant / nickname)  100 / 95 / 90
//   place (name or alias)                 88
//   description, tagged people (tsv A)    80
//   confirmed date                        75
//   back-of-print transcription (tsv B)   70
//   comment, album, place, pending
//     description, date evidence (tsv C)  60
//   unconfirmed capture date              55
//   person, phonetic ("Schmitt"→Schmidt)  50
//   place, phonetic                       45
//   folder / filename / locator (tsv D)   40
//   pending date suggestion ("estimated") 35
//
// A suggested (not yet accepted) person or place scores 30 below the fact.
// A phonetic person hit is only produced when pg_trgm similarity against
// the name it matched is ≥ 0.3, so "Schmitt" finds Schmidt but never Smith.
//
// Everything here goes through the same visibility + scope SQL as every
// other list: a non-member never sees a hit, a count, or an autocomplete
// suggestion for a photo they can't open.

const { photoVisibleSql } = require('../middleware/visibility');
const { scopeSql } = require('./scope');
const { encodeCursor, decodeCursor, orderBy, keysetWhere } = require('./cursor');
const { SORTS, BROWSE_SORTS, filtersSql, rowToItem, listPhotos } = require('./photos');
const { parseQuery, describeQuery } = require('./search-parse');
const fmt = require('./format');

const PERSON_SCORE = { exact: 100, variant: 95, nickname: 90, phonetic: 50 };
const PLACE_SCORE = { exact: 88, alias: 88, phonetic: 45 };
const SUGGESTED_PENALTY = 30;
const TEXT_SCORE = { text_a: 80, text_b: 70, text_c: 60, text_d: 40 };
const DATE_SCORE = { date: 75, date_unconfirmed: 55, date_estimated: 35, loose_penalty: 8 };
const TRIGRAM_FLOOR = 0.3;
// A phonetic hit is only allowed when the two spellings also LOOK alike:
// pg_trgm similarity ≥ 0.3, or a shared first four letters on two names of
// five letters or more. The second half is there for Katherine/Kathryn,
// which sound identical and read identically but score 0.29 on trigrams;
// Smith/Schmitt (0.17, shared prefix "s") stays excluded either way.
function looksAlike(a, b) {
  return `(similarity(${a}, ${b}) >= ${TRIGRAM_FLOOR}
           or (length(${a}) >= 5 and length(${b}) >= 5 and left(${a}, 4) = left(${b}, 4)))`;
}
const MAX_PEOPLE_PER_TERM = 50;
const SLOW_MS = 300;

const RELEVANCE = { col: 'score', dir: 'desc', cast: 'int', label: 'Best match' };
const SEARCH_SORTS = ['relevance', ...BROWSE_SORTS];

function visibleSql(user, params, alias = 'p') {
  if (user.role === 'admin') return photoVisibleSql(user, { alias, paramIndex: 0 });
  params.push(user.id);
  return photoVisibleSql(user, { alias, paramIndex: params.length });
}

// ---- resolution ---------------------------------------------------------

// One round trip: for every term, the people and places it could name and
// whether it has any searchable words left after stemming (a bare "the"
// has none, and must not be allowed to exclude every photo).
async function resolveTerms(pool, terms) {
  if (!terms.length) return terms.map((t) => ({ ...t, people: [], places: [], nodes: 0 }));
  const { rows } = await pool.query(
    `with t as (
       select * from unnest($1::text[]) with ordinality as x(raw, ix)
     ), norm as (
       select ix, raw, search_token(raw) as tok,
              case when search_token(raw) !~ ' ' then dmetaphone(search_token(raw)) end as ph,
              plainto_tsquery('english', search_text(raw)) as q
         from t
     ), pm as (
       select distinct on (n.ix, ps.person_id)
              n.ix, ps.person_id,
              case when ps.token = n.tok then ps.kind else 'phonetic' end as kind,
              ps.token as matched,
              case when ps.token = n.tok
                   then case ps.kind when 'exact' then 4 when 'variant' then 3
                                     when 'nickname' then 2 else 1 end
                   else 0 end as rank
         from norm n
         join person_search ps
           on ps.token = n.tok
           or (n.ph is not null and ps.phonetic = n.ph and ps.token <> n.tok
               and ${looksAlike('ps.token', 'n.tok')})
        where n.tok is not null
        order by n.ix, ps.person_id, rank desc, ps.token
     ), pl as (
       select distinct on (n.ix, x.place_id) n.ix, x.place_id, x.kind, x.matched, x.rank
         from norm n
         cross join lateral (
           select p2.id as place_id, 'exact'::text as kind, p2.name as matched, 2 as rank
             from places p2
            where p2.is_deleted = false and search_token(p2.name) = n.tok
           union all
           select a.place_id, 'alias', a.alias, 2
             from place_aliases a
             join places p3 on p3.id = a.place_id and p3.is_deleted = false
            where search_token(a.alias) = n.tok
           union all
           select p4.id, 'phonetic', p4.name, 0
             from places p4
            where p4.is_deleted = false and n.ph is not null
              and dmetaphone(search_token(p4.name)) = n.ph
              and search_token(p4.name) <> n.tok
              and ${looksAlike('search_token(p4.name)', 'n.tok')}
         ) x
        where n.tok is not null
        order by n.ix, x.place_id, x.rank desc
     )
     select n.ix, n.tok, numnode(n.q) as nodes,
            coalesce((select jsonb_agg(jsonb_build_object(
                               'id', pm.person_id, 'kind', pm.kind,
                               'matched', pm.matched, 'name', pe.display_name)
                             order by pm.rank desc, pe.display_name)
                        from pm join people pe on pe.id = pm.person_id and pe.is_deleted = false
                       where pm.ix = n.ix), '[]'::jsonb) as people,
            coalesce((select jsonb_agg(jsonb_build_object(
                               'id', pl.place_id, 'kind', pl.kind,
                               'matched', pl.matched, 'name', p5.name)
                             order by pl.rank desc, p5.name)
                        from pl join places p5 on p5.id = pl.place_id
                       where pl.ix = n.ix), '[]'::jsonb) as places
       from norm n
      order by n.ix`,
    [terms.map((t) => t.text)],
  );
  const byIx = new Map(rows.map((r) => [Number(r.ix) - 1, r]));
  return terms.map((t, i) => {
    const r = byIx.get(i) || {};
    return {
      ...t,
      token: r.tok || null,
      nodes: Number(r.nodes || 0),
      people: (r.people || []).slice(0, MAX_PEOPLE_PER_TERM).map((p) => ({ ...p, id: Number(p.id) })),
      places: (r.places || []).map((p) => ({ ...p, id: Number(p.id) })),
    };
  });
}

// A term with no layer at all can't narrow anything — "the" on its own.
function termHasLayers(t) {
  return Boolean(t.date) || t.nodes > 0 || t.people.length > 0 || t.places.length > 0;
}

// ---- the hit set --------------------------------------------------------

// SQL producing (photo_id, ix, layer, score, detail) rows for one term.
function termHitsSql(term, params) {
  const parts = [];
  const ix = term.ix;

  // People, grouped by how the name matched.
  const byKind = new Map();
  for (const p of term.people) {
    if (!byKind.has(p.kind)) byKind.set(p.kind, []);
    byKind.get(p.kind).push(p.id);
  }
  for (const [kind, ids] of byKind) {
    const score = PERSON_SCORE[kind] || PERSON_SCORE.phonetic;
    params.push(ids);
    const idsP = `$${params.length}::bigint[]`;
    parts.push(`
      select f.photo_id, ${ix} as ix, 'person_${kind}' as layer, ${score} as score,
             f.person_id::text as detail
        from faces f
       where f.person_id = any(${idsP}) and f.is_deleted = false and f.is_disputed = false`);
    parts.push(`
      select s.photo_id, ${ix}, 'person_${kind}_suggested', ${score - SUGGESTED_PENALTY},
             s.payload->>'person_id'
        from suggestions s
       where s.kind = 'person' and s.status = 'pending' and s.photo_id is not null
         and case when s.payload->>'person_id' ~ '^\\d+$'
                  then (s.payload->>'person_id')::bigint end = any(${idsP})`);
  }

  // Places.
  const placeByKind = new Map();
  for (const p of term.places) {
    if (!placeByKind.has(p.kind)) placeByKind.set(p.kind, []);
    placeByKind.get(p.kind).push(p.id);
  }
  for (const [kind, ids] of placeByKind) {
    const score = PLACE_SCORE[kind] || PLACE_SCORE.phonetic;
    params.push(ids);
    const idsP = `$${params.length}::bigint[]`;
    parts.push(`
      select pp.photo_id, ${ix}, 'place_${kind}', ${score}, pp.place_id::text
        from photo_places pp
       where pp.place_id = any(${idsP})`);
    parts.push(`
      select s.photo_id, ${ix}, 'place_${kind}_suggested', ${score - SUGGESTED_PENALTY},
             s.payload->>'place_id'
        from suggestions s
       where s.kind = 'place' and s.status = 'pending' and s.photo_id is not null
         and case when s.payload->>'place_id' ~ '^\\d+$'
                  then (s.payload->>'place_id')::bigint end = any(${idsP})`);
  }

  // Dates — overlap, so a decade-precision photo answers a year search and
  // a year-precision photo answers a decade search.
  if (term.date) {
    params.push(term.date.from, term.date.to);
    const range = `daterange($${params.length - 1}::date, $${params.length}::date, '[]')`;
    parts.push(`
      select p.id, ${ix},
             case when p.capture_date_confirmed then 'date' else 'date_unconfirmed' end,
             -- A photo whose own span sits inside what was asked for is a
             -- tighter answer than a decade-precision photo that merely
             -- overlaps it, so "March 1962" puts the March photo first.
             case when p.capture_date_confirmed then ${DATE_SCORE.date}
                  else ${DATE_SCORE.date_unconfirmed} end
             - case when photo_date_range(p.capture_date, p.capture_date_precision) <@ ${range}
                    then 0 else ${DATE_SCORE.loose_penalty} end,
             to_char(p.capture_date, 'YYYY-MM-DD') || '/' || coalesce(p.capture_date_precision::text, '')
        from photos p
       where p.capture_date is not null
         and photo_date_range(p.capture_date, p.capture_date_precision) && ${range}`);
    parts.push(`
      select s.photo_id, ${ix}, 'date_estimated', ${DATE_SCORE.date_estimated},
             (s.payload->>'date') || '/' || coalesce(s.payload->>'precision', '')
        from suggestions s
       where s.kind = 'date' and s.status = 'pending' and s.photo_id is not null
         and suggestion_date_range(s.payload) && ${range}`);
  }

  // Free text. Weight class tells the card which field matched.
  if (term.nodes > 0) {
    params.push(term.text);
    const q = term.quoted
      ? `phraseto_tsquery('english', search_text($${params.length}))`
      : `plainto_tsquery('english', search_text($${params.length}))`;
    parts.push(`
      select ps.photo_id, ${ix},
             case when ts_filter(ps.tsv, '{a}') @@ qq.q then 'text_a'
                  when ts_filter(ps.tsv, '{b}') @@ qq.q then 'text_b'
                  when ts_filter(ps.tsv, '{c}') @@ qq.q then 'text_c'
                  else 'text_d' end,
             case when ts_filter(ps.tsv, '{a}') @@ qq.q then ${TEXT_SCORE.text_a}
                  when ts_filter(ps.tsv, '{b}') @@ qq.q then ${TEXT_SCORE.text_b}
                  when ts_filter(ps.tsv, '{c}') @@ qq.q then ${TEXT_SCORE.text_c}
                  else ${TEXT_SCORE.text_d} end,
             null::text
        from photo_search ps
        cross join (select ${q} as q) qq
       where ps.tsv @@ qq.q`);
  }

  return parts.join('\n      union all\n');
}

// The CTE every search query starts from.
function hitsCte(terms, params) {
  const blocks = terms.map((t) => termHitsSql(t, params));
  params.push(terms.length);
  const n = `$${params.length}`;
  return `
    with hits (photo_id, ix, layer, score, detail) as (
      ${blocks.join('\n      union all\n')}
    ), best as (
      select distinct on (photo_id, ix) photo_id, ix, layer, score, detail
        from hits
       order by photo_id, ix, score desc, layer, detail
    ), agg as (
      select photo_id, sum(score)::int as score,
             jsonb_agg(jsonb_build_object('t', ix, 'l', layer, 'd', detail) order by ix) as why
        from best
       group by photo_id
      having count(*) = ${n}
    )`;
}

// ---- "why" ---------------------------------------------------------------

function norm(s) {
  return String(s || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
}

// A short quote from `text` around the first word of the term that appears
// in it. Returns null when nothing recognisable is in there.
function snippet(text, term) {
  const hay = norm(text);
  if (!hay) return null;
  const words = norm(term.text).split(/[^a-z0-9']+/).filter((w) => w.length >= 3);
  let at = -1;
  for (const w of words) {
    const stem = w.length > 5 ? w.slice(0, w.length - 1) : w;
    const i = hay.indexOf(stem);
    if (i >= 0 && (at < 0 || i < at)) at = i;
  }
  if (at < 0) return null;
  const raw = String(text);
  const start = Math.max(0, at - 30);
  const end = Math.min(raw.length, at + 70);
  return `${start > 0 ? '…' : ''}${raw.slice(start, end).replace(/\s+/g, ' ').trim()}${end < raw.length ? '…' : ''}`;
}

function dateWhy(detail, layer) {
  const [d, precision] = String(detail || '').split('/');
  const label = layer === 'date'
    ? fmt.dateLabel(d, precision)
    : fmt.dateRange(d, precision);
  if (!label) return layer === 'date_estimated' ? 'Date estimated' : 'Dated';
  if (layer === 'date') return `Dated ${label}`;
  if (layer === 'date_unconfirmed') return `Dated about ${label}`;
  return `Date estimated ${label}`;
}

// One hit → a sentence. `texts` is what the photo actually says (fetched
// for the page's rows only), so a text hit can be quoted.
function whyText(hit, term, texts) {
  const layer = hit.l;
  if (layer.startsWith('person_')) {
    const suggested = layer.endsWith('_suggested');
    const kind = layer.replace(/^person_/, '').replace(/_suggested$/, '');
    const person = term.people.find((p) => String(p.id) === String(hit.d));
    const name = person ? person.name : 'Someone';
    const tail = kind === 'nickname' ? ` (“${term.text}” is a nickname)`
      : kind === 'variant' ? ` (also known as “${person ? person.matched : term.text}”)`
        : kind === 'phonetic' ? ` (sounds like “${term.text}”)` : '';
    return {
      kind: 'person', person_id: person ? person.id : null,
      text: `${name}${tail}${suggested ? ' — suggested, not confirmed' : ' is in this photo'}`,
    };
  }
  if (layer.startsWith('place_')) {
    const suggested = layer.endsWith('_suggested');
    const kind = layer.replace(/^place_/, '').replace(/_suggested$/, '');
    const place = term.places.find((p) => String(p.id) === String(hit.d));
    const name = place ? place.name : 'a place';
    const tail = kind === 'alias' ? ` (also called “${place ? place.matched : term.text}”)`
      : kind === 'phonetic' ? ` (sounds like “${term.text}”)` : '';
    return {
      kind: 'place', place_id: place ? place.id : null,
      text: `${name}${tail}${suggested ? ' — suggested, not confirmed' : ''}`,
    };
  }
  if (layer.startsWith('date')) return { kind: 'date', text: dateWhy(hit.d, layer) };

  const t = texts || {};
  if (layer === 'text_a') {
    const s = snippet(t.description_ai, term);
    if (s) return { kind: 'text', text: `Description: “${s}”` };
    if (t.names && norm(t.names).includes(norm(term.text))) {
      return { kind: 'text', text: `Tagged: ${t.names}` };
    }
    return { kind: 'text', text: 'In the description' };
  }
  if (layer === 'text_b') {
    const s = snippet(t.back_text, term);
    return { kind: 'text', text: s ? `Back of print: “${s}”` : 'Written on the back' };
  }
  if (layer === 'text_c') {
    for (const [field, label] of [['comment_text', 'Comment'], ['pending_desc', 'Suggested description'],
      ['album_text', 'Album'], ['place_text', 'Place'], ['evidence_text', 'Date evidence']]) {
      const s = snippet(t[field], term);
      if (s) return { kind: 'text', text: `${label}: “${s}”` };
    }
    return { kind: 'text', text: 'In a comment or note' };
  }
  const folder = snippet(t.source_folder, term);
  if (folder) return { kind: 'text', text: `Folder: “${t.source_folder}”` };
  const ref = snippet(fmt.physicalRef(t) || '', term);
  if (ref) return { kind: 'text', text: `Print reference: ${fmt.physicalRef(t)}` };
  const file = snippet(t.source_filename, term);
  if (file) return { kind: 'text', text: `File name: ${t.source_filename}` };
  return { kind: 'text', text: 'In the file or folder name' };
}

// The texts behind a page of results, for the quotes above.
async function explainTexts(pool, ids) {
  if (!ids.length) return new Map();
  const { rows } = await pool.query(
    `select p.id, p.description_ai, p.source_folder, p.source_filename,
            p.scan_batch, p.scan_sequence, p.physical_ref_note, ps.names,
            (select string_agg(b.transcribed_text, ' / ')
               from photo_backs b where b.photo_id = p.id and b.transcribed_text is not null) as back_text,
            (select string_agg(c.body, ' / ')
               from comments c where c.photo_id = p.id and c.is_hidden = false) as comment_text,
            (select s.payload->>'text' from suggestions s
              where s.photo_id = p.id and s.status = 'pending' and s.kind = 'description'
              order by s.id desc limit 1) as pending_desc,
            (select string_agg(a.name, ' / ') from album_photos ap
               join albums a on a.id = ap.album_id and a.is_deleted = false
              where ap.photo_id = p.id) as album_text,
            (select string_agg(pc.name, ' / ') from photo_places php
               join places pc on pc.id = php.place_id and pc.is_deleted = false
              where php.photo_id = p.id) as place_text,
            (select string_agg(s.payload->>'evidence', ' / ') from suggestions s
              where s.photo_id = p.id and s.status = 'pending' and s.kind = 'date') as evidence_text
       from photos p
       left join photo_search ps on ps.photo_id = p.id
      where p.id = any($1::bigint[])`,
    [ids],
  );
  return new Map(rows.map((r) => [Number(r.id), r]));
}

// ---- the search ----------------------------------------------------------

function buildWhere(user, params, { scope, filters }) {
  const clauses = [visibleSql(user, params, 'p')];
  const sc = scopeSql(scope, params, 'p');
  if (sc) clauses.push(sc);
  clauses.push(...filtersSql(filters, params, 'p'));
  return clauses;
}

async function runSearch(pool, user, terms, { filters, sort, cursor, limit, scope, withCount }) {
  const sortKey = SEARCH_SORTS.includes(sort) ? sort : 'relevance';
  const sortDef = sortKey === 'relevance' ? RELEVANCE : SORTS[sortKey];
  const lim = Math.max(1, Math.min(Number(limit) || 40, 200));
  const params = [];
  const cte = hitsCte(terms, params);
  const clauses = buildWhere(user, params, { scope, filters });

  const c = typeof cursor === 'string' || cursor == null ? decodeCursor(cursor) : cursor;
  const outer = [];
  const kw = keysetWhere(sortDef, c, params);
  if (kw) outer.push(kw);
  params.push(lim);

  const sql = `${cte}
    select * from (
      select p.id, p.capture_date, to_char(p.capture_date, 'YYYY-MM-DD') as capture_date_str,
             p.capture_date_precision, p.capture_date_confirmed,
             p.scan_batch, p.scan_sequence, p.physical_ref_note,
             p.completeness_score, p.rescan_wanted, p.width, p.height, p.synced_file_version,
             a.score, a.why,
             ${withCount ? 'count(*) over ()::int as total_count,' : ''}
             (select count(*) from likes l where l.photo_id = p.id)::int as like_count
        from agg a
        join photos p on p.id = a.photo_id
       where ${clauses.join('\n         and ')}
    ) x
    ${outer.length ? `where ${outer.join(' and ')}` : ''}
    ${orderBy(sortDef)}
    limit $${params.length}`;
  const { rows } = await pool.query(sql, params);

  const texts = await explainTexts(pool, rows.map((r) => Number(r.id)));
  const items = rows.map((r) => {
    const item = rowToItem(r);
    const t = texts.get(Number(r.id));
    item.score = Number(r.score);
    item.why = (r.why || []).map((h) => {
      const term = terms[Number(h.t)] || { text: '', people: [], places: [] };
      return whyText(h, term, t);
    });
    return item;
  });

  let next = null;
  if (rows.length === lim) {
    const last = rows[rows.length - 1];
    const v = sortDef.col === 'capture_date' ? last.capture_date_str
      : sortDef.col ? last[sortDef.col.replace(/^.*\./, '')] : undefined;
    next = encodeCursor(v, last.id);
  }
  // The window function counts the whole match set before LIMIT, so the
  // first page's count comes free with the page (no second pass).
  const count = withCount
    ? (rows.length ? Number(rows[0].total_count) : 0)
    : null;
  return { items, next, sort: sortKey, count };
}

// Filters with no words: the plain count, same SQL as the grid.
async function countFiltered(pool, user, { filters, scope }) {
  const params = [];
  const clauses = buildWhere(user, params, { scope, filters });
  const { rows } = await pool.query(
    `select count(*)::int as n from photos p where ${clauses.join(' and ')}`, params,
  );
  return rows[0].n;
}

// The whole thing: parse, resolve, run. `query` is the request's query
// string object; `filters` have already been parsed by photos.parseFilters.
async function search(pool, user, {
  q = '', filters = {}, sort = 'relevance', cursor = null, limit = 40, scope = null,
  withCount = false,
} = {}) {
  const started = Date.now();
  const parsed = parseQuery(q);
  const resolved = await resolveTerms(pool, parsed.terms);
  const terms = resolved.filter(termHasLayers);
  const ignored = resolved.filter((t) => !termHasLayers(t)).map((t) => t.text);
  // Terms are re-indexed so `ix` stays dense for the SQL.
  terms.forEach((t, i) => { t.ix = i; });

  const hasFilters = filters.year_from != null || filters.year_to != null
    || filters.person_id != null || filters.place_id != null || filters.album_id != null
    || filters.has_no_date || filters.has_untagged_faces || filters.has_unknown_faces
    || filters.completeness_below != null || filters.low_completeness;

  const out = {
    query: parsed.raw,
    terms,
    ignored,
    truncated: parsed.truncated,
    described: describeQuery({ terms }),
    people: dedupeStrip(terms, 'people'),
    places: dedupeStrip(terms, 'places'),
    empty: !terms.length && !hasFilters,
    photos: { items: [], next: null, sort: 'relevance' },
    count: null,
  };
  if (out.empty) return out;

  if (!terms.length) {
    // Filters only: the ordinary Browse query, relevance meaningless.
    const s = BROWSE_SORTS.includes(sort) ? sort : 'recent';
    out.photos = await listPhotos(pool, user, { filters, sort: s, cursor, limit, scope });
    out.photos.items.forEach((i) => { i.why = []; });
    out.filters_only = true;
  } else {
    out.photos = await runSearch(pool, user, terms, {
      filters, sort, cursor, limit, scope, withCount: withCount && !cursor,
    });
    if (out.photos.count != null) out.count = out.photos.count;
  }
  if (withCount && !cursor && out.count == null) {
    out.count = await countFiltered(pool, user, { filters, scope });
  }

  const ms = Date.now() - started;
  out.took_ms = ms;
  if (ms > SLOW_MS) {
    console.warn(`[search] slow ${ms}ms q=${JSON.stringify(parsed.raw)} `
      + `terms=${JSON.stringify(terms.map((t) => ({ text: t.text, date: t.date ? t.date.label : null, people: t.people.length, places: t.places.length })))} `
      + `sort=${sort} user=${user.id}`);
  }
  return out;
}

// People / places chips above the grid: each resolved person or place once,
// with the plain-words reason it is here.
function dedupeStrip(terms, key) {
  const seen = new Map();
  for (const t of terms) {
    for (const item of t[key]) {
      if (seen.has(item.id)) continue;
      const why = item.kind === 'nickname' ? `nickname of ${t.text}`
        : item.kind === 'variant' ? `also known as ${item.matched}`
          : item.kind === 'phonetic' ? `sounds like ${t.text}`
            : item.kind === 'alias' ? `also called ${item.matched}` : null;
      seen.set(item.id, { id: item.id, name: item.name, kind: item.kind, why, term: t.text });
    }
  }
  return [...seen.values()];
}

// ---- autocomplete + the People page --------------------------------------

// Header box autocomplete: people and places the user can actually reach —
// a person with no visible photo is not offered.
async function autocompleteSearch(pool, user, q, scope) {
  const text = String(q || '').trim().slice(0, 100);
  if (!text) return { people: [], places: [] };
  const params = [text];
  const vis = visibleSql(user, params, 'ph');
  const sc = scopeSql(scope, params, 'ph');
  const photoWhere = [vis, sc].filter(Boolean).join(' and ');
  const prefixIdx = params.push(`${text.toLowerCase().replace(/[%_\\]/g, '')}%`);
  const { rows } = await pool.query(
    `with tok as (
       select search_token($1) as tok,
              case when search_token($1) !~ ' ' then dmetaphone(search_token($1)) end as ph
     ),
     person_hits as (
       select distinct ps.person_id,
              max(case ps.kind when 'exact' then 4 when 'variant' then 3
                               when 'nickname' then 2 else 1 end) as rank
         from person_search ps, tok
        where ps.token = tok.tok or ps.token like $${prefixIdx}
           or (tok.ph is not null and ps.phonetic = tok.ph
               and ${looksAlike('ps.token', 'tok.tok')})
        group by ps.person_id
     )
     select 'person' as kind, pe.id, pe.display_name as name, pe.birth_year, pe.death_year,
            h.rank, cnt.n
       from person_hits h
       join people pe on pe.id = h.person_id and pe.is_deleted = false
       cross join lateral (
         select count(*)::int as n
           from faces f join photos ph on ph.id = f.photo_id
          where f.person_id = pe.id and f.is_deleted = false and f.is_disputed = false
            and ${photoWhere}
       ) cnt
      where cnt.n > 0
      union all
     select 'place', pl.id, pl.name, null, null, 2, cnt.n
       from places pl
       cross join lateral (
         select count(*)::int as n
           from photo_places pp join photos ph on ph.id = pp.photo_id
          where pp.place_id = pl.id and ${photoWhere}
       ) cnt
      where pl.is_deleted = false and cnt.n > 0
        and (search_token(pl.name) = (select tok from tok)
             or lower(pl.name) like $${prefixIdx}
             or exists (select 1 from place_aliases a
                         where a.place_id = pl.id
                           and (search_token(a.alias) = (select tok from tok)
                                or lower(a.alias) like $${prefixIdx})))
      order by rank desc, n desc, name
      limit 10`,
    params,
  );
  const people = [];
  const places = [];
  for (const r of rows) {
    const item = {
      id: Number(r.id), name: r.name, photo_count: r.n,
      birth_year: r.birth_year, death_year: r.death_year,
    };
    (r.kind === 'person' ? people : places).push(item);
  }
  return { people, places };
}

// Person ids a typed name resolves to — the People page uses this so
// "Peggy" finds Margaret there too.
async function resolvePeopleIds(pool, q) {
  const text = String(q || '').trim().slice(0, 100);
  if (!text) return [];
  const { rows } = await pool.query(
    `select distinct ps.person_id
       from person_search ps,
            (select search_token($1) as tok,
                    case when search_token($1) !~ ' ' then dmetaphone(search_token($1)) end as ph) t
      where ps.token = t.tok
         or (t.ph is not null and ps.phonetic = t.ph
             and ${looksAlike('ps.token', 't.tok')})`,
    [text],
  );
  return rows.map((r) => Number(r.person_id));
}

module.exports = {
  search, countFiltered, autocompleteSearch, resolvePeopleIds, resolveTerms,
  parseQuery, describeQuery, snippet, whyText,
  SEARCH_SORTS, RELEVANCE, PERSON_SCORE, PLACE_SCORE, TEXT_SCORE, DATE_SCORE,
};
