// Insert an audit_log row inside whatever client/pool is passed in.
//   audit(pg, {
//     actor: 'georgefclay@gmail.com',          // required
//     action: 'auth.approve',                  // required, dotted namespace
//     entityType: 'access_request',            // required
//     entityId: 42,                            // optional
//     userId: 3,                               // optional, the acting user
//     previousValue: { status: 'pending' },    // optional JSON
//     newValue: { status: 'approved' },        // optional JSON
//   });
async function audit(pg, {
  actor,
  action,
  entityType,
  entityId = null,
  userId = null,
  previousValue = null,
  newValue = null,
}) {
  if (!actor || !action || !entityType) {
    throw new Error('audit: actor, action, entityType are required');
  }
  await pg.query(
    `insert into audit_log
       (user_id, actor, action, entity_type, entity_id, previous_value, new_value)
     values ($1, $2, $3, $4, $5, $6, $7)`,
    [userId, actor, action, entityType, entityId, previousValue, newValue],
  );
}

module.exports = { audit };
