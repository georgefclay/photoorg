// Phase 3 fix-up 2: JSONB `details` column on ingest_pairings so that
// the B-key ("this is a back") flow can record `details.source = 'triage'`
// (and future proposal sources can add whatever provenance they need).

export const shorthands = undefined;

export const up = (pgm) => {
  pgm.addColumn('ingest_pairings', {
    details: { type: 'jsonb', notNull: true, default: '{}' },
  });
};

export const down = (pgm) => {
  pgm.dropColumn('ingest_pairings', 'details');
};
