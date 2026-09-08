require('dotenv').config();

const { createApp } = require('./app');
const { makePool } = require('./db');

const PORT = process.env.PORT || 8090;

const pool = makePool();
const app = createApp({ pool });

app.listen(PORT, () => {
  console.log(`Photo Archive web listening on http://localhost:${PORT}`);
});
