// Fix-up 3: orphan backs — a scanned back of a print whose front we can't
// find (or one we know we don't have). Accepting a proposal with the N key
// creates a photo_backs row with photo_id NULL; the writing on the back
// will still be OCR'd in Phase 6 and may re-identify the print later.

export const shorthands = undefined;

export const up = (pgm) => {
  pgm.alterColumn('photo_backs', 'photo_id', {
    notNull: false,
  });
  pgm.createIndex('photo_backs', 'photo_id', {
    name: 'photo_backs_orphan_idx',
    where: 'photo_id is null',
  });
};

export const down = (pgm) => {
  pgm.dropIndex('photo_backs', 'photo_id', { name: 'photo_backs_orphan_idx' });
  pgm.alterColumn('photo_backs', 'photo_id', {
    notNull: true,
  });
};
