// Phase 9 — Contributions.
//
// Signed-in users upload photos. Each contribution holds one or more
// files; each file goes through sha256 + pHash dedupe on receipt, is
// visible only to the uploader and admins/moderators of the target
// groups, and stays that way until an admin (or the group's moderator)
// approves it. Approval never deletes a rejected file — rejected rows
// stay on disk under `uploads/`.
//
// Files land in the desktop archive only after the desktop's Sync mode
// Pull step copies bytes into an append-only `contrib`-kind master root
// on D:. Approval sets `status='approved'`; that's the trigger the
// desktop pull selector uses. Once the desktop confirms via
// `POST /sync/pull/contributions/:id/pulled`, `pulled_at` is set and
// the contribution stops appearing in the pull queue.

export const shorthands = undefined;

export const up = (pgm) => {
  pgm.createType('contribution_status',      ['pending', 'approved', 'rejected', 'partial']);
  pgm.createType('contribution_file_status', ['pending', 'approved', 'rejected']);

  pgm.createTable('contributions', {
    id:          { type: 'bigserial', primaryKey: true },
    user_id:     { type: 'bigint', references: 'users', onDelete: 'SET NULL' },
    note:        { type: 'text' },
    status:      { type: 'contribution_status', notNull: true, default: 'pending' },
    group_ids:   { type: 'bigint[]', notNull: true, default: '{}' },
    decided_by:  { type: 'bigint', references: 'users', onDelete: 'SET NULL' },
    decided_at:  { type: 'timestamptz' },
    pulled_at:   { type: 'timestamptz' },
    finished_at: { type: 'timestamptz' },
    created_at:  { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
    updated_at:  { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
  });
  pgm.createIndex('contributions', 'user_id');
  pgm.createIndex('contributions', 'status');
  pgm.sql(`
    create trigger contributions_set_updated_at before update on contributions
      for each row execute function set_updated_at();
  `);

  pgm.createTable('contribution_files', {
    id:                    { type: 'bigserial', primaryKey: true },
    contribution_id:       { type: 'bigint', notNull: true, references: 'contributions', onDelete: 'RESTRICT' },
    original_filename:     { type: 'text', notNull: true },
    stored_path:           { type: 'text', notNull: true, unique: true },
    sha256:                { type: 'text', notNull: true, unique: true },
    size:                  { type: 'bigint' },
    mime:                  { type: 'text', notNull: true },
    width:                 { type: 'int' },
    height:                { type: 'int' },
    exif_taken_at:         { type: 'timestamptz' },
    phash:                 { type: 'text' },
    status:                { type: 'contribution_file_status', notNull: true, default: 'pending' },
    decided_by:            { type: 'bigint', references: 'users', onDelete: 'SET NULL' },
    decided_at:            { type: 'timestamptz' },
    approved_group_ids:    { type: 'bigint[]', notNull: true, default: '{}' },
    duplicate_of_photo_id: { type: 'bigint', references: 'photos', onDelete: 'SET NULL' },
    duplicate_distance:    { type: 'int' },
    is_video:              { type: 'boolean', notNull: true, default: false },
    created_at:            { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
    updated_at:            { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
  });
  pgm.createIndex('contribution_files', 'contribution_id');
  pgm.createIndex('contribution_files', 'status');
  pgm.createIndex('contribution_files', 'phash');
  pgm.createIndex('contribution_files', 'duplicate_of_photo_id');
  pgm.sql(`
    create trigger contribution_files_set_updated_at before update on contribution_files
      for each row execute function set_updated_at();
  `);

  pgm.sql(`
    comment on table contributions is
      'Uploads from signed-in users. Files land in PHOTO_DIR/uploads/<contribution_id>/, visible only to the uploader and admins/moderators of a target group. Approval assigns the approver''s group to the file (moderators only add their own group). The desktop Sync mode Pull step copies approved bytes into D:\\Contributed under an append-only contrib master root and triggers ingest with provenance uploaded_by.'
  `);
};

export const down = (pgm) => {
  pgm.dropTable('contribution_files');
  pgm.dropTable('contributions');
  pgm.dropType('contribution_file_status');
  pgm.dropType('contribution_status');
};
