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
  // Orphan backs are legitimate data — a scanned back whose front we
  // haven't identified yet, still holding OCR text that may re-identify
  // the front later. Refuse to reverse this migration if any exist; a
  // silent DELETE would lose real photos of writing on backs.
  pgm.sql(`
    do $$
    declare n bigint;
    begin
      select count(*) into n from photo_backs where photo_id is null;
      if n > 0 then
        raise exception
          'photo_backs has % orphan row(s) (photo_id is null). '
          'Refusing to make photo_id NOT NULL: these are legitimate '
          'scanned backs whose fronts have not been identified. '
          'Reunite them with fronts (or explicitly move them elsewhere) '
          'before rolling this migration back.', n;
      end if;
    end
    $$;
  `);
  pgm.dropIndex('photo_backs', 'photo_id', { name: 'photo_backs_orphan_idx' });
  pgm.alterColumn('photo_backs', 'photo_id', {
    notNull: true,
  });
};
