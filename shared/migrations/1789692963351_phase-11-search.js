// Phase 11 — Search.
//
// Two derived tables, maintained entirely by SQL triggers so the desktop
// (Python) and the VM (Node) both stay correct without either knowing
// this exists:
//
//   photo_search  (photo_id, tsv, names, updated_at)
//     One row per photo. `tsv` is the weighted English + unaccent search
//     vector:
//       A  photos.description_ai (the accepted fact) and the names of
//          people whose faces are tagged on the photo
//       B  back-of-print transcriptions
//       C  non-hidden comments, album names, place names, the newest
//          pending `description` suggestion (+ its tags), pending
//          `person` / `place` suggestion names, pending `date` evidence
//       D  source_folder, source_filename, physical_ref_note and the
//          scan locator (`Batch 00012 #017`)
//     `names` is the display names of the tagged people, ' | '-joined:
//     it backs the result card's "why" line and a trigram fallback.
//
//   person_search (person_id, token, kind, phonetic)
//     Every string this person can be called, normalised by
//     search_token(): `exact` (the people columns), `variant`
//     (person_name_variants, whole string and each word) and `nickname`
//     (nickname_dictionary, BOTH directions — Peggy→Margaret and
//     Margaret→Peggy). `phonetic` is dmetaphone() of single-word tokens,
//     which is how a misspelling finds the right person; the caller
//     guards a phonetic-only hit with a pg_trgm similarity floor.
//
// Also: `place_aliases` (composite PK, no bigserial, so the id-range rule
// does not apply) and the date helpers the tolerant date search needs.
//
// Everything Phase 1 built for search (photos.search_tsv,
// refresh_photo_tsv(), its three triggers, people.search_key) is dropped
// here — one search path — and recreated exactly by `down`.
//
// Text is normalised with `unaccent()` before `to_tsvector('english', …)`
// rather than through a custom text-search configuration: the vector is
// stored by a trigger, never computed in an index expression, so the
// STABLE-ness of unaccent() costs nothing and there is no config name to
// resolve under a different search_path (the test runs use per-run
// schemas).

export const shorthands = undefined;

// Tables whose changes invalidate one or more photos' search rows.
// Each entry gets three statement-level triggers (insert / update /
// delete) with transition tables, so a 10 000-row desktop batch costs one
// set-based refresh instead of 10 000 row triggers.
const PHOTO_ID_TABLES = ['photo_backs', 'comments', 'faces', 'album_photos', 'photo_places'];

// Postgres refuses a column list on a trigger that uses transition
// tables, so an update trigger fires on every update of the table and the
// function itself decides which rows actually changed the search text (a
// join of the two transition tables — cheap, and zero refreshes when a
// bulk sync touches only file_version).
function statementTriggers(pgm, table, fn, { updateFn = null } = {}) {
  pgm.sql(`
    create trigger ${table}_search_ins after insert on ${table}
      referencing new table as nt
      for each statement execute function ${fn}();
    create trigger ${table}_search_upd after update on ${table}
      referencing old table as ot new table as nt
      for each statement execute function ${updateFn || fn}();
    create trigger ${table}_search_del after delete on ${table}
      referencing old table as ot
      for each statement execute function ${fn}();
  `);
}

function dropStatementTriggers(pgm, table) {
  pgm.sql(`
    drop trigger if exists ${table}_search_ins on ${table};
    drop trigger if exists ${table}_search_upd on ${table};
    drop trigger if exists ${table}_search_del on ${table};
  `);
}

export const up = (pgm) => {
  // ---- extensions -------------------------------------------------------
  // All three are trusted contrib extensions (PG 13+), so the app role can
  // create them as long as it has CREATE on the database. If it can't, say
  // exactly what a superuser has to run.
  pgm.sql(`create extension if not exists pg_trgm`);
  pgm.sql(`create extension if not exists fuzzystrmatch`);
  pgm.sql(`
    do $$
    begin
      create extension if not exists unaccent;
    exception when insufficient_privilege then
      raise exception 'Phase 11 needs the unaccent extension. Run as a superuser: psql -d % -c "create extension if not exists unaccent"', current_database();
    end
    $$;
  `);

  // ---- normalisation helpers -------------------------------------------
  // search_token: one spelling of a name, comparable across the whole
  // search — lower-cased, unaccented, punctuation to spaces, collapsed.
  // NULL for anything that normalises to nothing.
  pgm.sql(`
    create or replace function search_token(t text) returns text
      language sql stable parallel safe as $$
      select nullif(btrim(regexp_replace(
               regexp_replace(lower(unaccent(coalesce(t, ''))), '[^a-z0-9'']+', ' ', 'g'),
               '\\s+', ' ', 'g')), '')
    $$;
  `);

  // search_text: the same normalisation for free text going into a tsvector.
  pgm.sql(`
    create or replace function search_text(t text) returns text
      language sql stable parallel safe as $$
      select unaccent(coalesce(t, ''))
    $$;
  `);

  // The span a dated photo actually covers, given its precision. A
  // decade-precision photo overlaps every year in its decade; a
  // year-precision photo overlaps its decade. Search matches on overlap.
  // The implementation takes the enum, because this is what the GiST index
  // below is built on, and from Postgres 17 a CREATE INDEX evaluates its
  // expression with search_path restricted to pg_catalog — so the body may
  // not name anything in the application schema. Everything here is
  // pg_catalog: make_date, date_trunc, daterange, and enum equality against
  // an unknown-typed literal.
  pgm.sql(`
    create or replace function photo_date_range(d date, p date_precision)
      returns daterange language sql immutable parallel safe as $$
      select case
        when d is null then null
        when p = 'decade' then
          daterange(make_date((extract(year from d)::int / 10) * 10, 1, 1),
                    make_date((extract(year from d)::int / 10) * 10 + 9, 12, 31), '[]')
        when p = 'year' then
          daterange(make_date(extract(year from d)::int, 1, 1),
                    make_date(extract(year from d)::int, 12, 31), '[]')
        when p = 'month' then
          daterange(date_trunc('month', d::timestamp)::date,
                    (date_trunc('month', d::timestamp) + interval '1 month' - interval '1 day')::date, '[]')
        else daterange(d, d, '[]')
      end
    $$;
  `);

  // The text overload is for payloads, where the precision is whatever a
  // writer put in the JSON: an unknown word is treated as an exact date
  // rather than raising.
  pgm.sql(`
    create or replace function photo_date_range(d date, precision_text text)
      returns daterange language sql immutable parallel safe as $$
      select photo_date_range(d, case
        when precision_text in ('exact', 'month', 'year', 'decade', 'unknown')
          then precision_text::date_precision
        else 'exact'::date_precision end)
    $$;
  `);

  // A date search is an overlap test against that span, so it gets its own
  // GiST index — a decade search over 14 000 photos is 80 ms as a sequential
  // scan and a few ms with this.
  pgm.sql(`
    create index photos_date_range_gist on photos
      using gist (photo_date_range(capture_date, capture_date_precision))
  `);

  // Same, for a pending `date` suggestion's payload. Bad payloads are NULL,
  // never an error.
  pgm.sql(`
    create or replace function suggestion_date_range(payload jsonb)
      returns daterange language sql immutable parallel safe as $$
      select case
        when payload->>'date' ~ '^\\d{4}-\\d{2}-\\d{2}$'
          then photo_date_range((payload->>'date')::date,
                                coalesce(payload->>'precision', 'exact'))
      end
    $$;
  `);

  // Searchable text of a suggestion payload, per kind. `classification`
  // is deliberately absent — the model's reasoning sentence would swamp
  // every other layer.
  pgm.sql(`
    create or replace function search_suggestion_text(kind_text text, payload jsonb)
      returns text language sql immutable parallel safe as $$
      select case kind_text
        when 'description' then btrim(concat_ws(' ', payload->>'text',
          case when jsonb_typeof(payload->'tags') = 'array'
               then (select string_agg(v, ' ') from jsonb_array_elements_text(payload->'tags') v) end))
        when 'date' then payload->>'evidence'
        when 'person' then btrim(concat_ws(' ', payload#>>'{new_person,given_name}',
                                                payload#>>'{new_person,middle_name}',
                                                payload#>>'{new_person,surname}'))
        when 'place' then payload#>>'{new_place,name}'
      end
    $$;
  `);

  // ---- tables -----------------------------------------------------------
  pgm.createTable('photo_search', {
    photo_id:   { type: 'bigint', primaryKey: true, references: 'photos', onDelete: 'CASCADE' },
    tsv:        { type: 'tsvector' },
    names:      { type: 'text' },
    updated_at: { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
  });
  pgm.sql(`create index photo_search_tsv_idx on photo_search using gin (tsv)`);
  pgm.sql(`create index photo_search_names_trgm on photo_search using gin (names gin_trgm_ops)`);

  pgm.createTable('person_search', {
    person_id: { type: 'bigint', notNull: true, references: 'people', onDelete: 'CASCADE' },
    token:     { type: 'text', notNull: true },
    kind:      { type: 'text', notNull: true },
    phonetic:  { type: 'text' },
  });
  pgm.addConstraint('person_search', 'person_search_pk', {
    primaryKey: ['person_id', 'token', 'kind'],
  });
  // The four kinds mirror the four name layers. Rows are stored as
  // exact / variant / nickname; `phonetic` is what the phonetic layer
  // matches on, and a hand-added phonetic-only token is allowed.
  pgm.addConstraint('person_search', 'person_search_kind_check', {
    check: `kind in ('exact', 'variant', 'nickname', 'phonetic')`,
  });
  pgm.sql(`create index person_search_token_idx on person_search (token)`);
  pgm.sql(`create index person_search_token_trgm on person_search using gin (token gin_trgm_ops)`);
  pgm.sql(`create index person_search_phonetic_idx on person_search (phonetic)`);

  // Place aliases: "Gran's house", "the lake". Composite PK on purpose —
  // no bigserial, so the desktop/web id-range rule (shared/id-ranges.json)
  // does not apply. No sync route yet (Phase 12, with albums).
  pgm.createTable('place_aliases', {
    place_id:   { type: 'bigint', notNull: true, references: 'places', onDelete: 'CASCADE' },
    alias:      { type: 'text', notNull: true },
    kind:       { type: 'text', notNull: true, default: 'alias' },
    created_at: { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
  });
  pgm.addConstraint('place_aliases', 'place_aliases_pk', { primaryKey: ['place_id', 'alias'] });
  pgm.sql(`create unique index place_aliases_ci_uq on place_aliases (place_id, lower(alias))`);
  pgm.sql(`create index place_aliases_trgm on place_aliases using gin (alias gin_trgm_ops)`);

  // ---- the bulk-write escape hatch --------------------------------------
  // Measured cost of the triggers (photoorg_test, 5 000 faces + 5 000
  // suggestions over 5 000 photos in one transaction): 0.27 s without,
  // 5.07 s with — 4.8 s of trigger work, over the 2 s we were willing to
  // pay. So a session doing a big batch can defer the work:
  //
  //   set local photoarchive.search_defer = on;   -- queue instead of refresh
  //   …thousands of inserts…
  //   commit;
  //   select sweep_search();                      -- drains both queues
  //
  // Off by default: nothing sets it today, so every writer (desktop and
  // web alike) still gets a correct search row on commit. If you do turn
  // it on, sweeping is not optional — the queue is the only record of
  // what is stale.
  pgm.createTable('photo_search_dirty', {
    photo_id: { type: 'bigint', primaryKey: true },
  });
  pgm.createTable('person_search_dirty', {
    person_id: { type: 'bigint', primaryKey: true },
  });
  pgm.sql(`
    create or replace function search_deferred() returns boolean
      language sql stable parallel safe as $$
      select coalesce(current_setting('photoarchive.search_defer', true), 'off')
             in ('on', 'true', '1', 'yes')
    $$;
  `);

  // ---- photo_search maintenance ----------------------------------------
  // One set-based upsert for a batch of photo ids. Every trigger, the
  // sweep and the full rebuild go through this, so there is one
  // definition of the vector.
  pgm.sql(`
    create or replace function refresh_photos_search_now(p_ids bigint[]) returns void
      language sql as $$
      insert into photo_search (photo_id, tsv, names, updated_at)
      select p.id,
             setweight(to_tsvector('english', search_text(p.description_ai)), 'A')
          || setweight(to_tsvector('english', search_text(pe.name_parts)), 'A')
          || setweight(to_tsvector('english', search_text(bk.txt)), 'B')
          || setweight(to_tsvector('english', search_text(
               concat_ws(' ', cm.txt, al.txt, pl.txt, sg.txt, sp.txt))), 'C')
          || setweight(to_tsvector('english', search_text(
               concat_ws(' ', p.source_folder, p.source_filename, p.physical_ref_note,
                              p.scan_batch,
                              case when p.scan_sequence is not null
                                   then '#' || p.scan_sequence::text end))), 'D'),
             pe.display_names,
             now()
        from photos p
        left join lateral (
          select string_agg(distinct concat_ws(' ', pp.given_name, pp.middle_name, pp.surname,
                                                    pp.maiden_name, pp.nickname, pp.suffix), ' ') as name_parts,
                 string_agg(distinct pp.display_name, ' | ') as display_names
            from faces f
            join people pp on pp.id = f.person_id
           where f.photo_id = p.id and f.is_deleted = false and f.is_disputed = false
             and pp.is_deleted = false
        ) pe on true
        left join lateral (
          select string_agg(b.transcribed_text, ' ') as txt
            from photo_backs b
           where b.photo_id = p.id and b.transcribed_text is not null
        ) bk on true
        left join lateral (
          select string_agg(c.body, ' ') as txt
            from comments c
           where c.photo_id = p.id and c.is_hidden = false
        ) cm on true
        left join lateral (
          select string_agg(distinct a.name, ' ') as txt
            from album_photos ap
            join albums a on a.id = ap.album_id and a.is_deleted = false
           where ap.photo_id = p.id
        ) al on true
        left join lateral (
          select string_agg(distinct pc.name, ' ') as txt
            from photo_places php
            join places pc on pc.id = php.place_id and pc.is_deleted = false
           where php.photo_id = p.id
        ) pl on true
        left join lateral (
          -- The newest pending description (George will never hand-accept
          -- 12k of them), plus every pending date evidence and the names
          -- inside new-person / new-place suggestions.
          select concat_ws(' ',
            (select search_suggestion_text(s.kind::text, s.payload)
               from suggestions s
              where s.photo_id = p.id and s.status = 'pending' and s.kind = 'description'
              order by s.id desc limit 1),
            (select string_agg(search_suggestion_text(s.kind::text, s.payload), ' ')
               from suggestions s
              where s.photo_id = p.id and s.status = 'pending'
                and s.kind in ('date', 'person', 'place'))
          ) as txt
        ) sg on true
        left join lateral (
          select string_agg(distinct concat_ws(' ', sp2.given_name, sp2.surname,
                                                    sp2.nickname, sp2.maiden_name), ' ') as txt
            from suggestions s
            join people sp2 on sp2.id = case when s.payload->>'person_id' ~ '^\\d+$'
                                             then (s.payload->>'person_id')::bigint end
           where s.photo_id = p.id and s.status = 'pending' and s.kind = 'person'
             and sp2.is_deleted = false
        ) sp on true
       where p.id = any(p_ids)
      on conflict (photo_id) do update
         set tsv = excluded.tsv, names = excluded.names, updated_at = now();
    $$;
  `);

  pgm.sql(`
    create or replace function refresh_photos_search(p_ids bigint[]) returns void
      language plpgsql as $$
    begin
      if search_deferred() then
        insert into photo_search_dirty (photo_id)
          select unnest(p_ids) on conflict do nothing;
      else
        perform refresh_photos_search_now(p_ids);
      end if;
    end
    $$;
  `);
  pgm.sql(`
    create or replace function refresh_photo_search(p_photo_id bigint) returns void
      language sql as $$ select refresh_photos_search(array[p_photo_id]) $$;
  `);

  // ---- person_search maintenance ---------------------------------------
  pgm.sql(`
    create or replace function refresh_people_search_now(p_ids bigint[]) returns void
      language plpgsql as $$
    begin
      delete from person_search where person_id = any(p_ids);

      insert into person_search (person_id, token, kind, phonetic)
      with exact_tokens as (
        select pe.id as person_id, search_token(v.t) as token
          from people pe
          cross join lateral (values (pe.given_name), (pe.middle_name), (pe.surname),
                                     (pe.maiden_name), (pe.nickname), (pe.suffix)) v(t)
         where pe.id = any(p_ids) and pe.is_deleted = false
           and search_token(v.t) is not null
      ),
      variant_tokens as (
        select nv.person_id, search_token(w.t) as token
          from person_name_variants nv
          cross join lateral (
            select nv.variant as t
            union all
            select regexp_split_to_table(nv.variant, '\\s+')
          ) w(t)
         where nv.person_id = any(p_ids) and search_token(w.t) is not null
      ),
      all_names as (
        select person_id, token from exact_tokens
        union
        select person_id, token from variant_tokens
      ),
      nickname_tokens as (
        -- Both directions: a person called Margaret answers to Peggy, and
        -- a person called Peggy answers to Margaret.
        select a.person_id, search_token(n.t) as token
          from all_names a
          cross join lateral (
            select d.variant as t from nickname_dictionary d where lower(d.canonical) = a.token
            union all
            select d.canonical from nickname_dictionary d where lower(d.variant) = a.token
          ) n
         where search_token(n.t) is not null
      ),
      rows_in as (
        select person_id, token, 'exact'    as kind from exact_tokens
        union
        select person_id, token, 'variant'  from variant_tokens
        union
        select person_id, token, 'nickname' from nickname_tokens
      )
      select person_id, token, kind,
             case when token !~ ' ' then dmetaphone(token) end
        from rows_in
       where length(token) >= 2
      on conflict do nothing;
    end
    $$;
  `);

  pgm.sql(`
    create or replace function refresh_people_search(p_ids bigint[]) returns void
      language plpgsql as $$
    begin
      if search_deferred() then
        insert into person_search_dirty (person_id)
          select unnest(p_ids) on conflict do nothing;
      else
        perform refresh_people_search_now(p_ids);
      end if;
    end
    $$;
  `);
  pgm.sql(`
    create or replace function refresh_person_search(p_person_id bigint) returns void
      language sql as $$ select refresh_people_search(array[p_person_id]) $$;
  `);

  // Drain the queues. Safe to call any time; returns what it did.
  pgm.sql(`
    create or replace function sweep_search() returns text
      language plpgsql as $$
    declare batch bigint[];
            people_done bigint := 0;
            photos_done bigint := 0;
    begin
      loop
        select array_agg(person_id) into batch
          from (select person_id from person_search_dirty order by person_id limit 500) b;
        exit when batch is null;
        delete from person_search_dirty where person_id = any(batch);
        perform refresh_people_search_now(
          array(select id from people where id = any(batch)));
        people_done := people_done + array_length(batch, 1);
      end loop;
      loop
        select array_agg(photo_id) into batch
          from (select photo_id from photo_search_dirty order by photo_id limit 2000) b;
        exit when batch is null;
        delete from photo_search_dirty where photo_id = any(batch);
        perform refresh_photos_search_now(batch);
        photos_done := photos_done + array_length(batch, 1);
      end loop;
      return 'swept people: ' || people_done::text || ', photos: ' || photos_done::text;
    end
    $$;
  `);

  // ---- triggers ---------------------------------------------------------
  // Generic: any table with a photo_id column.
  pgm.sql(`
    create or replace function photo_search_touch_photo_id() returns trigger
      language plpgsql as $$
    declare ids bigint[];
    begin
      if tg_op = 'INSERT' then
        select array_agg(distinct photo_id) into ids from nt where photo_id is not null;
      elsif tg_op = 'DELETE' then
        select array_agg(distinct photo_id) into ids from ot where photo_id is not null;
      else
        select array_agg(distinct photo_id) into ids
          from (select photo_id from nt union select photo_id from ot) u
         where photo_id is not null;
      end if;
      if ids is not null then perform refresh_photos_search(ids); end if;
      return null;
    end
    $$;
  `);

  // faces: only the columns that change what a photo's search row says.
  // The jobs runner updates embeddings on tens of thousands of rows;
  // those must not cost a refresh.
  pgm.sql(`
    create or replace function photo_search_touch_faces_upd() returns trigger
      language plpgsql as $$
    declare ids bigint[];
    begin
      select array_agg(distinct pid) into ids from (
        select nt.photo_id as pid from nt join ot on ot.id = nt.id
         where nt.person_id is distinct from ot.person_id
            or nt.is_deleted is distinct from ot.is_deleted
            or nt.is_disputed is distinct from ot.is_disputed
            or nt.photo_id is distinct from ot.photo_id
        union
        select ot.photo_id from ot join nt on nt.id = ot.id
         where nt.photo_id is distinct from ot.photo_id
      ) u where pid is not null;
      if ids is not null then perform refresh_photos_search(ids); end if;
      return null;
    end
    $$;
  `);

  // Suggestions: only the kinds that carry searchable text.
  pgm.sql(`
    create or replace function photo_search_touch_suggestions() returns trigger
      language plpgsql as $$
    declare ids bigint[];
    begin
      if tg_op = 'INSERT' then
        select array_agg(distinct photo_id) into ids from nt
         where photo_id is not null and kind in ('description', 'date', 'person', 'place');
      elsif tg_op = 'DELETE' then
        select array_agg(distinct photo_id) into ids from ot
         where photo_id is not null and kind in ('description', 'date', 'person', 'place');
      else
        select array_agg(distinct photo_id) into ids from (
          select photo_id from nt where kind in ('description', 'date', 'person', 'place')
          union
          select photo_id from ot where kind in ('description', 'date', 'person', 'place')
        ) u where photo_id is not null;
      end if;
      if ids is not null then perform refresh_photos_search(ids); end if;
      return null;
    end
    $$;
  `);

  // photos itself (insert, and updates to the text columns).
  pgm.sql(`
    create or replace function photo_search_touch_photos() returns trigger
      language plpgsql as $$
    declare ids bigint[];
    begin
      -- Insert only; a delete cascades photo_search away.
      select array_agg(id) into ids from nt;
      if ids is not null then perform refresh_photos_search(ids); end if;
      return null;
    end
    $$;
  `);

  // photos update: only the text columns matter. A sync push bumping
  // file_version, or a triage decision, refreshes nothing.
  pgm.sql(`
    create or replace function photo_search_touch_photos_upd() returns trigger
      language plpgsql as $$
    declare ids bigint[];
    begin
      select array_agg(nt.id) into ids
        from nt join ot on ot.id = nt.id
       where nt.description_ai is distinct from ot.description_ai
          or nt.source_folder is distinct from ot.source_folder
          or nt.source_filename is distinct from ot.source_filename
          or nt.physical_ref_note is distinct from ot.physical_ref_note
          or nt.scan_batch is distinct from ot.scan_batch
          or nt.scan_sequence is distinct from ot.scan_sequence;
      if ids is not null then perform refresh_photos_search(ids); end if;
      return null;
    end
    $$;
  `);

  // An album or place rename changes the text of every photo in it.
  pgm.sql(`
    create or replace function photo_search_touch_albums() returns trigger
      language plpgsql as $$
    declare ids bigint[];
    begin
      select array_agg(distinct ap.photo_id) into ids
        from album_photos ap
       where ap.album_id in (select nt.id from nt join ot on ot.id = nt.id
                              where nt.name is distinct from ot.name
                                 or nt.is_deleted is distinct from ot.is_deleted);
      if ids is not null then perform refresh_photos_search(ids); end if;
      return null;
    end
    $$;
  `);
  pgm.sql(`
    create or replace function photo_search_touch_places() returns trigger
      language plpgsql as $$
    declare ids bigint[];
    begin
      select array_agg(distinct pp.photo_id) into ids
        from photo_places pp
       where pp.place_id in (select nt.id from nt join ot on ot.id = nt.id
                              where nt.name is distinct from ot.name
                                 or nt.is_deleted is distinct from ot.is_deleted);
      if ids is not null then perform refresh_photos_search(ids); end if;
      return null;
    end
    $$;
  `);

  // people: rebuild their tokens, and the vector of every photo they are
  // tagged in or suggested for.
  pgm.sql(`
    create or replace function person_search_touch_people() returns trigger
      language plpgsql as $$
    declare pids bigint[];
            ids bigint[];
    begin
      if tg_op = 'INSERT' then
        select array_agg(id) into pids from nt;
      elsif tg_op = 'DELETE' then
        select array_agg(id) into pids from ot;
      else
        -- Only a name change (or a soft delete) changes what this person
        -- is findable by; notes and years do not.
        select array_agg(nt.id) into pids
          from nt join ot on ot.id = nt.id
         where nt.given_name is distinct from ot.given_name
            or nt.middle_name is distinct from ot.middle_name
            or nt.surname is distinct from ot.surname
            or nt.maiden_name is distinct from ot.maiden_name
            or nt.nickname is distinct from ot.nickname
            or nt.suffix is distinct from ot.suffix
            or nt.display_name is distinct from ot.display_name
            or nt.is_deleted is distinct from ot.is_deleted;
      end if;
      if pids is null then return null; end if;
      if tg_op <> 'DELETE' then perform refresh_people_search(pids); end if;
      select array_agg(distinct photo_id) into ids from (
        select f.photo_id from faces f
         where f.person_id = any(pids) and f.is_deleted = false and f.photo_id is not null
        union
        select s.photo_id from suggestions s
         where s.kind = 'person' and s.status = 'pending' and s.photo_id is not null
           and case when s.payload->>'person_id' ~ '^\\d+$'
                    then (s.payload->>'person_id')::bigint end = any(pids)
      ) u;
      if ids is not null then perform refresh_photos_search(ids); end if;
      return null;
    end
    $$;
  `);

  pgm.sql(`
    create or replace function person_search_touch_variants() returns trigger
      language plpgsql as $$
    declare pids bigint[];
    begin
      if tg_op = 'INSERT' then
        select array_agg(distinct person_id) into pids from nt;
      elsif tg_op = 'DELETE' then
        select array_agg(distinct person_id) into pids from ot;
      else
        select array_agg(distinct person_id) into pids
          from (select person_id from nt union select person_id from ot) u;
      end if;
      if pids is not null then perform refresh_people_search(pids); end if;
      return null;
    end
    $$;
  `);

  for (const t of PHOTO_ID_TABLES) {
    statementTriggers(pgm, t, 'photo_search_touch_photo_id', {
      updateFn: t === 'faces' ? 'photo_search_touch_faces_upd' : null,
    });
  }
  statementTriggers(pgm, 'suggestions', 'photo_search_touch_suggestions');
  pgm.sql(`
    create trigger photos_search_ins after insert on photos
      referencing new table as nt
      for each statement execute function photo_search_touch_photos();
    create trigger photos_search_upd after update on photos
      referencing old table as ot new table as nt
      for each statement execute function photo_search_touch_photos_upd();
  `);
  pgm.sql(`
    create trigger albums_search_upd after update on albums
      referencing old table as ot new table as nt
      for each statement execute function photo_search_touch_albums();
    create trigger places_search_upd after update on places
      referencing old table as ot new table as nt
      for each statement execute function photo_search_touch_places();
  `);
  pgm.sql(`
    create trigger people_search_ins after insert on people
      referencing new table as nt
      for each statement execute function person_search_touch_people();
    create trigger people_search_upd after update on people
      referencing old table as ot new table as nt
      for each statement execute function person_search_touch_people();
    create trigger people_search_del after delete on people
      referencing old table as ot
      for each statement execute function person_search_touch_people();
  `);
  statementTriggers(pgm, 'person_name_variants', 'person_search_touch_variants');

  // ---- full rebuild ------------------------------------------------------
  pgm.sql(`
    create or replace function rebuild_person_search() returns bigint
      language plpgsql as $$
    declare n bigint;
    begin
      perform refresh_people_search_now(array(select id from people where is_deleted = false));
      select count(*) into n from person_search;
      return n;
    end
    $$;
  `);
  pgm.sql(`
    create or replace function rebuild_photo_search() returns bigint
      language plpgsql as $$
    declare batch bigint[];
            last_id bigint := 0;
            n bigint := 0;
    begin
      loop
        select array_agg(id) into batch
          from (select id from photos where id > last_id order by id limit 2000) b;
        exit when batch is null;
        perform refresh_photos_search_now(batch);
        last_id := batch[array_length(batch, 1)];
        n := n + array_length(batch, 1);
      end loop;
      return n;
    end
    $$;
  `);
  pgm.sql(`
    create or replace function rebuild_search() returns text
      language sql as $$
      select 'people: ' || rebuild_person_search()::text
          || ', photos: ' || rebuild_photo_search()::text
    $$;
  `);

  // ---- retire the Phase 1 search machinery ------------------------------
  pgm.sql(`drop trigger if exists photo_backs_refresh_tsv on photo_backs`);
  pgm.sql(`drop function if exists photo_backs_refresh_tsv_trg()`);
  pgm.sql(`drop trigger if exists comments_refresh_tsv on comments`);
  pgm.sql(`drop function if exists comments_refresh_tsv_trg()`);
  pgm.sql(`drop trigger if exists photos_refresh_tsv on photos`);
  pgm.sql(`drop function if exists photos_refresh_tsv_trg()`);
  pgm.sql(`drop function if exists refresh_photo_tsv(bigint)`);
  pgm.sql(`drop index if exists photos_search_tsv_idx`);
  pgm.dropColumns('photos', ['search_tsv']);

  pgm.sql(`drop trigger if exists people_search_key_trg on people`);
  pgm.sql(`drop function if exists people_set_search_key()`);
  pgm.sql(`drop function if exists people_compute_search_key(people)`);
  pgm.dropColumns('people', ['search_key']);

  // ---- build it ---------------------------------------------------------
  pgm.sql(`select rebuild_search()`);
};

export const down = (pgm) => {
  // The index depends on photo_date_range(), so it goes first.
  pgm.sql(`drop index if exists photos_date_range_gist`);
  for (const t of PHOTO_ID_TABLES) dropStatementTriggers(pgm, t);
  dropStatementTriggers(pgm, 'suggestions');
  dropStatementTriggers(pgm, 'person_name_variants');
  pgm.sql(`
    drop trigger if exists photos_search_ins on photos;
    drop trigger if exists photos_search_upd on photos;
    drop trigger if exists albums_search_upd on albums;
    drop trigger if exists places_search_upd on places;
    drop trigger if exists people_search_ins on people;
    drop trigger if exists people_search_upd on people;
    drop trigger if exists people_search_del on people;
  `);
  pgm.sql(`
    drop function if exists photo_search_touch_photo_id();
    drop function if exists photo_search_touch_faces_upd();
    drop function if exists photo_search_touch_suggestions();
    drop function if exists photo_search_touch_photos();
    drop function if exists photo_search_touch_photos_upd();
    drop function if exists photo_search_touch_albums();
    drop function if exists photo_search_touch_places();
    drop function if exists person_search_touch_people();
    drop function if exists person_search_touch_variants();
    drop function if exists rebuild_search();
    drop function if exists rebuild_photo_search();
    drop function if exists rebuild_person_search();
    drop function if exists sweep_search();
    drop function if exists refresh_photo_search(bigint);
    drop function if exists refresh_photos_search(bigint[]);
    drop function if exists refresh_photos_search_now(bigint[]);
    drop function if exists refresh_person_search(bigint);
    drop function if exists refresh_people_search(bigint[]);
    drop function if exists refresh_people_search_now(bigint[]);
    drop function if exists search_deferred();
    drop function if exists search_suggestion_text(text, jsonb);
    drop function if exists suggestion_date_range(jsonb);
    drop function if exists photo_date_range(date, date_precision);
    drop function if exists photo_date_range(date, text);
    drop function if exists search_text(text);
    drop function if exists search_token(text);
  `);
  pgm.dropTable('place_aliases');
  pgm.dropTable('person_search_dirty');
  pgm.dropTable('photo_search_dirty');
  pgm.dropTable('person_search');
  pgm.dropTable('photo_search');

  // ---- put Phase 1's search back exactly --------------------------------
  pgm.addColumns('photos', { search_tsv: { type: 'tsvector' } });
  pgm.sql(`create index photos_search_tsv_idx on photos using gin (search_tsv)`);
  pgm.sql(`
    create or replace function refresh_photo_tsv(p_photo_id bigint) returns void
      language plpgsql as $$
    declare
      v_desc text;
      v_comments text;
      v_backs text;
    begin
      select coalesce(description_ai, '') into v_desc from photos where id = p_photo_id;
      if not found then return; end if;

      select coalesce(string_agg(body, ' '), '') into v_comments
        from comments where photo_id = p_photo_id and not is_hidden;

      select coalesce(string_agg(transcribed_text, ' '), '') into v_backs
        from photo_backs where photo_id = p_photo_id and transcribed_text is not null;

      update photos
         set search_tsv = to_tsvector('english', v_desc || ' ' || v_comments || ' ' || v_backs)
       where id = p_photo_id;
    end
    $$;
  `);
  pgm.sql(`
    create or replace function photos_refresh_tsv_trg() returns trigger
      language plpgsql as $$
    begin
      if (tg_op = 'UPDATE'
          and new.description_ai is not distinct from old.description_ai) then
        return new;
      end if;
      perform refresh_photo_tsv(new.id);
      return new;
    end
    $$;
  `);
  pgm.sql(`
    create trigger photos_refresh_tsv
      after insert or update of description_ai on photos
      for each row execute function photos_refresh_tsv_trg();
  `);
  pgm.sql(`
    create or replace function comments_refresh_tsv_trg() returns trigger
      language plpgsql as $$
    begin
      if tg_op = 'DELETE' then
        perform refresh_photo_tsv(old.photo_id);
        return old;
      end if;
      perform refresh_photo_tsv(new.photo_id);
      if tg_op = 'UPDATE' and new.photo_id <> old.photo_id then
        perform refresh_photo_tsv(old.photo_id);
      end if;
      return new;
    end
    $$;
  `);
  pgm.sql(`
    create trigger comments_refresh_tsv
      after insert or update or delete on comments
      for each row execute function comments_refresh_tsv_trg();
  `);
  pgm.sql(`
    create or replace function photo_backs_refresh_tsv_trg() returns trigger
      language plpgsql as $$
    begin
      if tg_op = 'DELETE' then
        perform refresh_photo_tsv(old.photo_id);
        return old;
      end if;
      perform refresh_photo_tsv(new.photo_id);
      if tg_op = 'UPDATE' and new.photo_id <> old.photo_id then
        perform refresh_photo_tsv(old.photo_id);
      end if;
      return new;
    end
    $$;
  `);
  pgm.sql(`
    create trigger photo_backs_refresh_tsv
      after insert or update or delete on photo_backs
      for each row execute function photo_backs_refresh_tsv_trg();
  `);
  pgm.sql(`select refresh_photo_tsv(id) from photos`);

  pgm.addColumns('people', { search_key: { type: 'text' } });
  pgm.createIndex('people', 'search_key');
  pgm.sql(`
    create or replace function people_compute_search_key(p people) returns text
      language sql immutable as $$
      select trim(both ' ' from
        coalesce(metaphone(p.surname,     16), '') || ' ' ||
        coalesce(metaphone(p.maiden_name, 16), '') || ' ' ||
        coalesce(metaphone(p.given_name,  16), '')
      )
    $$;
  `);
  pgm.sql(`
    create or replace function people_set_search_key() returns trigger
      language plpgsql as $$
    begin
      new.search_key := people_compute_search_key(new);
      return new;
    end
    $$;
  `);
  pgm.sql(`
    create trigger people_search_key_trg before insert or update on people
      for each row execute function people_set_search_key();
  `);
  // The BEFORE trigger above fills search_key on any update.
  pgm.sql(`update people set given_name = given_name`);
};
