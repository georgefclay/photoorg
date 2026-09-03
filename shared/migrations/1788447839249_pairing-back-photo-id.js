// Fix-up 1: allow ingest_pairings to reference an already-committed photo as
// the proposed back. Populated by the Rebuild-back-proposals action for
// photos that qualify under the corrected scorer but were missed by the
// initial ingest. Nullable because held (not-yet-committed) backs still use
// back_master_path only.

export const shorthands = undefined;

export const up = (pgm) => {
  pgm.addColumn('ingest_pairings', {
    back_photo_id: {
      type: 'bigint',
      references: 'photos',
      onDelete: 'RESTRICT',
    },
  });
  pgm.createIndex('ingest_pairings', 'back_photo_id');
  // A given committed photo should have at most one active pending proposal
  // as a back (otherwise the rebuild could double-propose).
  pgm.sql(`
    create unique index ingest_pairings_back_photo_pending
      on ingest_pairings (back_photo_id)
      where back_photo_id is not null and status = 'pending'
  `);
};

export const down = (pgm) => {
  pgm.sql('drop index if exists ingest_pairings_back_photo_pending');
  pgm.dropIndex('ingest_pairings', 'back_photo_id');
  pgm.dropColumn('ingest_pairings', 'back_photo_id');
};
