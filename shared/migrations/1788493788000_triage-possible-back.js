// Phase 3 fix-up 2: add 'possible_back' to the triage_hints.hint enum.
// A scan-root photo whose blank_or_dark hint would fire but which has any
// ink at all becomes possible_back instead — likely the back of a print
// with only a date scribbled on it, which the strict back detector missed.

export const shorthands = undefined;

export const up = (pgm) => {
  pgm.sql(`
    alter table triage_hints drop constraint triage_hints_hint_check;
    alter table triage_hints add constraint triage_hints_hint_check
      check (hint in (
        'photo','screenshot','document','blank_or_dark','tiny','burst',
        'exact_dup_of','possible_back'
      ));
  `);
};

export const down = (pgm) => {
  pgm.sql(`
    -- Callers must have already remapped any 'possible_back' rows back
    -- to 'blank_or_dark' before dropping the value; otherwise this fails.
    alter table triage_hints drop constraint triage_hints_hint_check;
    alter table triage_hints add constraint triage_hints_hint_check
      check (hint in (
        'photo','screenshot','document','blank_or_dark','tiny','burst',
        'exact_dup_of'
      ));
  `);
};
