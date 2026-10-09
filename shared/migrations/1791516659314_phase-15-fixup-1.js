// Phase 15 fix-up 1 — the last two real deletes.
//
// Phase 15 made every human-visible text editable and gave `album_photos`
// and `photo_places` soft-delete so a *removal* could reach the VM at all.
// It left two tables still doing hard deletes, and both are the same fault
// the Inviolable names:
//
// * **`person_name_variants`** — a nickname removed on the desktop went on
//   showing on the web forever, because the push only upserts. That is the
//   `album_photos` bug with a different table name.
// * **`place_aliases`** — the desktop's removal and `/sync/place_aliases`
//   both issued a real `delete`. "No real deletes, ever" has no exception
//   for text that happens to be its own key.
//
// Both now carry the flags. Two differences from the join tables, each for
// a reason:
//
// 1. `person_name_variants.is_deleted` joins `WEB_EDITABLE` in
//    `routes/sync.js`, so `sync_web_edit_wins` settles a *removal* exactly
//    as it settles a rename. That is a deliberate exception to Phase 15's
//    "is_deleted is a decision and stays desktop-authoritative": the web's
//    People editor can remove a variant, so here the removal really is a
//    human edit either side could make. `deleted_at` travels with the flag
//    it belongs to. `people.is_deleted` (soft-deleting a whole person) is
//    NOT in that list and stays desktop-only.
// 2. `place_aliases` has no `id` and no `edited_on_*` pair, so it stays
//    desktop-authoritative and keeps its replace-the-whole-set push. The
//    route now flags what is absent instead of deleting it.
//
// `person_search` has to stop counting a removed variant, so Phase 11's
// `refresh_people_search_now` is re-created with the filter — the same
// move Phase 15 made for `photo_search`. Its `person_name_variants` update
// trigger already fires on an `is_deleted` flip (it unions `ot` and `nt`),
// so nothing else is needed to keep the index honest.
//
// `down` aborts rather than deleting, per the precedent this fix-up also
// applied to Phase 15's own migration.

export const shorthands = undefined;

export const up = (pgm) => {
  // ---- person_name_variants --------------------------------------
  pgm.addColumns('person_name_variants', {
    is_deleted: { type: 'boolean', notNull: true, default: false },
    deleted_at: { type: 'timestamptz' },
    deleted_by: { type: 'bigint', references: 'users', onDelete: 'SET NULL' },
  });
  pgm.sql(`
    create index person_name_variants_live_idx on person_name_variants (person_id)
      where not is_deleted
  `);

  // ---- place_aliases ---------------------------------------------
  pgm.addColumns('place_aliases', {
    is_deleted: { type: 'boolean', notNull: true, default: false },
    deleted_at: { type: 'timestamptz' },
    deleted_by: { type: 'bigint', references: 'users', onDelete: 'SET NULL' },
    updated_at: { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
  });
  pgm.sql(`
    create trigger place_aliases_set_updated_at before update on place_aliases
      for each row execute function set_updated_at();
  `);
  pgm.sql(`
    create index place_aliases_live_idx on place_aliases (place_id)
      where not is_deleted
  `);

  // ---- person_search stops counting removed variants -------------
  pgm.sql(peopleSearchFn({ liveVariantsOnly: true }));
};

export const down = (pgm) => {
  pgm.sql(peopleSearchFn({ liveVariantsOnly: false }));

  // Dropping the flag makes every removed name live again — a nickname
  // somebody deleted reappears on the person page and back in the search
  // index. Abort and say what to do instead (the `photo-back-orphan`
  // precedent); the removals are all in audit_log.
  pgm.sql(`
    do $$
    declare n_v bigint; n_a bigint;
    begin
      select count(*) into n_v from person_name_variants where is_deleted;
      select count(*) into n_a from place_aliases where is_deleted;
      if n_v > 0 or n_a > 0 then
        raise exception
          'person_name_variants has % soft-deleted row(s) and place_aliases has %. '
          'Refusing to drop is_deleted: without the column every removed '
          'nickname and alias reads as live again and comes back on the '
          'person page and in search. Restore what you want to keep '
          '(set is_deleted = false) or move those rows out before rolling '
          'this back. Every removal is in audit_log under '
          'person.variant_remove / place.alias_remove.', n_v, n_a;
      end if;
    end
    $$;
  `);

  pgm.sql(`drop index if exists place_aliases_live_idx`);
  pgm.sql(`drop trigger if exists place_aliases_set_updated_at on place_aliases`);
  pgm.dropColumns('place_aliases', ['is_deleted', 'deleted_at', 'deleted_by', 'updated_at']);

  pgm.sql(`drop index if exists person_name_variants_live_idx`);
  pgm.dropColumns('person_name_variants', ['is_deleted', 'deleted_at', 'deleted_by']);
};

// Phase 11's refresh_people_search_now, with the variant tokens optionally
// restricted to live rows. One function so `up` and `down` cannot drift.
function peopleSearchFn({ liveVariantsOnly }) {
  const live = liveVariantsOnly ? ' and nv.is_deleted = false' : '';
  return `
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
         where nv.person_id = any(p_ids) and search_token(w.t) is not null${live}
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
  `;
}
