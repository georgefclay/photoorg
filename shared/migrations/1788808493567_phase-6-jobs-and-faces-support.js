// Phase 6 — inference client / jobs / faces support.
//
//   job_cursors                — per-job NDJSON cursor (line_no in the mini's
//                                per-job results file). The batch runner reads
//                                new lines after this cursor, applies the
//                                writer idempotently, then advances it.
//   photo_job_status.prompt_version
//                              — remember the prompt template as well as the
//                                model, so a prompt bump can re-run a job
//                                without a model change.
//   faces.deleted_at / delete_reason
//                              — the "not a face" soft-delete audit fields.
//                                is_deleted already exists (migration 4).
//   triage_hints.hint          — accept 'ai_junk' from the classify writer.
//                                Precedence is presort-wins; ai_junk only
//                                lands as the winning hint on rows with no
//                                presort hint. When presort has already
//                                written a hint, the AI label goes into
//                                details.also (application-side).

export const shorthands = undefined;

export const up = (pgm) => {
  pgm.createTable('job_cursors', {
    job_name:   { type: 'text', primaryKey: true },
    line_no:    { type: 'bigint', notNull: true, default: 0 },
    updated_at: { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
  });
  pgm.sql(`
    create trigger job_cursors_set_updated_at before update on job_cursors
      for each row execute function set_updated_at();
  `);

  pgm.addColumn('photo_job_status', {
    prompt_version: { type: 'text' },
  });

  pgm.addColumns('faces', {
    deleted_at:    { type: 'timestamptz' },
    delete_reason: { type: 'text' },
  });

  pgm.sql(`
    alter table triage_hints drop constraint triage_hints_hint_check;
    alter table triage_hints add constraint triage_hints_hint_check
      check (hint in (
        'photo','screenshot','document','blank_or_dark','tiny','burst',
        'exact_dup_of','possible_back','ai_junk'
      ));
  `);
};

export const down = (pgm) => {
  pgm.sql(`
    -- Callers must have remapped any 'ai_junk' rows before dropping the value.
    alter table triage_hints drop constraint triage_hints_hint_check;
    alter table triage_hints add constraint triage_hints_hint_check
      check (hint in (
        'photo','screenshot','document','blank_or_dark','tiny','burst',
        'exact_dup_of','possible_back'
      ));
  `);

  pgm.dropColumns('faces', ['deleted_at', 'delete_reason']);
  pgm.dropColumn('photo_job_status', 'prompt_version');
  pgm.dropTable('job_cursors');
};
