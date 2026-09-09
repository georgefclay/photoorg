// Phase 6 fix-up 4 — name suffix (Jr./II/III/…) on people.
//
// Nullable text column. Included in the trigger-maintained display_name
// as `given "nickname" surname suffix (née maiden)` so the existing
// `Margaret "Peggy" Clay (née Schmidt)` render extends to
// `George Clay Jr.` and `John "Jack" Smith III (née …)` when set.

export const shorthands = undefined;

export const up = (pgm) => {
  pgm.addColumn('people', {
    suffix: { type: 'text' },
  });

  pgm.sql(`
    create or replace function people_display_name(p people) returns text
      language sql immutable as $$
      select trim(both ' ' from
        coalesce(nullif(p.given_name, ''), '')
        || case when p.nickname is not null and p.nickname <> ''
                then ' "' || p.nickname || '"' else '' end
        || case when p.surname is not null and p.surname <> ''
                then ' ' || p.surname else '' end
        || case when p.suffix is not null and p.suffix <> ''
                then ' ' || p.suffix else '' end
        || case when p.maiden_name is not null and p.maiden_name <> ''
                     and (p.surname is null or p.maiden_name <> p.surname)
                then ' (née ' || p.maiden_name || ')' else '' end
      )
    $$;
  `);

  // Recompute display_name for existing rows so the new column is
  // reflected everywhere. The trigger fires on update; the no-op
  // touch is enough.
  pgm.sql(`update people set updated_at = updated_at`);
};

export const down = (pgm) => {
  // Restore the pre-fix-up-4 definition (no suffix), then drop the column.
  pgm.sql(`
    create or replace function people_display_name(p people) returns text
      language sql immutable as $$
      select trim(both ' ' from
        coalesce(nullif(p.given_name, ''), '')
        || case when p.nickname is not null and p.nickname <> ''
                then ' "' || p.nickname || '"' else '' end
        || case when p.surname is not null and p.surname <> ''
                then ' ' || p.surname else '' end
        || case when p.maiden_name is not null and p.maiden_name <> ''
                     and (p.surname is null or p.maiden_name <> p.surname)
                then ' (née ' || p.maiden_name || ')' else '' end
      )
    $$;
  `);
  pgm.sql(`update people set updated_at = updated_at`);
  pgm.dropColumn('people', 'suffix');
};
