// Fix-up 2: aspect ratio is evidence, not a veto. When the score is >= 0.8
// we now propose the pair even if aspects differ, and the grid tags the
// proposal so George knows what he's looking at.

export const shorthands = undefined;

export const up = (pgm) => {
  pgm.addColumn('ingest_pairings', {
    back_aspect_mismatch: {
      type: 'boolean',
      notNull: true,
      default: false,
    },
  });
};

export const down = (pgm) => {
  pgm.dropColumn('ingest_pairings', 'back_aspect_mismatch');
};
