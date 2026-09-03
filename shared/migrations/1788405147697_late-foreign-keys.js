// FKs to users that could not be declared in the original migrations because
// users (migration 6) is created after faces (4), relationships (3), and
// albums (5). audit_log.entity_id is intentionally left as a bare bigint
// (polymorphic across entity_type).

export const shorthands = undefined;

export const up = (pgm) => {
  pgm.addConstraint('faces', 'faces_disputed_by_fkey', {
    foreignKeys: {
      columns: 'disputed_by',
      references: 'users(id)',
      onDelete: 'SET NULL',
    },
  });
  pgm.addConstraint('faces', 'faces_created_by_fkey', {
    foreignKeys: {
      columns: 'created_by',
      references: 'users(id)',
      onDelete: 'SET NULL',
    },
  });
  pgm.addConstraint('relationships', 'relationships_created_by_fkey', {
    foreignKeys: {
      columns: 'created_by',
      references: 'users(id)',
      onDelete: 'SET NULL',
    },
  });
  pgm.addConstraint('albums', 'albums_created_by_fkey', {
    foreignKeys: {
      columns: 'created_by',
      references: 'users(id)',
      onDelete: 'SET NULL',
    },
  });
};

export const down = (pgm) => {
  pgm.dropConstraint('albums', 'albums_created_by_fkey');
  pgm.dropConstraint('relationships', 'relationships_created_by_fkey');
  pgm.dropConstraint('faces', 'faces_created_by_fkey');
  pgm.dropConstraint('faces', 'faces_disputed_by_fkey');
};
