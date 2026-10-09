// Phase 15 — Corrections: fixing a mistake must never need SQL.
//
// Three groups of changes, all in service of one rule: every human-visible
// text is editable in a UI, the edit is audited, and it reaches every copy
// on its own.
//
// 1. `edited_on_desktop_at` / `edited_on_web_at` — the third and fourth
//    members of the `tombstoned_at` family. Every `/sync/*` upsert sets
//    `updated_at = now()`, so on the web `updated_at` means "when a push
//    last touched this row", never "when a human edited it". A
//    last-writer-wins between the tiers therefore cannot read `updated_at`
//    on either side: after any push the web's is newer than the laptop's
//    for every row, edited or not. These two columns are written ONLY by a
//    human edit in a UI (desktop Corrections / People / album editors; web
//    admin editors) and never by a job, an upsert or a sweep, so the
//    comparison is human-edit vs human-edit. Ties go to the web — it is the
//    copy a relative is looking at.
//
// 2. Soft-delete on `album_photos` and `photo_places`. Both were
//    insert-or-update-only and the push only upserts, so a desktop
//    "remove this photo from the album" left the web's row in place
//    forever and the site kept showing the photo in the album. They now
//    carry the same `is_deleted / deleted_at / deleted_by / updated_at`
//    shape as `photo_groups` and sync by the same LWW rule. Every reader
//    must filter `is_deleted = false` — including the search vector, whose
//    two lateral joins are re-created below.
//
// 3. `comments.edited_at`. Comment bodies had no edit path at all
//    (insert-only; the only correction was hide/unhide). The author may now
//    edit their own and an admin any, and an edited comment says so — text
//    must never change silently under a relative who already read it.

export const shorthands = undefined;

// The tables the web can edit and the desktop can push.
const EDITABLE = ['suggestions', 'people', 'person_name_variants', 'places', 'albums'];

// The join tables gaining soft-delete.
const JOIN_TABLES = ['album_photos', 'photo_places'];

export const up = (pgm) => {
  // ---------------------------------------------------------------- 1
  for (const t of EDITABLE) {
    pgm.addColumns(t, {
      edited_on_desktop_at: { type: 'timestamptz' },
      edited_on_web_at:     { type: 'timestamptz' },
    });
    pgm.sql(`
      comment on column ${t}.edited_on_desktop_at is
        'Set only by a human edit in the desktop UI. Never by a job, sync or sweep.';
      comment on column ${t}.edited_on_web_at is
        'Set only by a human edit in the web UI. Never by a job, sync or sweep. Wins ties.';
    `);
  }

  // The rule itself, as one function, so the five `/sync/*` upserts that
  // apply it cannot each spell it slightly differently. "The web's human
  // edit wins" means: the web has one at all, and the desktop either has
  // none or made its edit no later. Ties go to the web (`>=`) — it is the
  // copy a relative is looking at, and a tie means the two edits are
  // indistinguishable in time anyway.
  pgm.sql(`
    create or replace function sync_web_edit_wins(
      web_edited timestamptz, desktop_edited timestamptz
    ) returns boolean
      language sql immutable parallel safe as $$
      select web_edited is not null
         and (desktop_edited is null or web_edited >= desktop_edited)
    $$;
  `);

  // `person_name_variants` has no `updated_at` and no set_updated_at
  // trigger; it does not need one (the LWW reads the two columns above),
  // but the push needs a stable ordering column for the edit audit, and
  // /sync/pull/web_edits needs something to page on. Give it the same
  // shape as its siblings so no caller has to special-case it.
  pgm.addColumns('person_name_variants', {
    updated_at: { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
  });
  pgm.sql(`
    create trigger person_name_variants_set_updated_at before update on person_name_variants
      for each row execute function set_updated_at();
  `);

  // ---------------------------------------------------------------- 2
  for (const t of JOIN_TABLES) {
    pgm.addColumns(t, {
      is_deleted: { type: 'boolean', notNull: true, default: false },
      deleted_at: { type: 'timestamptz' },
      deleted_by: { type: 'bigint', references: 'users', onDelete: 'SET NULL' },
      updated_at: { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
    });
    pgm.sql(`
      create trigger ${t}_set_updated_at before update on ${t}
        for each row execute function set_updated_at();
    `);
  }
  pgm.sql(`
    create index album_photos_live_idx on album_photos (album_id, photo_id)
      where not is_deleted
  `);
  pgm.sql(`
    create index photo_places_live_idx on photo_places (photo_id, place_id)
      where not is_deleted
  `);

  // The search vector must stop counting a removed album / place. Only the
  // two lateral joins change (`and ap.is_deleted = false`,
  // `and php.is_deleted = false`); the rest is Phase 11's function verbatim.
  pgm.sql(searchFn({ softDeleteJoins: true }));

  // ---------------------------------------------------------------- 3
  pgm.addColumns('comments', {
    edited_at: { type: 'timestamptz' },
  });
  pgm.sql(`
    comment on column comments.edited_at is
      'Null until the first edit. Non-null renders "(edited)" next to the timestamp.';
  `);
};

export const down = (pgm) => {
  pgm.dropColumns('comments', ['edited_at']);

  pgm.sql(searchFn({ softDeleteJoins: false }));
  pgm.sql(`drop index if exists photo_places_live_idx`);
  pgm.sql(`drop index if exists album_photos_live_idx`);
  for (const t of JOIN_TABLES) {
    pgm.sql(`drop trigger if exists ${t}_set_updated_at on ${t}`);
    // A soft-deleted join row has no representation without the column, and
    // dropping it would make the row live again — which is a silent
    // *re-add* of a photo to an album somebody removed it from. Delete
    // those rows instead: the removal is what the operator asked for, and
    // it is in the audit log either way.
    pgm.sql(`delete from ${t} where is_deleted`);
    pgm.dropColumns(t, ['is_deleted', 'deleted_at', 'deleted_by', 'updated_at']);
  }

  pgm.sql(`drop trigger if exists person_name_variants_set_updated_at on person_name_variants`);
  pgm.dropColumns('person_name_variants', ['updated_at']);
  pgm.sql(`drop function if exists sync_web_edit_wins(timestamptz, timestamptz)`);
  for (const t of EDITABLE) {
    pgm.dropColumns(t, ['edited_on_desktop_at', 'edited_on_web_at']);
  }
};

// Phase 11's refresh_photos_search_now, with the album / place lateral
// joins optionally honouring the new soft-delete flags. Kept as one
// function so `up` and `down` cannot drift.
function searchFn({ softDeleteJoins }) {
  const apLive = softDeleteJoins ? ' and ap.is_deleted = false' : '';
  const ppLive = softDeleteJoins ? ' and php.is_deleted = false' : '';
  return `
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
           where ap.photo_id = p.id${apLive}
        ) al on true
        left join lateral (
          select string_agg(distinct pc.name, ' ') as txt
            from photo_places php
            join places pc on pc.id = php.place_id and pc.is_deleted = false
           where php.photo_id = p.id${ppLive}
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
  `;
}
