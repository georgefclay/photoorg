// Phase 7 — Scan cleanup.
//
//   cleanup_proposals    — one row per analysed scan photo; a *proposal*
//                          until George accepts it. `operations` holds what
//                          the analyser measured, `transform` the affine it
//                          would apply, `split_regions` the per-print
//                          regions of a multi-print scan.
//   cleanup_spend        — cumulative remote-enhance spend, so the session
//                          counter survives a restart.
//   photo_masters.region — a split child points at the SAME master file as
//                          its siblings, with the region it came from.
//                          The global uniques on `master_path` and `sha256`
//                          therefore become per-region.
//   photos.parent_photo_id — the scan a split child came from.
//
// Identity note (SCHEMA.md): a split child's `photos.sha256` is
// sha256(master_sha256 + ':' + region_key) — an identity key, not a file
// hash. The parent keeps the real master filename so re-ingesting the
// master stays a no-op.

export const shorthands = undefined;

export const up = (pgm) => {
  pgm.createType('cleanup_status', [
    'pending', 'accepted', 'rejected', 'manual', 'clean', 'superseded',
  ]);

  pgm.createTable('cleanup_proposals', {
    id:            { type: 'bigserial', primaryKey: true },
    photo_id:      { type: 'bigint', notNull: true, references: 'photos', onDelete: 'RESTRICT' },
    status:        { type: 'cleanup_status', notNull: true, default: 'pending' },
    // What the analyser measured + the per-op parameters it chose.
    operations:    { type: 'jsonb', notNull: true, default: '{}' },
    // The affine the full ticked set would apply (see modes/cleanup/geometry.py).
    transform:     { type: 'jsonb' },
    // The ~2000 px preview JPEG (full resolution is rendered on demand).
    derived_path:  { type: 'text' },
    needs_manual:  { type: 'boolean', notNull: true, default: false },
    // Why the analyser refused to decide: print_too_small, implausible_aspect,
    // skew_too_large, has_back, no_print_found.
    manual_reason: { type: 'text' },
    // One entry per print on a multi-print scan; null when not a split.
    split_regions: { type: 'jsonb' },
    analysed_at:   { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
    analysis_ms:   { type: 'int' },
    decided_at:    { type: 'timestamptz' },
    decided_by:    { type: 'text' },
    created_at:    { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
    updated_at:    { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
  });
  pgm.createIndex('cleanup_proposals', 'photo_id');
  pgm.createIndex('cleanup_proposals', 'status');
  // At most one live (pending) proposal per photo. Decided rows accumulate.
  pgm.sql(`
    create unique index cleanup_proposals_one_pending
      on cleanup_proposals (photo_id) where status = 'pending';
  `);
  pgm.sql(`
    create trigger cleanup_proposals_set_updated_at before update on cleanup_proposals
      for each row execute function set_updated_at();
  `);

  pgm.createTable('cleanup_spend', {
    id:                { type: 'bigserial', primaryKey: true },
    provider:          { type: 'text', notNull: true },
    photo_id:          { type: 'bigint', references: 'photos', onDelete: 'SET NULL' },
    proposal_id:       { type: 'bigint', references: 'cleanup_proposals', onDelete: 'SET NULL' },
    job_ref:           { type: 'text' },
    cost_estimate_usd: { type: 'numeric(10,4)' },
    actual_cost_usd:   { type: 'numeric(10,4)' },
    status:            { type: 'text', notNull: true, default: 'submitted' },
    created_at:        { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
  });
  pgm.createIndex('cleanup_spend', 'provider');
  pgm.createIndex('cleanup_spend', 'created_at');

  // --- split children: many photo_masters rows per master file ------------
  pgm.addColumns('photo_masters', {
    region:     { type: 'jsonb' },
    // '-' for a whole-file master; otherwise 'x,y,w,h' of the region in the
    // master's own pixel frame. Part of the uniqueness key below.
    region_key: { type: 'text', notNull: true, default: '-' },
  });
  pgm.dropConstraint('photo_masters', 'photo_masters_master_path_key');
  pgm.dropConstraint('photo_masters', 'photo_masters_sha256_key');
  pgm.addConstraint('photo_masters', 'photo_masters_path_region_unique',
    'unique(master_path, region_key)');
  pgm.addConstraint('photo_masters', 'photo_masters_sha_region_unique',
    'unique(sha256, region_key)');

  pgm.addColumns('photos', {
    parent_photo_id: { type: 'bigint', references: 'photos', onDelete: 'SET NULL' },
    // When the desktop last told the web that this photo is gone. Junk,
    // private and soft-deleted photos are excluded from the ordinary push
    // selector, so without this a photo that was pushed and then junked (a
    // dedupe loser, a triage decision, a split parent) or made private would
    // stay visible on the VM forever. `set_updated_at` fires on every update,
    // so "synced_at > updated_at" cannot serve as the marker — this can.
    tombstoned_at: { type: 'timestamptz' },
  });
  pgm.createIndex('photos', 'parent_photo_id');
  pgm.sql(`
    create index photos_tombstone_pending_idx on photos (id)
      where synced_at is not null
        and (triage_status = 'junk' or is_deleted or is_private)
        and tombstoned_at is null
  `);
};

export const down = (pgm) => {
  pgm.sql('drop index if exists photos_tombstone_pending_idx');
  pgm.dropIndex('photos', 'parent_photo_id');
  pgm.dropColumns('photos', ['parent_photo_id', 'tombstoned_at']);

  // Refuse to go back while split children exist — collapsing the
  // per-region uniques would silently drop their photo_masters rows.
  pgm.sql(`
    do $$
    declare n int;
    begin
      select count(*) into n from photo_masters where region_key <> '-';
      if n > 0 then
        raise exception
          'phase-7-cleanup down: % photo_masters rows carry a region (split children). Resolve them before rolling back.', n;
      end if;
    end $$;
  `);
  pgm.dropConstraint('photo_masters', 'photo_masters_path_region_unique');
  pgm.dropConstraint('photo_masters', 'photo_masters_sha_region_unique');
  pgm.addConstraint('photo_masters', 'photo_masters_master_path_key', 'unique(master_path)');
  pgm.addConstraint('photo_masters', 'photo_masters_sha256_key', 'unique(sha256)');
  pgm.dropColumns('photo_masters', ['region', 'region_key']);

  pgm.dropTable('cleanup_spend');
  pgm.dropTable('cleanup_proposals');
  pgm.dropType('cleanup_status');
};
