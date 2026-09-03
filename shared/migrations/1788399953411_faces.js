export const shorthands = undefined;

export const up = (pgm) => {
  pgm.createTable('faces', {
    id:              { type: 'bigserial', primaryKey: true },
    photo_id:        { type: 'bigint', notNull: true, references: 'photos', onDelete: 'CASCADE' },
    person_id:       { type: 'bigint', references: 'people', onDelete: 'SET NULL' },
    bbox:            { type: 'jsonb', notNull: true },
    embedding:       { type: 'real[]' },
    embedding_model: { type: 'text' },
    confidence:      { type: 'real' },
    source:          { type: 'face_source', notNull: true },
    is_disputed:     { type: 'boolean', notNull: true, default: false },
    disputed_by:     { type: 'bigint' },
    dispute_note:    { type: 'text' },
    created_by:      { type: 'bigint' },
    is_deleted:      { type: 'boolean', notNull: true, default: false },
    created_at:      { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
    updated_at:      { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
  });
  pgm.createIndex('faces', 'photo_id');
  pgm.createIndex('faces', 'person_id');
  pgm.sql(`create index faces_disputed_idx on faces (id) where is_disputed`);
  pgm.sql(`
    create trigger faces_set_updated_at before update on faces
      for each row execute function set_updated_at();
  `);
};

export const down = (pgm) => {
  pgm.dropTable('faces');
};
