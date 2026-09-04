// Phase 3 (Triage): pre-sort hints stored per photo. Set by the
// triage_presort job, read by the Triage grid to pick a default key.
// Hints are *hints* — they never make a decision on their own.
//
// One row per photo (PK photo_id). `hint` is the winning label after the
// precedence cascade; losers land in `details.also`.

export const shorthands = undefined;

export const up = (pgm) => {
  pgm.createTable('triage_hints', {
    photo_id:    { type: 'bigint', primaryKey: true,
                   references: 'photos', onDelete: 'RESTRICT' },
    hint:        { type: 'text', notNull: true,
                   check: "hint in ('photo','screenshot','document'," +
                          "'blank_or_dark','tiny','burst','exact_dup_of')" },
    confidence:  { type: 'real', notNull: true, default: 0 },
    details:     { type: 'jsonb', notNull: true, default: '{}' },
    computed_at: { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
  });
  pgm.createIndex('triage_hints', 'hint');
};

export const down = (pgm) => {
  pgm.dropTable('triage_hints');
};
