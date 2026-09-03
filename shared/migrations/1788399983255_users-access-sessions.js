export const shorthands = undefined;

export const up = (pgm) => {
  pgm.createTable('users', {
    id:            { type: 'bigserial', primaryKey: true },
    email:         { type: 'text', notNull: true, unique: true },
    display_name:  { type: 'text' },
    role:          { type: 'user_role', notNull: true, default: 'contributor' },
    status:        { type: 'user_status', notNull: true, default: 'active' },
    is_service:    { type: 'boolean', notNull: true, default: false },
    last_login_at: { type: 'timestamptz' },
    created_at:    { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
    updated_at:    { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
  });
  pgm.addConstraint('users', 'users_email_lowercase', {
    check: 'email = lower(email)',
  });
  pgm.sql(`
    create trigger users_set_updated_at before update on users
      for each row execute function set_updated_at();
  `);

  pgm.createTable('access_requests', {
    id:               { type: 'bigserial', primaryKey: true },
    email:            { type: 'text', notNull: true },
    message:          { type: 'text' },
    status:           { type: 'access_status', notNull: true, default: 'pending' },
    token:            { type: 'text', unique: true },
    token_expires_at: { type: 'timestamptz' },
    decided_by:       { type: 'bigint', references: 'users', onDelete: 'SET NULL' },
    decided_at:       { type: 'timestamptz' },
    created_at:       { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
    updated_at:       { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
  });
  pgm.createIndex('access_requests', 'email');
  pgm.createIndex('access_requests', 'status');
  pgm.sql(`
    create trigger access_requests_set_updated_at before update on access_requests
      for each row execute function set_updated_at();
  `);

  // Expiries are always computed in SQL, never in Node: use
  //   insert into magic_links (..., expires_at) values (..., now() + interval '15 minutes')
  pgm.createTable('magic_links', {
    id:         { type: 'bigserial', primaryKey: true },
    user_id:    { type: 'bigint', notNull: true, references: 'users', onDelete: 'RESTRICT' },
    token_hash: { type: 'text', notNull: true, unique: true },
    expires_at: { type: 'timestamptz', notNull: true },
    used_at:    { type: 'timestamptz' },
    created_at: { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
  });
  pgm.createIndex('magic_links', 'user_id');
  pgm.createIndex('magic_links', 'expires_at');

  // connect-pg-simple's exact schema. Deviates from the house style
  // (varchar not text, timestamp not timestamptz) so the middleware works
  // without a custom schema override. Do not "normalise" it.
  pgm.sql(`
    create table "session" (
      "sid"    varchar not null collate "default",
      "sess"   json not null,
      "expire" timestamp(6) not null
    ) with (oids = false);
  `);
  pgm.sql(`
    alter table "session"
      add constraint "session_pkey" primary key ("sid") not deferrable initially immediate;
  `);
  pgm.sql(`create index "IDX_session_expire" on "session" ("expire")`);
};

export const down = (pgm) => {
  pgm.sql(`drop table if exists "session"`);
  pgm.dropTable('magic_links');
  pgm.dropTable('access_requests');
  pgm.dropTable('users');
};
