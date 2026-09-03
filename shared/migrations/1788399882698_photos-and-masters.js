export const shorthands = undefined;

export const up = (pgm) => {
  pgm.createTable('photos', {
    id:                { type: 'bigserial', primaryKey: true },
    working_path:      { type: 'text' },
    sha256:            { type: 'text', notNull: true, unique: true },
    phash:             { type: 'text' },
    dhash:             { type: 'text' },
    width:             { type: 'int' },
    height:            { type: 'int' },
    mime:              { type: 'text', notNull: true },
    file_size:         { type: 'bigint' },

    is_scan:           { type: 'boolean', notNull: true, default: false },
    has_no_people:     { type: 'boolean', notNull: true, default: false },

    capture_date:            { type: 'date' },
    capture_date_precision:  { type: 'date_precision', notNull: true, default: 'unknown' },
    capture_date_confirmed:  { type: 'boolean', notNull: true, default: false },

    exif_taken_at:     { type: 'timestamptz' },
    exif_camera:       { type: 'text' },
    exif_gps_lat:      { type: 'double precision' },
    exif_gps_lon:      { type: 'double precision' },

    source_root:       { type: 'text', notNull: true },
    source_folder:     { type: 'text', notNull: true },
    source_filename:   { type: 'text', notNull: true },
    scan_batch:        { type: 'text' },
    scan_sequence:     { type: 'int' },
    physical_ref_note: { type: 'text' },
    rescan_wanted:     { type: 'boolean', notNull: true, default: false },

    triage_status:     { type: 'triage_status', notNull: true, default: 'untriaged' },
    is_private:        { type: 'boolean', notNull: true, default: false },
    is_deleted:        { type: 'boolean', notNull: true, default: false },
    deleted_at:        { type: 'timestamptz' },
    quarantine_path:   { type: 'text' },

    file_version:        { type: 'int', notNull: true, default: 1 },
    synced_at:           { type: 'timestamptz' },
    synced_file_version: { type: 'int' },

    description_ai:      { type: 'text' },
    completeness_score:  { type: 'int', notNull: true, default: 0 },

    created_at: { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
    updated_at: { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
  });

  pgm.addConstraint('photos', 'photos_source_unique', {
    unique: ['source_root', 'source_folder', 'source_filename'],
  });

  pgm.createIndex('photos', 'phash');
  pgm.createIndex('photos', 'dhash');
  pgm.createIndex('photos', ['scan_batch', 'scan_sequence']);
  pgm.createIndex('photos', 'capture_date');
  pgm.createIndex('photos', 'triage_status');
  pgm.createIndex('photos', 'is_private');
  pgm.createIndex('photos', 'is_deleted');
  pgm.sql(`create index photos_rescan_wanted_idx on photos (id) where rescan_wanted`);

  pgm.sql(`
    create trigger photos_set_updated_at before update on photos
      for each row execute function set_updated_at();
  `);

  pgm.createTable('photo_masters', {
    id:           { type: 'bigserial', primaryKey: true },
    photo_id:     { type: 'bigint', notNull: true, references: 'photos', onDelete: 'CASCADE' },
    master_path:  { type: 'text', notNull: true, unique: true },
    sha256:       { type: 'text', notNull: true, unique: true },
    width:        { type: 'int' },
    height:       { type: 'int' },
    dpi:          { type: 'int' },
    mime:         { type: 'text', notNull: true },
    file_size:    { type: 'bigint' },
    is_preferred: { type: 'boolean', notNull: true, default: false },
    ingested_at:  { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
    created_at:   { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
    updated_at:   { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
  });
  pgm.createIndex('photo_masters', 'photo_id');
  pgm.sql(`
    create unique index photo_masters_one_preferred on photo_masters (photo_id)
      where is_preferred
  `);
  pgm.sql(`
    create trigger photo_masters_set_updated_at before update on photo_masters
      for each row execute function set_updated_at();
  `);

  pgm.createTable('photo_backs', {
    id:                        { type: 'bigserial', primaryKey: true },
    photo_id:                  { type: 'bigint', notNull: true, references: 'photos', onDelete: 'CASCADE' },
    master_path:               { type: 'text', notNull: true, unique: true },
    sha256:                    { type: 'text', notNull: true, unique: true },
    working_path:              { type: 'text' },
    source_folder:             { type: 'text' },
    source_filename:           { type: 'text' },
    scan_sequence:             { type: 'int' },
    transcribed_text:          { type: 'text' },
    transcription_confidence:  { type: 'real' },
    transcription_confirmed:   { type: 'boolean', notNull: true, default: false },
    created_at:                { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
    updated_at:                { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
  });
  pgm.createIndex('photo_backs', 'photo_id');
  pgm.sql(`
    create trigger photo_backs_set_updated_at before update on photo_backs
      for each row execute function set_updated_at();
  `);
};

export const down = (pgm) => {
  pgm.dropTable('photo_backs');
  pgm.dropTable('photo_masters');
  pgm.dropTable('photos');
};
