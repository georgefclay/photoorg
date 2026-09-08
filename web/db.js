const { Pool } = require('pg');

function makePool(connectionString) {
  return new Pool({
    connectionString: connectionString || process.env.DATABASE_URL,
    max: 10,
    idleTimeoutMillis: 30_000,
  });
}

module.exports = { makePool };
