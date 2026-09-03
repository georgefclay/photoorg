// Fix-up 4: an ingest_pairings row can now have front_photo_id NULL —
// this happens when the immediate predecessor is itself a probable back
// (so we can't safely pair with it). George decides in the grid: N to
// accept as an orphan back, or F to pick a front from the filmstrip.

export const shorthands = undefined;

export const up = (pgm) => {
  pgm.alterColumn('ingest_pairings', 'front_photo_id', {
    notNull: false,
  });
};

export const down = (pgm) => {
  pgm.alterColumn('ingest_pairings', 'front_photo_id', {
    notNull: true,
  });
};
