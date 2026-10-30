// Phase 12 — Download and export, and albums editable on the web.
//
// 1. `download_jobs` — a download too big to stream (> 200 photos or
//    > 1 GB) becomes a row here, built by a child process
//    (`web/tools/zip-worker.js`) into PHOTO_DIR/downloads/<id>.zip and
//    announced by email. `expires_at` is computed in SQL, never in Node.
//    The zip is a derived file and the sweeper deletes it at expiry; the
//    row stays (status `expired`) as the record of who took what.
//    One queued-or-running job per user (admins exempt) is enforced in
//    services/downloads.js, because "admin" lives on `users`, which a
//    partial index cannot see.
//
// 2. `magic_links.return_to` — the emailed "your download is ready" link
//    needs a signed-out reader to land back on /downloads/:id after
//    signing in. The path lives on the link's row and never in the
//    emailed URL.
//
// 3. `album_photos.edited_on_desktop_at` / `edited_on_web_at` — the
//    Phase 15 human-edit pair, now on the join table, because the web can
//    re-order and remove photos in desktop-born albums. `updated_at`
//    cannot settle it: every push stamps it on the VM, so a pull keyed on
//    it would echo every push back down. `position` and `is_deleted` are
//    the guarded columns (routes/sync.js WEB_EDITABLE_JOIN).
//
// `down` drops only what `up` added. It refuses while `download_jobs`
// holds rows (they are the record of who downloaded what — move them
// out first), and the album_photos columns carry no rows' meaning on
// their own, so dropping them changes no membership.

export const shorthands = undefined;

export const up = (pgm) => {
  pgm.createTable('download_jobs', {
    id:             { type: 'bigserial', primaryKey: true },
    user_id:        { type: 'bigint', notNull: true, references: 'users', onDelete: 'RESTRICT' },
    selection:      { type: 'jsonb', notNull: true },
    status:         { type: 'text', notNull: true, default: 'queued' },
    photo_count:    { type: 'integer', notNull: true, default: 0 },
    missing_count:  { type: 'integer', notNull: true, default: 0 },
    bytes_estimate: { type: 'bigint', notNull: true, default: 0 },
    zip_path:       { type: 'text' },
    zip_bytes:      { type: 'bigint' },
    error:          { type: 'text' },
    worker_pid:     { type: 'integer' },
    created_at:     { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
    started_at:     { type: 'timestamptz' },
    finished_at:    { type: 'timestamptz' },
    expires_at:     { type: 'timestamptz' },
  });
  pgm.addConstraint('download_jobs', 'download_jobs_status_check',
    "check (status in ('queued','running','done','failed','expired'))");
  pgm.createIndex('download_jobs', ['user_id', 'created_at']);
  pgm.sql(`create index download_jobs_open_idx on download_jobs (status, id)
             where status in ('queued','running')`);
  pgm.sql(`
    comment on column download_jobs.expires_at is
      'now() + interval ''72 hours'' at finish, computed in SQL. The sweeper deletes the zip and sets status=expired; the row stays.';
    comment on column download_jobs.bytes_estimate is
      'Sum of file_size over photos whose file is on this server. MISSING.txt entries add nothing.';
  `);

  pgm.addColumns('magic_links', { return_to: { type: 'text' } });
  pgm.sql(`comment on column magic_links.return_to is
    'Local path to land on after the link is used. Never part of the emailed URL.'`);

  pgm.addColumns('album_photos', {
    edited_on_desktop_at: { type: 'timestamptz' },
    edited_on_web_at:     { type: 'timestamptz' },
  });
  pgm.sql(`
    comment on column album_photos.edited_on_desktop_at is
      'Set only by a human edit in the desktop UI (add, remove, re-order). Never by a job, sync or sweep.';
    comment on column album_photos.edited_on_web_at is
      'Set only by a human edit in the web UI (add, remove, re-order). Never by a job, sync or sweep. Wins ties.';
  `);
  pgm.sql(`create index album_photos_web_edit_idx on album_photos (edited_on_web_at, album_id, photo_id)
             where edited_on_web_at is not null`);
};

export const down = (pgm) => {
  pgm.sql(`
    do $$
    declare n bigint;
    begin
      select count(*) into n from download_jobs;
      if n > 0 then
        raise exception
          'download_jobs has % row(s): the record of who downloaded what. '
          'Move them to a side table you keep '
          '(create table download_jobs_kept as select * from download_jobs) '
          'before rolling this back; audit_log keeps the download.* rows either way.', n;
      end if;
    end
    $$;
  `);
  pgm.sql(`drop index if exists album_photos_web_edit_idx`);
  pgm.dropColumns('album_photos', ['edited_on_desktop_at', 'edited_on_web_at']);
  pgm.dropColumns('magic_links', ['return_to']);
  pgm.dropTable('download_jobs');
};
