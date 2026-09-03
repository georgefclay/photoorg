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

## Tables

### Photos and physical files

- **`photos`** — one row per unique photograph. `sha256` (of the preferred master) is the identity. `working_path` is the derived copy the desktop app manipulates; masters are never touched. `source_root` / `source_folder` / `source_filename` / `scan_batch` / `scan_sequence` record where the file came from and, for scans, how to find the physical print (`Batch 00012 #017`). `capture_date` + `capture_date_precision` (`exact|month|year|decade|unknown`) + `capture_date_confirmed` separate a confirmed date from a hint. `triage_status` (`untriaged|keep|junk|private`), `is_private`, `is_deleted`, `rescan_wanted` are workflow flags. `file_version` bumps whenever the working copy changes so sync knows to re-push. `description_ai` is the accepted AI description (feeds FTS). `completeness_score` is 0–100, maintained by `refresh_completeness(photo_id)`.
- **`photo_masters`** — one photograph, many master files (original 300 DPI JPG, later 1200 DPI TIFF rescan, etc.). Exactly one is `is_preferred` per `photo_id` (partial unique index). Ingest inserts here first, then `photos.sha256` mirrors the preferred row. Rescans add a new preferred row and demote the old one.
- **`photo_backs`** — scans of the back of a physical print. `photo_id` is nullable (migration 15) so an "orphan back" — a scanned back whose front we cannot identify — can still be stored, OCR'd in Phase 6, and possibly reunited with its front later. Backs are never photos themselves. `transcribed_text` + `transcription_confidence` + `transcription_confirmed` hold the OCR/VLM result.

### People, names, relationships

- **`people`** — one row per person. `given_name`, `middle_name`, `surname`, `maiden_name`, `nickname`, `birth_year`, `death_year`, `notes`, `is_deleted`. `display_name` is trigger-maintained from the parts: e.g. `Margaret "Peggy" Clay (née Schmidt)`.
- **`person_name_variants`** — hand-curated aliases per person (nickname / misspelling / alternate_spelling). Unique `(person_id, lower(variant))`; GIN trigram index on `variant` for fuzzy match.
- **`relationships`** — pairwise, typed `parent|spouse|sibling`. Check `person_a_id <> person_b_id`; unique on `(person_a_id, person_b_id, type)`. Grandparent / cousin are derived by traversal at query time, never stored.
- **`nickname_dictionary`** — canonical→variant pairs shared across everyone (Rick→Richard, Peggy→Margaret, …). Populated from `seed/nicknames.csv` (Apache-2.0 list from carltonnorthern/nicknames). Phase 11 search joins against this; it is NOT per-person data.

### Faces

- **`faces`** — one detected face box per row. `photo_id` FK, `person_id` FK nullable (unlabelled faces are still stored), `bbox` JSONB (`{x,y,w,h}` in working-copy pixels), `embedding` `real[]` + `embedding_model` (InsightFace/ArcFace via ONNX; no pgvector needed). `source` (`ai|human`), `is_disputed` + `disputed_by` + `dispute_note` support family disputes; disputed faces are excluded from the face reference set. `created_by` records the user who labelled or confirmed.

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

### Ingest staging (migration 12)

- **`ingest_pairings`** — proposed front/back pairs held between the ingest scan pass and George's review. `front_photo_id` FK to the already-committed front photo (nullable as of migration 16 — a proposal whose immediate predecessor was itself a probable back has front_photo_id null, and George decides in the grid with N=orphan or F=pick front from filmstrip); `back_master_path` / `back_sha256` identify the back file on disk (unique). `back_score` 0–1 from the back-detect heuristic. `staging_working_path` and `staging_thumb_path` point to `WORKING_DIR/_staging/{sha256}.{ext}` and `THUMBS_DIR/_staging/{sha256}.jpg` for held (not-yet-committed) backs. `back_photo_id` (nullable, added migration 13) points to an already-committed photo when the Rebuild-back-proposals action re-classifies it as a back. `back_aspect_mismatch` boolean (migration 14): true when the back's aspect ratio differs from the front's. Aspect is a hard gate below score 0.8 and a review tag at or above 0.8 — a back can be cropped very differently from its front, so aspect is evidence not veto for strong candidates. `status ingest_proposal_status` (`pending|accepted|rejected`) + `decided_at`. Accepting a held back inserts a `photo_backs` row for `front_photo_id` and renames the staged files into place. Accepting a photo-as-back (rebuild path) inserts a `photo_backs` row referencing the demoted photo's working file and thumb, then marks the old `photos` row `is_deleted=true` with `physical_ref_note='converted to back of photo <front_id>'`. Rejecting a held back takes it through the normal new-photo path. Rejecting a photo-as-back leaves the photo alone and blocks re-proposal (unique index on `back_photo_id` where pending prevents duplicates; the rejected row remains and rebuild skips already-rejected photos).
- **`ingest_rescans`** — proposed rescans (a new file whose pHash Hamming distance ≤ 6 to an existing scan photo). `existing_photo_id` FK; `new_master_path` / `new_sha256` identify the incoming file (unique). Full source metadata (`new_source_root/folder/filename`, `new_scan_batch`, `new_scan_sequence`, `new_width`/`height`/`file_size`/`mime`) is carried on the staging row so accepting does not require re-decoding. Same staging paths and status columns as `ingest_pairings`. Accepting inserts a new `photo_masters` row for `existing_photo_id`, marks it preferred if its pixel count is larger, and updates `photos.sha256` / `working_path` / `file_version`; rejecting inserts a normal new photo.
- **`ingest_failures`** — files that failed to ingest (unreadable, undecodable, hash error). `job_items` requires a non-null `photo_id`, and these files never got a photos row, so failures land here instead. Records the run, source root/folder/filename, master_path, and error text.

### Audit and jobs

- **`audit_log`** — id, `user_id` nullable, `actor` (`user email | 'desktop' | 'system'`), `action`, `entity_type`, `entity_id`, `previous_value` / `new_value` JSONB, `created_at`. Every state change writes one.
- **`job_runs`** — one row per invocation of a batch job (classify, describe, detect-faces, …). `params` JSONB.
- **`job_items`** — per-photo status within a run; PK `(job_run_id, photo_id)`.
- **`photo_job_status`** — per-photo, per-job current state. `model` remembered so a model upgrade is `update photo_job_status set status='pending' where job_name='describe'` and the batch runner re-runs the whole set.

## Search

- `photos.search_tsv` is a `tsvector` maintained by `refresh_photo_tsv(photo_id)`, which concatenates `description_ai`, all non-hidden `comments.body`, and all `photo_backs.transcribed_text` and re-parses with `to_tsvector('english', ...)`. Triggers on `photos` (updates to `description_ai`), `comments`, and `photo_backs` call it. GIN index.
- `people.search_key` is space-joined `metaphone(surname) metaphone(maiden_name) metaphone(given_name)`, trigger-maintained. B-tree index; combined with `person_name_variants` trigram matches and the `nickname_dictionary` for the Phase 11 name search ("Peggy" finds Margaret, "Schmitt" finds Schmidt).

## Completeness

`compute_completeness(photo_id)` returns 0–100:

- 40 for `capture_date_confirmed = true`
- 40 for at least one non-disputed, non-deleted face with a `person_id`, **or** `has_no_people = true`
- 20 for at least one row in `photo_places`

`refresh_completeness(photo_id)` writes the result back into `photos.completeness_score`. `refresh_all_completeness()` recomputes for every non-deleted photo (used by batch jobs after a bulk change).
