// Ingest staging: proposed front/back pairs and rescan candidates. Held here
// between the scan pass and George's review; nothing lands in photo_backs or
// creates a new preferred photo_masters until accepted.

export const shorthands = undefined;

export const up = (pgm) => {
  pgm.createType('ingest_proposal_status', ['pending', 'accepted', 'rejected']);

  pgm.createTable('ingest_pairings', {
    id:               { type: 'bigserial', primaryKey: true },
    front_photo_id:   { type: 'bigint', notNull: true, references: 'photos', onDelete: 'RESTRICT' },
    back_master_path: { type: 'text', notNull: true, unique: true },
    back_sha256:      { type: 'text', notNull: true, unique: true },
    back_source_folder:   { type: 'text', notNull: true },
    back_source_filename: { type: 'text', notNull: true },
    back_scan_sequence:   { type: 'int' },
    back_score:       { type: 'real', notNull: true },
    staging_working_path: { type: 'text', notNull: true },
    staging_thumb_path:   { type: 'text' },
    status:           { type: 'ingest_proposal_status', notNull: true, default: 'pending' },
    decided_at:       { type: 'timestamptz' },
    created_at:       { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
    updated_at:       { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
  });
  pgm.createIndex('ingest_pairings', 'status');
  pgm.createIndex('ingest_pairings', 'front_photo_id');
  pgm.sql(`
    create trigger ingest_pairings_set_updated_at before update on ingest_pairings
      for each row execute function set_updated_at();
  `);

  pgm.createTable('ingest_rescans', {
    id:                { type: 'bigserial', primaryKey: true },
    existing_photo_id: { type: 'bigint', notNull: true, references: 'photos', onDelete: 'RESTRICT' },
    new_master_path:   { type: 'text', notNull: true, unique: true },
    new_sha256:        { type: 'text', notNull: true, unique: true },
    new_source_root:      { type: 'text', notNull: true },
    new_source_folder:    { type: 'text', notNull: true },
    new_source_filename:  { type: 'text', notNull: true },
    new_scan_batch:       { type: 'text' },
    new_scan_sequence:    { type: 'int' },
    distance:          { type: 'int', notNull: true },
    new_width:         { type: 'int' },
    new_height:        { type: 'int' },
    new_file_size:     { type: 'bigint' },
    new_mime:          { type: 'text' },
    staging_working_path: { type: 'text', notNull: true },
    staging_thumb_path:   { type: 'text' },
    status:            { type: 'ingest_proposal_status', notNull: true, default: 'pending' },
    decided_at:        { type: 'timestamptz' },
    created_at:        { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
    updated_at:        { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
  });
  pgm.createIndex('ingest_rescans', 'status');
  pgm.createIndex('ingest_rescans', 'existing_photo_id');
  pgm.sql(`
    create trigger ingest_rescans_set_updated_at before update on ingest_rescans
      for each row execute function set_updated_at();
  `);

  // Files that fail ingest (corrupt, undecodable) never get a photos row and
  // therefore cannot be recorded in job_items (photo_id is NOT NULL there).
  // Keep them here so ingest reports them and George can find them later.
  pgm.createTable('ingest_failures', {
    id:              { type: 'bigserial', primaryKey: true },
    job_run_id:      { type: 'bigint', notNull: true, references: 'job_runs', onDelete: 'RESTRICT' },
    source_root:     { type: 'text', notNull: true },
    source_folder:   { type: 'text', notNull: true },
    source_filename: { type: 'text', notNull: true },
    master_path:     { type: 'text', notNull: true },
    error:           { type: 'text', notNull: true },
    created_at:      { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
  });
  pgm.createIndex('ingest_failures', 'job_run_id');
  pgm.createIndex('ingest_failures', ['source_root', 'source_folder']);
};

export const down = (pgm) => {
  pgm.dropTable('ingest_failures');
  pgm.dropTable('ingest_rescans');
  pgm.dropTable('ingest_pairings');
  pgm.dropType('ingest_proposal_status');
};
