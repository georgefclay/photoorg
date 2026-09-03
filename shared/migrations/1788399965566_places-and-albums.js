export const shorthands = undefined;

export const up = (pgm) => {
  pgm.createTable('places', {
    id:         { type: 'bigserial', primaryKey: true },
    name:       { type: 'text', notNull: true },
    latitude:   { type: 'double precision' },
    longitude:  { type: 'double precision' },
    notes:      { type: 'text' },
    is_deleted: { type: 'boolean', notNull: true, default: false },
    created_at: { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
    updated_at: { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
  });
  pgm.sql(`create unique index places_name_lower_uq on places (lower(name))`);
  pgm.sql(`
    create trigger places_set_updated_at before update on places
      for each row execute function set_updated_at();
  `);

  pgm.createTable('photo_places', {
    photo_id:  { type: 'bigint', notNull: true, references: 'photos', onDelete: 'RESTRICT' },
    place_id:  { type: 'bigint', notNull: true, references: 'places', onDelete: 'RESTRICT' },
    confirmed: { type: 'boolean', notNull: true, default: false },
    created_at:{ type: 'timestamptz', notNull: true, default: pgm.func('now()') },
  });
  pgm.addConstraint('photo_places', 'photo_places_pk', {
    primaryKey: ['photo_id', 'place_id'],
  });
  pgm.createIndex('photo_places', 'place_id');

  pgm.createTable('albums', {
    id:          { type: 'bigserial', primaryKey: true },
    name:        { type: 'text', notNull: true },
    description: { type: 'text' },
    created_by:  { type: 'bigint' },
    source:      { type: 'text', notNull: true, default: 'manual' },
    is_deleted:  { type: 'boolean', notNull: true, default: false },
    created_at:  { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
    updated_at:  { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
  });
  pgm.addConstraint('albums', 'albums_source_check', {
    check: "source in ('manual', 'import')",
  });
  pgm.sql(`
    create trigger albums_set_updated_at before update on albums
      for each row execute function set_updated_at();
  `);

  pgm.createTable('album_photos', {
    album_id:  { type: 'bigint', notNull: true, references: 'albums', onDelete: 'RESTRICT' },
    photo_id:  { type: 'bigint', notNull: true, references: 'photos', onDelete: 'RESTRICT' },
    position:  { type: 'int' },
    created_at:{ type: 'timestamptz', notNull: true, default: pgm.func('now()') },
  });
  pgm.addConstraint('album_photos', 'album_photos_pk', {
    primaryKey: ['album_id', 'photo_id'],
  });
  pgm.createIndex('album_photos', 'photo_id');
};

export const down = (pgm) => {
  pgm.dropTable('album_photos');
  pgm.dropTable('albums');
  pgm.dropTable('photo_places');
  pgm.dropTable('places');
};
