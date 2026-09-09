// Phase 6 fix-up 8 — face review status.
//
// `review_status` is about *unassigned* faces: George presses U (unknown)
// or I (ignore) to dismiss a cluster he cannot name. Assigned faces keep
// `pending` — their identity comes from `person_id`.
//
//   pending — the default; face is either labelled (person_id set) or
//             still awaiting review.
//   unknown — face is a person, but not one George can name yet. Still
//             clustered so a future label can catch it via "likely X".
//             Kept out of the main labelling queue; surfaced later.
//   ignore  — noise (fake face, reflection, painting on a wall). Excluded
//             from clustering AND from any reference set.

export const shorthands = undefined;

export const up = (pgm) => {
  pgm.addColumns('faces', {
    review_status: { type: 'text', notNull: true, default: 'pending' },
    review_note:   { type: 'text' },
    reviewed_at:   { type: 'timestamptz' },
  });
  pgm.sql(`
    alter table faces add constraint faces_review_status_check
      check (review_status in ('pending', 'unknown', 'ignore'))
  `);
  pgm.createIndex('faces', 'review_status');
};

export const down = (pgm) => {
  pgm.dropIndex('faces', 'review_status');
  pgm.sql(`alter table faces drop constraint faces_review_status_check`);
  pgm.dropColumns('faces', ['review_status', 'review_note', 'reviewed_at']);
};
