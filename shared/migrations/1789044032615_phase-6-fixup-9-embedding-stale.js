// Phase 6 fix-up 9 — `faces.embedding_stale` flag.
//
// Manual bbox edits (fix-up 9 item 2) don't always have the LAN inference
// service available to recompute the embedding from the new crop. When
// the service is down, the writer updates `faces.bbox` + `source='human'`
// and sets `embedding_stale = true`. The next `detect_faces` pass picks
// those rows up (in addition to its usual selector) and refreshes the
// embedding from the current bbox.

export const shorthands = undefined;

export const up = (pgm) => {
  pgm.addColumn('faces', {
    embedding_stale: { type: 'boolean', notNull: true, default: false },
  });
  pgm.sql(`
    create index faces_embedding_stale_idx on faces (id) where embedding_stale
  `);
};

export const down = (pgm) => {
  pgm.sql(`drop index if exists faces_embedding_stale_idx`);
  pgm.dropColumn('faces', 'embedding_stale');
};
