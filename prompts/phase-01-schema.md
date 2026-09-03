# Phase 1 — Database schema and migrations

Run in Claude Code with `C:\Programming\Photos` as the working directory. Read `PROJECT-PLAN.md` §1–§2 and `photo-archive-build-prompts.md` §0–§1 first. Phase 0 is done: `shared/` has node-pg-migrate 9 wired to `DATABASE_URL` in `shared/.env` (database `photoorg`, role `photo_user`).

GOAL: the complete Postgres schema as ordered migrations in `shared/migrations/`, a nickname seed, a completeness function, and a smoke test. The same migrations will later run unchanged on the AWS VM.

GROUND RULES (from the spec):
- Deletes are never real deletes: soft-delete flags everywhere.
- The database is the source of truth. Albums are virtual.
- Facts and guesses are different columns. AI and family input go to `suggestions`; only an admin promotes them.
- Every state change is audited with the previous value. Audit history lives here, never in image metadata.

## Migrations

Use `npm run migrate:create -- <name>` so timestamps order them. One migration per group below, in this order. Use `timestamptz` for all timestamps, `bigserial`/`bigint` ids, `text` not `varchar`. Every table gets `created_at timestamptz not null default now()`; mutable tables also get `updated_at` maintained by a shared trigger function `set_updated_at()` created in migration 1.

### 1 — extensions, enums, helpers
- `create extension if not exists pg_trgm;` and `fuzzystrmatch` (for Metaphone in Phase 11). Do **not** require `pgvector`; embeddings are `real[]` (see faces).
- Enums: `date_precision` (exact, month, year, decade, unknown); `triage_status` (untriaged, keep, junk, private); `face_source` (ai, human); `relationship_type` (parent, spouse, sibling); `suggestion_kind` (date, person, place, relationship, description, transcription, classification); `suggestion_status` (pending, accepted, rejected); `suggestion_source` (human, ai, import); `user_role` (admin, contributor); `user_status` (active, suspended); `access_status` (pending, approved, denied); `name_variant_kind` (nickname, misspelling, alternate_spelling).
- `set_updated_at()` trigger function.

### 2 — photos and masters
`photos`:
- id, `working_path text` (nullable until ingest writes the copy), `sha256 text not null unique` (of the preferred master), `phash text`, `dhash text`, `width int`, `height int`, `mime text not null`, `file_size bigint`
- `is_scan bool not null default false`, `has_no_people bool not null default false`
- `capture_date date`, `capture_date_precision date_precision not null default 'unknown'`, `capture_date_confirmed bool not null default false`
- `exif_taken_at timestamptz`, `exif_camera text`, `exif_gps_lat double precision`, `exif_gps_lon double precision`
- Physical / provenance: `source_root text not null` (`photos` | `scans`), `source_folder text not null`, `source_filename text not null`, `scan_batch text` (folder name for scans, null for digital), `scan_sequence int` (1-based position within the folder by filename sort), `physical_ref_note text`, `rescan_wanted bool not null default false`
- State: `triage_status triage_status not null default 'untriaged'`, `is_private bool not null default false`, `is_deleted bool not null default false`, `deleted_at timestamptz`, `quarantine_path text`
- `file_version int not null default 1` (bump whenever `working_path` content changes; sync uses it), `synced_at timestamptz`, `synced_file_version int`
- `description_ai text` (accepted AI description, for FTS), `completeness_score int not null default 0`
- Unique on `(source_root, source_folder, source_filename)`. Indexes: `phash`, `dhash`, `(scan_batch, scan_sequence)`, `capture_date`, `triage_status`, `is_private`, `is_deleted`, partial index on `rescan_wanted where rescan_wanted`.

`photo_masters`: id, `photo_id` FK, `master_path text not null unique`, `sha256 text not null unique`, `width int`, `height int`, `dpi int`, `mime text not null`, `file_size bigint`, `is_preferred bool not null default false`, `ingested_at timestamptz not null default now()`. Partial unique index: one preferred per photo. Ingest inserts here first; `photos.sha256` mirrors the preferred row.

`photo_backs`: id, `photo_id` FK, `master_path text not null unique`, `sha256 text not null unique`, `working_path text`, `source_folder`, `source_filename`, `scan_sequence int`, `transcribed_text text`, `transcription_confidence real`, `transcription_confirmed bool not null default false`. Backs are never `photos` rows.

### 3 — people, names, relationships
`people`: id, given_name, middle_name, surname, maiden_name, nickname, `birth_year int`, `death_year int`, notes, `is_deleted bool default false`. Generated column or trigger-maintained `display_name text`.
`person_name_variants`: id, person_id FK, variant text, kind name_variant_kind. Unique `(person_id, lower(variant))`. GIN trigram index on `variant`.
`relationships`: id, person_a_id, person_b_id, type relationship_type, confirmed bool default false, created_by (users FK, nullable). Check `person_a_id <> person_b_id`. Unique on the ordered triple. Grandparent/cousin are derived by traversal, never stored.

### 4 — faces
`faces`: id, photo_id FK, person_id FK nullable, `bbox jsonb not null` (`{x,y,w,h}` in pixels of the working copy), `embedding real[]`, `embedding_model text`, `confidence real`, `source face_source not null`, `is_disputed bool not null default false`, `disputed_by` users FK nullable, `dispute_note text`, `created_by` users FK nullable, `is_deleted bool default false`. Indexes on `photo_id`, `person_id`, partial on `is_disputed`.

### 5 — places, albums
`places`: id, name text not null, latitude, longitude, notes, `is_deleted`. Unique `lower(name)`.
`photo_places`: (photo_id, place_id) PK, `confirmed bool default false`.
`albums`: id, name, description, `created_by` users FK nullable, `source text` (`manual` | `import`), `is_deleted`.
`album_photos`: (album_id, photo_id) PK, `position int`.

### 6 — users, access, sessions
`users`: id, `email text not null unique` (store lower-cased; check constraint), display_name, role user_role not null default 'contributor', status user_status not null default 'active', `is_service bool not null default false`, last_login_at.
`access_requests`: id, email, message, status access_status default 'pending', `token text unique`, `token_expires_at timestamptz`, decided_by users FK, decided_at.
`magic_links`: id, user_id FK, `token_hash text not null unique`, expires_at, used_at. Expiry is always computed in SQL (`now() + interval`), never in Node — put that in a comment.
`session`: the exact table `connect-pg-simple` expects (`sid varchar primary key, sess json not null, expire timestamp(6) not null`, index on expire).

### 7 — contributions
`comments`: id, photo_id FK, user_id FK, body text not null, is_hidden bool default false, hidden_by, hidden_at.
`likes`: (user_id, photo_id) PK, created_at.
`suggestions`: id, photo_id FK nullable (relationship suggestions have none), user_id FK nullable, kind suggestion_kind, `payload jsonb not null`, `confidence real`, status suggestion_status default 'pending', source suggestion_source not null, `model text` (for ai), resolved_by, resolved_at, `resolution_note text`. Indexes on `(status, kind)`, `photo_id`.
Payload shapes, documented in a comment on the table:
- date: `{"date":"1962-03-01","precision":"month","evidence":"handwritten on back"}`
- person: `{"person_id":12}` or `{"new_person":{"given_name":"...","surname":"..."}}`, optional `"face_id"`
- place: `{"place_id":3}` or `{"new_place":{"name":"..."}}`
- relationship: `{"person_a_id":1,"person_b_id":2,"type":"parent"}`
- description: `{"text":"two children on a porch with a dog"}`
- transcription: `{"text":"...","parsed_date":"1962","names":["Peggy"]}` (attach `photo_back_id` too)
- classification: `{"label":"document","confidence":0.93}`

### 8 — audit and jobs
`audit_log`: id, user_id FK nullable, `actor text` (user email, `desktop`, `system`), action text, entity_type text, entity_id bigint, previous_value jsonb, new_value jsonb, created_at. Indexes on `(entity_type, entity_id)`, `created_at`, `user_id`.
`job_runs` and `job_items` for the desktop batch runner: `job_runs` (id, job_name text, started_at, finished_at, status, params jsonb); `job_items` (job_run_id FK, photo_id FK, status text, error text, updated_at; PK on the pair). Also `photo_job_status` (photo_id, job_name, model text, status, completed_at; PK `(photo_id, job_name)`) so "re-run describe for everything after a model upgrade" is one update.

### 9 — search support
- `photos.search_tsv tsvector` maintained by trigger from `description_ai` plus all non-hidden comment bodies plus back transcriptions (a function `refresh_photo_tsv(photo_id)` called by triggers on `comments` and `photo_backs`). GIN index.
- `people.search_key text` = metaphone of surname, maiden_name, given_name concatenated; trigger-maintained; index.

### 10 — completeness
Function `compute_completeness(photo_id) returns int`: 40 points for a confirmed date, 40 for at least one non-disputed face with a person **or** `has_no_people`, 20 for a place. Trigger-free; a helper `refresh_completeness(photo_id)` updates the column. Also a `refresh_all_completeness()` for batch use.

### Seed
`shared/seed/nicknames.csv` populated with a standard English nickname list (at least 150 canonical names, e.g. Richard→Rick/Dick/Richie/Rich, Margaret→Peggy/Maggie/Meg/Marge/Greta, Elizabeth→Liz/Beth/Betty/Eliza/Lisa/Bess, William→Bill/Will/Billy/Willie, Katherine→Kate/Kathy/Cathy/Kay/Kitty). `shared/seed/nicknames.js` loads it into a standalone table `nickname_dictionary(canonical, variant)` (created in migration 3) that Phase 11 search joins against. It is a dictionary, not per-person data; `person_name_variants` stays hand-curated.

### Smoke test
`shared/test/smoke.js` (run with `npm test`): against a fresh DB, `migrate up`, insert one user, photo (+ master), person, face, place, album, comment, like, suggestion, audit row; call `refresh_completeness`; assert score 0, then confirm the date and assert 40; `migrate down` to zero and back up again cleanly. Exits non-zero on any failure.

### Docs
`shared/SCHEMA.md`: one paragraph per table, the payload shapes, and the rule about which columns are facts vs. suggestions. Keep it under 200 lines.

## Verification, then stop
- `npm run migrate:up` from empty → all migrations apply; `npm run migrate:down` ×N → clean; up again.
- `npm test` passes.
- `node seed/nicknames.js` loads ≥ 400 rows.
- Create `CLAUDE.md` at the repo root (committed) with the standing rules every future session must know: the masters rule; DB `photoorg` / role `photo_user`; migrations live only in `shared/migrations/`; expiries computed in SQL; soft deletes only; facts vs. suggestions; where `PROJECT-PLAN.md`, `SCHEMA.md`, and `prompts/` are. Under 60 lines.
- Commit: `Phase 1: schema, seed, smoke test`.

Report back with the migration list, the smoke test output, and any place you deviated from this prompt and why.

---

## Answers to Claude Code's questions

1. **Migration style.** Mixed, as you propose: JS DSL for plain tables and indexes, raw `pgm.sql` for enums, functions, triggers, generated columns, partial/unique-expression indexes. Down migrations must be complete for both.
2. **Test runner.** `node --test`. No Jest/Vitest.
3. **Fresh DB.** (b). `TEST_DATABASE_URL` in `shared/.env.example`; the test refuses to run if `TEST_DATABASE_URL` equals `DATABASE_URL`. Print the one-time `createdb photoorg_test` command for George.
4. **display_name.** Trigger-maintained. Format: `given_name "nickname" surname` when nickname present, else `given_name surname`; append ` (née maiden_name)` when maiden_name is set and differs from surname. Skip empty parts cleanly. Example: `Margaret "Peggy" Clay (née Schmidt)`.
5. **Nickname seed.** Public-domain genealogy list is fine; cite the source and licence in the CSV header. Prefer the list at github.com/carltonnorthern/nicknames (Apache-2.0) or an equivalent; hand-add anything obviously missing.
6. **session table.** Match connect-pg-simple exactly, with the comment.
7. **TSV trigger on photos.** Yes, add it.
8. **Suggestion check constraint.** Leave it soft. App code enforces; note the rule in `SCHEMA.md`.

---

## Phase 1 follow-up (after the first report)

Two changes, one new migration, then re-run the smoke test.

A. **No cascading deletes.** The project rule is "no real deletes, ever". `onDelete: 'CASCADE'` on `comments.photo_id`, `likes.*`, `suggestions.photo_id`, and anywhere else it appears (`album_photos`, `photo_places`, `faces.photo_id`, `photo_masters`, `photo_backs`, `job_items`, `person_name_variants`, `relationships`, `magic_links`, …) would silently destroy history if a parent row were ever hard-deleted. Change every FK to `RESTRICT` (or the default `NO ACTION`). `SET NULL` on `*_by` user references is fine and stays. Do this by editing the existing migrations (nothing is deployed yet), not by a new migration.

B. **Late foreign keys.** Add migration 11 `late-foreign-keys` that adds the FKs that were skipped because `users` did not exist yet: `faces.disputed_by`, `faces.created_by`, `relationships.created_by`, `albums.created_by`, all `references users on delete set null`. Down drops them. Keep `audit_log.entity_id` as a bare bigint — it is polymorphic on purpose.

C. Update `SCHEMA.md` with one line under the rules: "All parent/child FKs are RESTRICT; rows are soft-deleted, never removed."

D. George runs `createdb -U postgres -O photo_user photoorg_test`, then you run `npm test` and paste the output. Verify `migrate down 0` still reaches zero with the new migration, and up again.

Commit: `Phase 1 follow-up: RESTRICT FKs, late user FKs`.
