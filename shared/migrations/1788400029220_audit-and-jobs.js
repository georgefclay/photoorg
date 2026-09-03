export const shorthands = undefined;

export const up = (pgm) => {
  pgm.createTable('audit_log', {
    id:             { type: 'bigserial', primaryKey: true },
    user_id:        { type: 'bigint', references: 'users', onDelete: 'SET NULL' },
    actor:          { type: 'text', notNull: true },
    action:         { type: 'text', notNull: true },
    entity_type:    { type: 'text', notNull: true },
    entity_id:      { type: 'bigint' },
    previous_value: { type: 'jsonb' },
    new_value:      { type: 'jsonb' },
    created_at:     { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
  });
  pgm.createIndex('audit_log', ['entity_type', 'entity_id']);
  pgm.createIndex('audit_log', 'created_at');
  pgm.createIndex('audit_log', 'user_id');

  pgm.createTable('job_runs', {
    id:          { type: 'bigserial', primaryKey: true },
    job_name:    { type: 'text', notNull: true },
    started_at:  { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
    finished_at: { type: 'timestamptz' },
    status:      { type: 'text', notNull: true, default: 'running' },
    params:      { type: 'jsonb' },
  });
  pgm.createIndex('job_runs', 'job_name');
  pgm.createIndex('job_runs', 'status');

  pgm.createTable('job_items', {
    job_run_id: { type: 'bigint', notNull: true, references: 'job_runs', onDelete: 'CASCADE' },
    photo_id:   { type: 'bigint', notNull: true, references: 'photos', onDelete: 'CASCADE' },
    status:     { type: 'text', notNull: true },
    error:      { type: 'text' },
    updated_at: { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
  });
  pgm.addConstraint('job_items', 'job_items_pk', {
    primaryKey: ['job_run_id', 'photo_id'],
  });
  pgm.createIndex('job_items', 'photo_id');

  // Per-photo, per-job status so "re-run describe for everything after a model
  // upgrade" is a single update to model/status.
  pgm.createTable('photo_job_status', {
    photo_id:     { type: 'bigint', notNull: true, references: 'photos', onDelete: 'CASCADE' },
    job_name:     { type: 'text', notNull: true },
    model:        { type: 'text' },
    status:       { type: 'text', notNull: true },
    completed_at: { type: 'timestamptz' },
  });
  pgm.addConstraint('photo_job_status', 'photo_job_status_pk', {
    primaryKey: ['photo_id', 'job_name'],
  });
  pgm.createIndex('photo_job_status', ['job_name', 'status']);
};

export const down = (pgm) => {
  pgm.dropTable('photo_job_status');
  pgm.dropTable('job_items');
  pgm.dropTable('job_runs');
  pgm.dropTable('audit_log');
};
