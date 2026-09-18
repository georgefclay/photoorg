# Schema

Postgres. `bigserial` ids, `text` not `varchar`, `timestamptz` everywhere except the `session` table (which matches `connect-pg-simple` verbatim). Every mutable table has `created_at`/`updated_at`; the trigger `set_updated_at()` keeps `updated_at` current on `UPDATE`.

## Facts vs. suggestions

The rule that governs the whole schema:

- **Facts** live in the target table's columns (`photos.capture_date`, `faces.person_id`, `photo_places`, `photos.description_ai`, `photo_backs.transcribed_text` with `transcription_confirmed = true`).
- **Suggestions** — from AI, family contributors, or the import step — live in `suggestions` with `status = 'pending'`. An admin promotes a suggestion by copying its payload into the facts row and setting `resolved_at`, `resolved_by`, `status`.
- Nothing else writes AI output straight into a fact column.
- Deletes are soft: `is_deleted` and `deleted_at` on every entity that carries them; quarantined files sit at `quarantine_path`.
- All parent/child FKs are `RESTRICT`; rows are soft-deleted, never removed. `SET NULL` is used only on user references (`*_by`, actor `user_id`) so a suspended user's contributions retain a null attribution.
- Every state change writes an `audit_log` row with `previous_value` and `new_value` (JSONB), never in image metadata.

## Id ranges (Phase 9 fix-up 1)

`shared/id-ranges.json` holds `web_id_floor` (1 000 000 000 000) and the
tables it covers: `faces`, `people`, `suggestions`, `albums`, `places`,
`relationships`, `person_name_variants` (all `bigserial`). Desktop-born
ids stay below the floor; on a web DB those sequences start at it.
Migration `phase-9-fixup-1-web-origin-ids` is gated on
`PHOTOORG_DB_ROLE=web` and is a recorded no-op otherwise. `users`,
`groups`, `comments`, `likes`, `contributions` are web-only tables and
need no range; `photos`, `photo_masters`, `photo_backs` are desktop-only.

## Tables

### Photos and physical files

- **`photos`** — one row per unique photograph. `sha256` (of the preferred master) is the identity. `working_path` is the derived copy the desktop app manipulates; masters are never touched. `source_root` / `source_folder` / `source_filename` / `scan_batch` / `scan_sequence` record where the file came from and, for scans, how to find the physical print (`Batch 00012 #017`). `capture_date` + `capture_date_precision` (`exact|month|year|decade|unknown`) + `capture_date_confirmed` separate a confirmed date from a hint. `triage_status` (`untriaged|keep|junk|private`), `is_private`, `is_deleted`, `rescan_wanted` are workflow flags. `file_version` bumps whenever the working copy changes so sync knows to re-push. `description_ai` is the accepted AI description (feeds FTS). `completeness_score` is 0–100, maintained by `refresh_completeness(photo_id)`. `width` / `height` are the **display** dimensions (post EXIF-transpose) since fix-up 6 — the coordinate frame face bboxes are stored in. `orientation` is the EXIF value (1..8, migration 23); `photo_masters.width/height` on each master row remain the raw file dims.
- **`photo_masters`** — one photograph, many master files (original 300 DPI JPG, later 1200 DPI TIFF rescan, etc.). Exactly one is `is_preferred` per `photo_id` (partial unique index). Ingest inserts here first, then `photos.sha256` mirrors the preferred row. Rescans add a new preferred row and demote the old one.
- **`photo_backs`** — scans of the back of a physical print. `photo_id` is nullable (migration 15) so an "orphan back" — a scanned back whose front we cannot identify — can still be stored, OCR'd in Phase 6, and possibly reunited with its front later. Backs are never photos themselves. `transcribed_text` + `transcription_confidence` + `transcription_confirmed` hold the OCR/VLM result.

### People, names, relationships

- **`people`** — one row per person. `given_name`, `middle_name`, `surname`, `maiden_name`, `nickname`, `suffix` (Jr./II/III…, Phase 6 fix-up 4), `birth_year`, `death_year`, `notes`, `is_deleted`. `display_name` is trigger-maintained from the parts: e.g. `Margaret "Peggy" Clay (née Schmidt)`, `George Clay Jr.`, `John "Jack" Smith III`.
- **`person_name_variants`** — hand-curated aliases per person (nickname / misspelling / alternate_spelling). Unique `(person_id, lower(variant))`; GIN trigram index on `variant` for fuzzy match.
- **`relationships`** — pairwise, typed `parent|spouse|sibling`. Check `person_a_id <> person_b_id`; unique on `(person_a_id, person_b_id, type)`. Grandparent / cousin are derived by traversal at query time, never stored.
- **`nickname_dictionary`** — canonical→variant pairs shared across everyone (Rick→Richard, Peggy→Margaret, …). Populated from `seed/nicknames.csv` (Apache-2.0 list from carltonnorthern/nicknames). Phase 11 search joins against this; it is NOT per-person data.

### Faces

- **`faces`** — one detected face box per row. `photo_id` FK, `person_id` FK nullable (unlabelled faces are still stored), `bbox` JSONB (`{x,y,w,h}` in working-copy pixels), `embedding` `real[]` + `embedding_model` (InsightFace/ArcFace via ONNX; no pgvector needed). `source` (`ai|human`), `is_disputed` + `disputed_by` + `dispute_note` support family disputes; disputed faces are excluded from the face reference set. `created_by` records the user who labelled or confirmed. `is_deleted` + `deleted_at` + `delete_reason` (Phase 6 migration 21) hold "not a face" soft-deletes from the Faces mode; every selector filters `not is_deleted`. `review_status` (Phase 6 fix-up 8, migration 24) is one of `pending | unknown | ignore` for unassigned faces — assigned faces keep `pending` because their identity comes from `person_id`. `unknown` faces stay in clustering (a later labelled person's prototype can match them) but drop to the "Unknown queue" behind the main flow; `ignore` faces are excluded from clustering and reference sets entirely. `review_note` + `reviewed_at` capture the decision. Sync (Phase 9) pushes `review_status`; the web will surface `unknown` faces as "Who is this?" prompts. `embedding_stale` (Phase 6 fix-up 9, migration 25) is set to true when a face's bbox is edited without a matching `/detect-faces` refresh; a maintenance path (or the next detect_faces pass) recomputes the embedding from the current bbox.

### Places and albums

- **`places`** — `name` unique on `lower(name)`, optional `latitude`/`longitude`, `notes`, `is_deleted`.
- **`photo_places`** — PK `(photo_id, place_id)`, `confirmed` bool. Confirmed by admin promotion of a place suggestion.
- **`albums`** — `name`, `description`, `created_by`, `source` (`manual|import`), `is_deleted`. Named scan folders become `source='import'` albums at ingest.
- **`album_photos`** — PK `(album_id, photo_id)`, `position` for ordering.

### Users, access, sessions

- **`users`** — `email` unique, stored lower-cased (check constraint). `role` (`admin|contributor`), `status` (`active|suspended`), `is_service` for the desktop app's service account. Suspending kills sessions but keeps contributions.
- **`access_requests`** — pending signups. `token` + `token_expires_at` for the Approve/Deny magic link in the admin email. All expiries are computed in SQL (`now() + interval`), never in Node.
- **`magic_links`** — hashed one-time login tokens per user. Same SQL-computed expiry rule.
- **`session`** — the exact schema `connect-pg-simple` expects: `sid varchar collate "default" pk`, `sess json`, `expire timestamp(6)`, index on `expire`. Do not "normalise" this; the middleware installs no schema of its own.

### Contributions

- **`comments`** — per-photo threaded remarks. `is_hidden` + `hidden_by` + `hidden_at` for moderation. Hidden comments are excluded from the search vector.
- **`likes`** — PK `(user_id, photo_id)`.
- **`suggestions`** — the queue of pending facts (see rule above). `kind` enum; `payload` JSONB with a shape per kind (see below); `source` (`human|ai|import`); `model` for AI provenance; `confidence` real; resolution fields for the admin who promotes or rejects it. `photo_id` is nullable — relationship suggestions have no photo. This is soft — app code enforces payload shape; there is no check constraint.

  Payload shapes:
  - `date`: `{"date":"1962-03-01","precision":"month","evidence":"handwritten on back"}`
  - `person`: `{"person_id":12}` or `{"new_person":{"given_name":"...","surname":"..."}}`, optional `"face_id"`
  - `place`: `{"place_id":3}` or `{"new_place":{"name":"..."}}`
  - `relationship`: `{"person_a_id":1,"person_b_id":2,"type":"parent"}` (`photo_id` null)
  - `description`: `{"text":"two children on a porch with a dog"}`
  - `transcription`: `{"text":"...","parsed_date":"1962","names":["Peggy"],"photo_back_id":34}`
  - `classification`: `{"label":"document","confidence":0.93}`

### Groups (Phase 9, migration `phase-9-groups`)

The unit of visibility on the web. A photo is visible to a user when
they share at least one live group with it (`photo_groups + group_members`,
both `is_deleted = false`); admins see every non-private, non-deleted
photo including unfiled; `is_private` and `is_deleted` always exclude.

- **`groups`** — `name`, `description`, `created_by`, soft-delete fields
  (`is_deleted / deleted_at / deleted_by`). Name uniqueness is
  case-insensitive per **live** row (partial unique index
  `groups_name_unique_live on lower(name) where not is_deleted`).
- **`group_members`** — PK `(group_id, user_id)`. `role` is
  `group_role` enum (`member | moderator`). Soft-delete fields;
  `updated_at` trigger for LWW sync.
- **`photo_groups`** — PK `(photo_id, group_id)`. Soft-delete fields;
  `updated_at` trigger for LWW sync. Partial live index
  `photo_groups_live_idx (photo_id, group_id) where not is_deleted` for
  the hot visibility path.

### Contribution uploads (Phase 9, migration `phase-9-contributions`)

- **`contributions`** — one row per upload session. `user_id`
  (nullable — uploader can be soft-deleted later), `note`, `status`
  `contribution_status` enum (`pending|approved|rejected|partial`),
  `group_ids bigint[]` (uploader's chosen target groups; app-enforces
  membership), `decided_by`, `decided_at`, `pulled_at`, `finished_at`.
- **`contribution_files`** — one row per file. `contribution_id` FK,
  `original_filename`, `stored_path` (relative to `PHOTO_DIR` — usually
  `uploads/<contribution_id>/<file_id>.<ext>`), `sha256` UNIQUE, `size`,
  `mime`, `width`, `height`, `exif_taken_at`, `phash` (64-bit dHash),
  `status` `contribution_file_status` enum, `decided_by`, `decided_at`,
  `approved_group_ids bigint[]` (which groups this specific file has
  been approved into — moderator approve only adds their own group,
  admin approve-all adds every target group), `duplicate_of_photo_id`,
  `duplicate_distance`, `is_video`.

### Photo sync columns

`photos.synced_at` and `photos.synced_file_version` (both from Phase 1)
track what has been pushed to the web. `POST /sync/photos` returns
`need_files: [ids]` for anything whose `synced_file_version <
file_version` or whose file is missing on disk; `PUT /sync/photos/:id/file`
writes the working file, regenerates the 320-px thumb via `sharp`, and
sets `synced_file_version = file_version`. Re-running push after a
successful one sends 0 files.

### Ingest staging (migration 12)

- **`ingest_pairings`** — proposed front/back pairs held between the ingest scan pass and George's review. `front_photo_id` FK to the already-committed front photo (nullable as of migration 16 — a proposal whose immediate predecessor was itself a probable back has front_photo_id null, and George decides in the grid with N=orphan or F=pick front from filmstrip); `back_master_path` / `back_sha256` identify the back file on disk (unique). `back_score` 0–1 from the back-detect heuristic (1.0 when George asserts it via the Triage B key). `staging_working_path` and `staging_thumb_path` point to `WORKING_DIR/_staging/{sha256}.{ext}` and `THUMBS_DIR/_staging/{sha256}.jpg` for held (not-yet-committed) backs. `back_photo_id` (nullable, added migration 13) points to an already-committed photo when the Rebuild-back-proposals action or the Triage B key re-classifies it as a back. `back_aspect_mismatch` boolean (migration 14): true when the back's aspect ratio differs from the front's. Aspect is a hard gate below score 0.8 and a review tag at or above 0.8 — a back can be cropped very differently from its front, so aspect is evidence not veto for strong candidates. `details` JSONB (migration 19) carries proposal provenance, e.g. `{"source": "triage", "reason": "orphan_predecessor_is_back"}` for B-key entries. `status ingest_proposal_status` (`pending|accepted|rejected`) + `decided_at`. Accepting a held back inserts a `photo_backs` row for `front_photo_id` and renames the staged files into place. Accepting a photo-as-back (rebuild path) inserts a `photo_backs` row referencing the demoted photo's working file and thumb, then marks the old `photos` row `is_deleted=true` with `physical_ref_note='converted to back of photo <front_id>'`. Rejecting a held back takes it through the normal new-photo path. Rejecting a photo-as-back leaves the photo alone and blocks re-proposal (unique index on `back_photo_id` where pending prevents duplicates; the rejected row remains and rebuild skips already-rejected photos).
- **`ingest_rescans`** — proposed rescans (a new file whose pHash Hamming distance ≤ 6 to an existing scan photo). `existing_photo_id` FK; `new_master_path` / `new_sha256` identify the incoming file (unique). Full source metadata (`new_source_root/folder/filename`, `new_scan_batch`, `new_scan_sequence`, `new_width`/`height`/`file_size`/`mime`) is carried on the staging row so accepting does not require re-decoding. Same staging paths and status columns as `ingest_pairings`. Accepting inserts a new `photo_masters` row for `existing_photo_id`, marks it preferred if its pixel count is larger, and updates `photos.sha256` / `working_path` / `file_version`; rejecting inserts a normal new photo.
- **`ingest_failures`** — files that failed to ingest (unreadable, undecodable, hash error). `job_items` requires a non-null `photo_id`, and these files never got a photos row, so failures land here instead. Records the run, source root/folder/filename, master_path, and error text.

### Triage hints (migration 17)

- **`triage_hints`** — one row per photo (PK `photo_id`) written by the
  `triage_presort` job (Phase 3), with an additional value `ai_junk`
  added by the Phase 6 `classify` writer. `hint` is one of `photo |
  screenshot | document | blank_or_dark | possible_back | tiny | burst |
  exact_dup_of | ai_junk` after the precedence cascade (`exact_dup_of` →
  `screenshot` → `possible_back` → `blank_or_dark` → `tiny` → `document`
  → `burst`). `ai_junk` is set only when no presort row exists — presort
  wins; the AI label survives inside the existing row's
  `details.also.ai_classify`.
  `confidence` 0–1. `details` JSONB carries per-hint evidence (e.g.
  dimensions, tone stats, sharpest peer in a burst group, ink fraction for
  `possible_back`) and a `details.also` key holding the losing hints so
  nothing is lost. Hints set the default decision key in the Triage grid;
  they never make a decision on their own.
  - **`possible_back`** (migration 18) fires when a scan-root photo would
    otherwise be tagged `blank_or_dark` but the ink measure is above 0.001
    — likely a print's back with only a date written on it. George presses
    B in Triage to promote it to a pending pairing.

### Dedupe (migration 20)

- **`dedupe_groups`** — one row per candidate near-duplicate group. `status`
  is `pending | resolved | not_duplicates` (enum `dedupe_group_status`).
  `size` and `min_distance` are cached at scan time so the UI can sort the
  queue without joining. `resolved_at` / `resolved_by` set when the reviewer
  accepts or marks not-duplicates. Pending groups are dropped and rebuilt
  by every `dedupe_scan` run; resolved and not_duplicates groups are
  left alone.
- **`dedupe_members`** — the photos in each group. `is_keeper` (exactly
  one per resolved group); `phash_dist` and `dhash_dist` nullable (only
  the algo(s) that matched are populated); `matched_by` (`phash | dhash |
  both`); `transform` (`identity | mirror | rot90 | rot180 | rot270 |
  rot90+mirror | rot270+mirror`) records the alignment that made the pair
  match; `distance_to_keeper` is min of phash/dhash to the eventual
  keeper; `keeper_reason` is the human-readable reason chain shown in
  the UI ("has EXIF > 2.0x pixels"). Unique `(group_id, photo_id)`.
  At-most-one-pending-group-per-photo is enforced by the orchestrator
  (partial unique indexes cannot cross tables).
- **`dedupe_exclusions`** — pairs the reviewer has marked "not
  duplicates". Stored `(least, greatest)` (check constraint enforces
  `photo_a < photo_b`) with unique `(photo_a, photo_b)`. Dedupe scan
  filters these out and never re-proposes them, even after new scans
  add new photos.

### Audit and jobs

- **`audit_log`** — id, `user_id` nullable, `actor` (`user email | 'desktop' | 'system'`), `action`, `entity_type`, `entity_id`, `previous_value` / `new_value` JSONB, `created_at`. Every state change writes one.
- **`job_runs`** — one row per invocation of a batch job (classify, describe, detect-faces, …). `params` JSONB.
- **`job_items`** — per-photo status within a run; PK `(job_run_id, photo_id)`.
- **`photo_job_status`** — per-photo, per-job current state. `model` and (as of Phase 6 migration 21) `prompt_version` remembered so an upgrade to either — new VLM weights *or* a new prompt template — is `update photo_job_status set status='pending' where job_name='describe'` and the batch runner re-runs the whole set at the current model+prompt_version. Selectors gate on this rather than on downstream row counts (so a legitimately zero-face photo isn't re-processed forever).
- **`job_cursors`** (Phase 6, migration 21) — one row per job name; `line_no` is the last NDJSON line index consumed from the mini's per-job results file (`LOG_DIR/batches/{job_name}.ndjson` on the service). Collect reads strictly after this cursor, applies each writer idempotently in a transaction with the cursor advance, then sweeps the inbox with `?done=true`. Rewinding the cursor and re-collecting is safe — every writer checks for the row it would insert.

## Search (Phase 11)

Two derived tables, maintained **only** by SQL triggers, so the desktop
(Python) and the VM (Node) both stay correct without either knowing they
exist. Phase 1's `photos.search_tsv`, `refresh_photo_tsv()` and
`people.search_key` were dropped by the Phase 11 migration (its `down`
recreates them); there is one search path now.

- **`photo_search`** — `photo_id` PK (cascades with the photo), `tsv`, `names`, `updated_at`. `tsv` is `to_tsvector('english', unaccent(...))` with weights: **A** `photos.description_ai` + the names of people tagged on the photo; **B** back-of-print transcriptions; **C** non-hidden comments, album names, place names, the newest pending `description` suggestion (text + tags), pending `person`/`place` suggestion names, pending `date` evidence; **D** `source_folder`, `source_filename`, `physical_ref_note` and the scan locator. `names` is the tagged people's display names, `' | '`-joined (it backs the result card's "why" line). GIN on `tsv`, GIN trigram on `names`.
- **`person_search`** — `(person_id, token, kind)` PK, plus `phonetic`. One row per string a person can be called, normalised by `search_token()` (lower-cased, unaccented, punctuation to spaces). `kind` is `exact` (the `people` name columns), `variant` (`person_name_variants`, the whole string **and** each word) or `nickname` (`nickname_dictionary`, **both** directions — a Margaret gets peggy/peg/meg, a Peggy gets margaret). `phonetic` is `dmetaphone(token)` for single-word tokens; the constraint also allows a hand-added `phonetic` row. Indexes: btree on `token`, GIN trigram on `token`, btree on `phonetic`.
- **`place_aliases`** — `(place_id, alias)` PK (composite on purpose: no bigserial, so the desktop/web id-range rule does not apply), `kind`, `created_at`, plus a case-insensitive unique index and a trigram index. **No sync route yet** — like album edits, pulling these back to the desktop is an open item for Phase 12.
- **Helpers**: `search_token(text)`, `search_text(text)`, `photo_date_range(date, precision)` → `daterange` (a decade-precision photo covers its decade, which is how a year search finds it and a decade search finds a year-precision photo), `suggestion_date_range(jsonb)`, `search_suggestion_text(kind, payload)` (deliberately ignores `classification` — the model's reasoning sentence would swamp every other layer).
- **Maintenance**: `refresh_photo_search(id)` / `refresh_photos_search(ids)` and `refresh_person_search(id)` / `refresh_people_search(ids)`; full rebuild `rebuild_search()` (`rebuild_photo_search()` + `rebuild_person_search()`). Triggers are statement-level with transition tables on `photos`, `photo_backs`, `comments`, `faces`, `suggestions`, `album_photos`, `photo_places`, `albums`, `places`, `people`, `person_name_variants`; an update trigger cannot carry a column list alongside transition tables, so each update function compares the old and new transition tables itself (a sync push that only bumps `file_version`, or a jobs run that only writes embeddings, refreshes nothing).
- **Bulk-write escape hatch**: measured cost of the triggers is ~4.8 s per 10 000 rows (5 000 faces + 5 000 suggestions over 5 000 photos in one transaction: 0.27 s → 5.07 s). A session doing a big batch may `set local photoarchive.search_defer = on`, which queues photo / person ids into `photo_search_dirty` / `person_search_dirty` instead; `select sweep_search()` afterwards drains them. **Off by default** — nothing sets it today, and if you do set it, sweeping is not optional.
- **`nickname_dictionary` has no trigger** (a 2 827-row re-seed would rebuild everything 2 827 times). `shared/seed/nicknames.js` calls `rebuild_person_search()` when it finishes.
- **Extensions**: `pg_trgm`, `fuzzystrmatch` (`dmetaphone`), `unaccent` — all trusted, so the app role creates them if it has CREATE on the database.

## Completeness

`compute_completeness(photo_id)` returns 0–100:

- 40 for `capture_date_confirmed = true`
- 40 for at least one non-disputed, non-deleted face with a `person_id`, **or** `has_no_people = true`
- 20 for at least one row in `photo_places`

`refresh_completeness(photo_id)` writes the result back into `photos.completeness_score`. `refresh_all_completeness()` recomputes for every non-deleted photo (used by batch jobs after a bulk change).
