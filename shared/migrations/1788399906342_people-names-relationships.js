export const shorthands = undefined;

export const up = (pgm) => {
  pgm.createTable('people', {
    id:           { type: 'bigserial', primaryKey: true },
    given_name:   { type: 'text' },
    middle_name:  { type: 'text' },
    surname:      { type: 'text' },
    maiden_name:  { type: 'text' },
    nickname:     { type: 'text' },
    birth_year:   { type: 'int' },
    death_year:   { type: 'int' },
    notes:        { type: 'text' },
    display_name: { type: 'text' },
    is_deleted:   { type: 'boolean', notNull: true, default: false },
    created_at:   { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
    updated_at:   { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
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
        || case when p.maiden_name is not null and p.maiden_name <> ''
                     and (p.surname is null or p.maiden_name <> p.surname)
                then ' (née ' || p.maiden_name || ')' else '' end
      )
    $$;
  `);

  pgm.sql(`
    create or replace function people_set_display_name() returns trigger
      language plpgsql as $$
    begin
      new.display_name := people_display_name(new);
      return new;
    end
    $$;
  `);

  pgm.sql(`
    create trigger people_display_name_trg before insert or update on people
      for each row execute function people_set_display_name();
  `);
  pgm.sql(`
    create trigger people_set_updated_at before update on people
      for each row execute function set_updated_at();
  `);

  pgm.createTable('person_name_variants', {
    id:        { type: 'bigserial', primaryKey: true },
    person_id: { type: 'bigint', notNull: true, references: 'people', onDelete: 'RESTRICT' },
    variant:   { type: 'text', notNull: true },
    kind:      { type: 'name_variant_kind', notNull: true },
    created_at:{ type: 'timestamptz', notNull: true, default: pgm.func('now()') },
  });
  pgm.sql(`
    create unique index person_name_variants_uq
      on person_name_variants (person_id, lower(variant))
  `);
  pgm.sql(`
    create index person_name_variants_trgm
      on person_name_variants using gin (variant gin_trgm_ops)
  `);

  pgm.createTable('relationships', {
    id:          { type: 'bigserial', primaryKey: true },
    person_a_id: { type: 'bigint', notNull: true, references: 'people', onDelete: 'RESTRICT' },
    person_b_id: { type: 'bigint', notNull: true, references: 'people', onDelete: 'RESTRICT' },
    type:        { type: 'relationship_type', notNull: true },
    confirmed:   { type: 'boolean', notNull: true, default: false },
    created_by:  { type: 'bigint' },
    created_at:  { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
    updated_at:  { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
  });
  pgm.addConstraint('relationships', 'relationships_distinct', {
    check: 'person_a_id <> person_b_id',
  });
  pgm.addConstraint('relationships', 'relationships_unique_triple', {
    unique: ['person_a_id', 'person_b_id', 'type'],
  });
  pgm.createIndex('relationships', 'person_a_id');
  pgm.createIndex('relationships', 'person_b_id');
  pgm.sql(`
    create trigger relationships_set_updated_at before update on relationships
      for each row execute function set_updated_at();
  `);

  // Dictionary table for Phase 11 search. Populated by shared/seed/nicknames.js.
  // Not per-person data; person_name_variants stays hand-curated.
  pgm.createTable('nickname_dictionary', {
    id:         { type: 'bigserial', primaryKey: true },
    canonical:  { type: 'text', notNull: true },
    variant:    { type: 'text', notNull: true },
    created_at: { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
  });
  pgm.sql(`
    create unique index nickname_dictionary_uq
      on nickname_dictionary (lower(canonical), lower(variant))
  `);
  pgm.sql(`
    create index nickname_dictionary_variant_idx
      on nickname_dictionary (lower(variant))
  `);
  pgm.sql(`
    create index nickname_dictionary_canonical_idx
      on nickname_dictionary (lower(canonical))
  `);
};

export const down = (pgm) => {
  pgm.dropTable('nickname_dictionary');
  pgm.dropTable('relationships');
  pgm.dropTable('person_name_variants');
  pgm.sql(`drop trigger if exists people_display_name_trg on people`);
  pgm.sql(`drop function if exists people_set_display_name()`);
  pgm.sql(`drop function if exists people_display_name(people)`);
  pgm.dropTable('people');
};
