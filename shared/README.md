# shared

Postgres migrations (node-pg-migrate), nickname seed, API contract notes.
The same migrations run on the laptop's local DB and on the VM's DB.
Phase 0 has no migrations yet — Phase 1 writes the schema.

## Commands

```
npm install
npm run migrate:up
npm run migrate:down
npm run migrate:create -- some-migration-name
```

Reads `DATABASE_URL` from `shared/.env`.
