// Phase 9 — Groups as the access model.
//
// A photo is visible to a user if they share at least one group with it.
// Admins see all non-private photos including unfiled ones. is_private
// still wins over everything.
//
// Soft-delete on the join tables (`group_members`, `photo_groups`) so
// the desktop/web sync uses last-writer-wins by updated_at. A "remove"
// is a soft-delete row; never hard-delete-and-recreate. On the desktop
// side, unassign becomes `update ... set is_deleted=true, updated_at=now()`.

export const shorthands = undefined;

export const up = (pgm) => {
  pgm.createType('group_role', ['member', 'moderator']);

  pgm.createTable('groups', {
    id:          { type: 'bigserial', primaryKey: true },
    name:        { type: 'text', notNull: true },
    description: { type: 'text' },
    created_by:  { type: 'bigint', references: 'users', onDelete: 'SET NULL' },
    is_deleted:  { type: 'boolean', notNull: true, default: false },
    deleted_at:  { type: 'timestamptz' },
    deleted_by:  { type: 'bigint', references: 'users', onDelete: 'SET NULL' },
    created_at:  { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
    updated_at:  { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
  });
  pgm.sql(`
    create unique index groups_name_unique_live on groups (lower(name))
      where not is_deleted
  `);
  pgm.sql(`
    create trigger groups_set_updated_at before update on groups
      for each row execute function set_updated_at();
  `);

  pgm.createTable('group_members', {
    group_id:   { type: 'bigint', notNull: true, references: 'groups', onDelete: 'RESTRICT' },
    user_id:    { type: 'bigint', notNull: true, references: 'users',  onDelete: 'RESTRICT' },
    role:       { type: 'group_role', notNull: true, default: 'member' },
    added_by:   { type: 'bigint', references: 'users', onDelete: 'SET NULL' },
    is_deleted: { type: 'boolean', notNull: true, default: false },
    deleted_at: { type: 'timestamptz' },
    deleted_by: { type: 'bigint', references: 'users', onDelete: 'SET NULL' },
    added_at:   { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
    created_at: { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
    updated_at: { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
  });
  pgm.addConstraint('group_members', 'group_members_pk', {
    primaryKey: ['group_id', 'user_id'],
  });
  pgm.createIndex('group_members', 'user_id');
  pgm.sql(`
    create index group_members_live_idx on group_members (group_id, user_id)
      where not is_deleted
  `);
  pgm.sql(`
    create trigger group_members_set_updated_at before update on group_members
      for each row execute function set_updated_at();
  `);

  pgm.createTable('photo_groups', {
    photo_id:   { type: 'bigint', notNull: true, references: 'photos', onDelete: 'RESTRICT' },
    group_id:   { type: 'bigint', notNull: true, references: 'groups', onDelete: 'RESTRICT' },
    added_by:   { type: 'bigint', references: 'users', onDelete: 'SET NULL' },
    is_deleted: { type: 'boolean', notNull: true, default: false },
    deleted_at: { type: 'timestamptz' },
    deleted_by: { type: 'bigint', references: 'users', onDelete: 'SET NULL' },
    added_at:   { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
    created_at: { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
    updated_at: { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
  });
  pgm.addConstraint('photo_groups', 'photo_groups_pk', {
    primaryKey: ['photo_id', 'group_id'],
  });
  pgm.createIndex('photo_groups', 'group_id');
  pgm.sql(`
    create index photo_groups_live_idx on photo_groups (photo_id, group_id)
      where not is_deleted
  `);
  pgm.sql(`
    create index photo_groups_group_live_idx on photo_groups (group_id, photo_id)
      where not is_deleted
  `);
  pgm.sql(`
    create trigger photo_groups_set_updated_at before update on photo_groups
      for each row execute function set_updated_at();
  `);

  pgm.sql(`
    comment on table groups is
      'Access-model group. A photo is visible to a user if they share at least one live group with it (photo_groups + group_members). Admins see all non-private photos including unfiled ones. is_private overrides visibility everywhere.'
  `);
};

export const down = (pgm) => {
  pgm.dropTable('photo_groups');
  pgm.dropTable('group_members');
  pgm.dropTable('groups');
  pgm.dropType('group_role');
};
