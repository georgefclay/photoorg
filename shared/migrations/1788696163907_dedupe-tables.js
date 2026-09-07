// Phase 4 — Dedupe: perceptual-hash near-duplicate groups.
//   dedupe_groups      — one row per candidate group, workflow status.
//   dedupe_members     — the photos in each group + which one is the keeper,
//                        the distances and which transform matched.
//   dedupe_exclusions  — pairs George has marked "not duplicates"; never
//                        proposed again on later re-scans.

export const shorthands = undefined;

export const up = (pgm) => {
  pgm.createType('dedupe_group_status', ['pending', 'resolved', 'not_duplicates']);

  pgm.createTable('dedupe_groups', {
    id:          { type: 'bigserial', primaryKey: true },
    status:      { type: 'dedupe_group_status', notNull: true, default: 'pending' },
    size:        { type: 'int', notNull: true },
    min_distance: { type: 'int', notNull: true },
    created_at:  { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
    resolved_at: { type: 'timestamptz' },
    resolved_by: { type: 'text' },
    updated_at:  { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
  });
  pgm.createIndex('dedupe_groups', 'status');
  pgm.sql(`
    create trigger dedupe_groups_set_updated_at before update on dedupe_groups
      for each row execute function set_updated_at();
  `);

  pgm.createTable('dedupe_members', {
    id:                  { type: 'bigserial', primaryKey: true },
    group_id:            { type: 'bigint', notNull: true, references: 'dedupe_groups', onDelete: 'CASCADE' },
    photo_id:            { type: 'bigint', notNull: true, references: 'photos', onDelete: 'RESTRICT' },
    is_keeper:           { type: 'boolean', notNull: true, default: false },
    phash_dist:          { type: 'int' },
    dhash_dist:          { type: 'int' },
    // 'phash' | 'dhash' | 'both' — which algo(s) put this pair in the group.
    matched_by:          { type: 'text', notNull: true, default: 'phash' },
    // one of identity | mirror | rot90 | rot180 | rot270 | rot90+mirror | rot270+mirror
    transform:           { type: 'text', notNull: true, default: 'identity' },
    distance_to_keeper:  { type: 'int', notNull: true, default: 0 },
    keeper_reason:       { type: 'text' },
    created_at:          { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
  });
  pgm.addConstraint('dedupe_members', 'dedupe_members_unique_photo_per_group',
    'unique(group_id, photo_id)');
  pgm.createIndex('dedupe_members', 'group_id');
  pgm.createIndex('dedupe_members', 'photo_id');
  // At-most-one-pending-group-per-photo is enforced procedurally by the
  // dedupe_scan orchestrator (which deletes all pending groups before
  // rebuilding); partial unique indexes cannot reference other tables.

  pgm.createTable('dedupe_exclusions', {
    id:         { type: 'bigserial', primaryKey: true },
    // Always store with photo_a < photo_b so a swapped pair is the same row.
    photo_a:    { type: 'bigint', notNull: true, references: 'photos', onDelete: 'RESTRICT' },
    photo_b:    { type: 'bigint', notNull: true, references: 'photos', onDelete: 'RESTRICT' },
    reason:     { type: 'text' },
    created_at: { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
  });
  pgm.addConstraint('dedupe_exclusions', 'dedupe_exclusions_ordered',
    'check (photo_a < photo_b)');
  pgm.addConstraint('dedupe_exclusions', 'dedupe_exclusions_unique_pair',
    'unique(photo_a, photo_b)');
  pgm.createIndex('dedupe_exclusions', 'photo_a');
  pgm.createIndex('dedupe_exclusions', 'photo_b');
};

export const down = (pgm) => {
  pgm.dropTable('dedupe_exclusions');
  pgm.dropTable('dedupe_members');
  pgm.dropTable('dedupe_groups');
  pgm.dropType('dedupe_group_status');
};
