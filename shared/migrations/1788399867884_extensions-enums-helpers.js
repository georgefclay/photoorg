export const shorthands = undefined;

export const up = (pgm) => {
  pgm.sql(`create extension if not exists pg_trgm`);
  pgm.sql(`create extension if not exists fuzzystrmatch`);

  pgm.createType('date_precision', ['exact', 'month', 'year', 'decade', 'unknown']);
  pgm.createType('triage_status', ['untriaged', 'keep', 'junk', 'private']);
  pgm.createType('face_source', ['ai', 'human']);
  pgm.createType('relationship_type', ['parent', 'spouse', 'sibling']);
  pgm.createType('suggestion_kind', [
    'date', 'person', 'place', 'relationship', 'description', 'transcription', 'classification',
  ]);
  pgm.createType('suggestion_status', ['pending', 'accepted', 'rejected']);
  pgm.createType('suggestion_source', ['human', 'ai', 'import']);
  pgm.createType('user_role', ['admin', 'contributor']);
  pgm.createType('user_status', ['active', 'suspended']);
  pgm.createType('access_status', ['pending', 'approved', 'denied']);
  pgm.createType('name_variant_kind', ['nickname', 'misspelling', 'alternate_spelling']);

  pgm.sql(`
    create or replace function set_updated_at() returns trigger
      language plpgsql as $$
    begin
      new.updated_at = now();
      return new;
    end
    $$;
  `);
};

export const down = (pgm) => {
  pgm.sql(`drop function if exists set_updated_at()`);
  pgm.dropType('name_variant_kind');
  pgm.dropType('access_status');
  pgm.dropType('user_status');
  pgm.dropType('user_role');
  pgm.dropType('suggestion_source');
  pgm.dropType('suggestion_status');
  pgm.dropType('suggestion_kind');
  pgm.dropType('relationship_type');
  pgm.dropType('face_source');
  pgm.dropType('triage_status');
  pgm.dropType('date_precision');
  // pg_trgm and fuzzystrmatch remain; extensions are shared and cheap.
};
