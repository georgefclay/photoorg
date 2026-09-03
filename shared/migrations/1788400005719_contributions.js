export const shorthands = undefined;

export const up = (pgm) => {
  pgm.createTable('comments', {
    id:         { type: 'bigserial', primaryKey: true },
    photo_id:   { type: 'bigint', notNull: true, references: 'photos', onDelete: 'RESTRICT' },
    user_id:    { type: 'bigint', notNull: true, references: 'users', onDelete: 'RESTRICT' },
    body:       { type: 'text', notNull: true },
    is_hidden:  { type: 'boolean', notNull: true, default: false },
    hidden_by:  { type: 'bigint', references: 'users', onDelete: 'SET NULL' },
    hidden_at:  { type: 'timestamptz' },
    created_at: { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
    updated_at: { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
  });
  pgm.createIndex('comments', 'photo_id');
  pgm.createIndex('comments', 'user_id');
  pgm.sql(`
    create trigger comments_set_updated_at before update on comments
      for each row execute function set_updated_at();
  `);

  pgm.createTable('likes', {
    user_id:    { type: 'bigint', notNull: true, references: 'users', onDelete: 'RESTRICT' },
    photo_id:   { type: 'bigint', notNull: true, references: 'photos', onDelete: 'RESTRICT' },
    created_at: { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
  });
  pgm.addConstraint('likes', 'likes_pk', { primaryKey: ['user_id', 'photo_id'] });
  pgm.createIndex('likes', 'photo_id');

  pgm.createTable('suggestions', {
    id:              { type: 'bigserial', primaryKey: true },
    photo_id:        { type: 'bigint', references: 'photos', onDelete: 'RESTRICT' },
    user_id:         { type: 'bigint', references: 'users', onDelete: 'SET NULL' },
    kind:            { type: 'suggestion_kind', notNull: true },
    payload:         { type: 'jsonb', notNull: true },
    confidence:      { type: 'real' },
    status:          { type: 'suggestion_status', notNull: true, default: 'pending' },
    source:          { type: 'suggestion_source', notNull: true },
    model:           { type: 'text' },
    resolved_by:     { type: 'bigint', references: 'users', onDelete: 'SET NULL' },
    resolved_at:     { type: 'timestamptz' },
    resolution_note: { type: 'text' },
    created_at:      { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
    updated_at:      { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
  });
  pgm.createIndex('suggestions', ['status', 'kind']);
  pgm.createIndex('suggestions', 'photo_id');
  pgm.sql(`
    create trigger suggestions_set_updated_at before update on suggestions
      for each row execute function set_updated_at();
  `);

  // Payload shapes (enforced in app code, not by check constraint; see SCHEMA.md):
  //   date:           {"date":"1962-03-01","precision":"month","evidence":"..."}
  //   person:         {"person_id":12}  or  {"new_person":{"given_name":"...","surname":"..."}}
  //                   optional "face_id"
  //   place:          {"place_id":3}    or  {"new_place":{"name":"..."}}
  //   relationship:   {"person_a_id":1,"person_b_id":2,"type":"parent"}  (photo_id null)
  //   description:    {"text":"two children on a porch with a dog"}
  //   transcription:  {"text":"...","parsed_date":"1962","names":["Peggy"]}  + photo_back_id
  //   classification: {"label":"document","confidence":0.93}
  pgm.sql(`
    comment on table suggestions is
      'Facts vs. suggestions: AI and user input land here with status=pending; only an admin promotes a suggestion into the target row''s columns. Payload shapes: date {date,precision,evidence}; person {person_id|new_person,face_id?}; place {place_id|new_place}; relationship {person_a_id,person_b_id,type} (photo_id null); description {text}; transcription {text,parsed_date,names,photo_back_id}; classification {label,confidence}.'
  `);
};

export const down = (pgm) => {
  pgm.dropTable('suggestions');
  pgm.dropTable('likes');
  pgm.dropTable('comments');
};
