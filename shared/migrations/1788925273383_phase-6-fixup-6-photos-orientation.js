// Phase 6 fix-up 6 — EXIF orientation on photos.
//
// The face detector is fed the EXIF-transposed (display) image, so face
// bboxes MUST be interpreted in display coordinates. Store the EXIF
// orientation code so downstream code (crops, preview, per-photo view,
// metadata writer) never has to re-read the master's EXIF to know
// whether to transpose. `photos.width/height` are the DISPLAY dimensions
// as of this fix-up; the `photo_masters.width/height` on each master
// row remains the raw file dimension.

export const shorthands = undefined;

export const up = (pgm) => {
  pgm.addColumn('photos', {
    orientation: { type: 'int' },
  });
  // EXIF value 1..8. Nullable while backfill runs — the repair tool
  // fills it from each master's EXIF. Once backfilled, nothing writes
  // NULL anymore.
  pgm.sql(`
    alter table photos add constraint photos_orientation_check
      check (orientation is null or orientation between 1 and 8)
  `);
};

export const down = (pgm) => {
  pgm.sql(`alter table photos drop constraint photos_orientation_check`);
  pgm.dropColumn('photos', 'orientation');
};
