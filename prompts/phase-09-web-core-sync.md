# Phase 9 — Web: core resources, sync, and contributor uploads

Read `CLAUDE.md`, `PROJECT-PLAN.md` (§2 — especially Privacy, Physical location, Rescans, Contributor uploads, Mobile-first; Phase 9), `shared/SCHEMA.md`, `web/README.md` (Phase 8 auth: `requireUser`, `requireAdmin`, `requireService`, CSRF), and `photo-archive-build-prompts.md` §8. Work in `web/` and `shared/migrations/`; the desktop Sync mode is in `desktop/` (Python). Start with `git pull`.

GOAL: the VM's copy of the archive gets populated from the laptop by the desktop Sync button, exposes the resources the pages (Phase 10) need, accepts contributions (suggestions, comments, likes, face tags, disputes) from family, and accepts photo uploads that stay hidden until George approves them and pulls them back to the laptop. No pages in this phase beyond what is needed to test — JSON routes plus a minimal upload/approval page pair, since those need a browser to verify.

INVIOLABLE on the web side:
- **Private is private.** A photo with `is_private = true` is never served — not its metadata, not its file, not in counts. Sync never sends it; the API refuses it even if a row somehow exists.
- **Facts vs. suggestions.** Contributors write to `suggestions`, `comments`, `likes`, and `faces` (tags). Only an admin action writes to `photos.capture_date`, `faces.person_id` confirmation, `photo_places`, etc., and every such write goes through the suggestion-accept path with an audit row.
- **No real deletes.** Hide, soft-delete, reject — never remove.
- Expiries in SQL; POST for every state change; CSRF on session-authed POSTs; service token for the desktop.

## Storage on the VM
`PHOTO_DIR` (from `.env`) holds `working/` (synced working copies, flat, same names as the laptop), `thumbs/` (320 px), `faces/` (face crops), `backs/`, and `uploads/` (contributions, originals, never modified). Express serves `working/`, `thumbs/`, `faces/`, `backs/` **only** through a route that checks the photo is not private and the requester is signed in — never `express.static` on `PHOTO_DIR`. Set `Cache-Control: private, max-age=86400` on served images.

## Sync (service token)
`Authorization: Bearer SERVICE_TOKEN` on every route under `/sync`. All idempotent; the desktop can re-send anything.

- `POST /sync/photos` — batch of up to 200 photo records: every non-private column the site needs (id, sha256, phash, width/height, mime, is_scan, capture_date + precision + confirmed, exif_taken_at, exif_camera, scan_batch, scan_sequence, source_filename, physical_ref_note, rescan_wanted, description_ai, completeness_score, file_version, triage_status, is_deleted, updated_at). Upsert by id. **Reject with 400 if any record has `is_private = true`** — the desktop must never send one. Returns which ids need their file (`file_version` newer than stored `synced_file_version`, or missing on disk).
- `PUT /sync/photos/:id/file` — multipart working copy; server writes `working/<name>`, generates `thumbs/<id>.jpg`, records `synced_file_version`. `PUT /sync/photos/:id/back` for the back image; `PUT /sync/faces/:id/crop`.
- `POST /sync/people`, `/sync/person_name_variants`, `/sync/relationships`, `/sync/places`, `/sync/photo_places`, `/sync/albums`, `/sync/album_photos`, `/sync/faces` (with embeddings — needed for web-side match suggestions later; exclude embeddings of faces on private photos), `/sync/photo_backs` (transcriptions), `/sync/suggestions` (AI suggestions with source `ai`/`import`), `/sync/photo_masters` (metadata only, no files). Upsert by id. Batches of 500. Soft-deleted rows sync as soft-deleted.
- `GET /sync/pull/confirmed?since=<ts>` — everything an admin confirmed on the web since `since`: accepted suggestions with their payloads and the resulting fact writes, new/edited people and places, comments (for the metadata writer's exclusion list — it needs to *not* write them), likes counts. The desktop applies these to the laptop DB (Phase 13 uses them).
- `GET /sync/pull/contributions?status=approved&pulled=false` and `GET /sync/pull/contributions/:id/files/:file_id` (file bytes), `POST /sync/pull/contributions/:id/pulled` — see Contributions.
- `GET /sync/status` — counts per table, last sync timestamps.

Desktop side (`desktop/src/photoarchive/modes/sync/`): Sync mode with a **Push** button (photos → files → related tables, resumable via `synced_at`/`synced_file_version`, progress + ETA, skips private and junk) and a **Pull** button (confirmed values → laptop DB with audit rows `source='web'`; approved contributions → `D:\Contributed\<uploader>\<contribution_id>\` as an append-only master root — create-only, never overwrite; the masters guard permits creation under `_incoming/` for `contrib`-kind roots and verifies via manifest that nothing existing changed; then triggers ingest for that root with `triage_status` pre-set to `keep` and provenance `uploaded_by`). Manual only, never scheduled.

## Resources (session auth, contributor or admin)
JSON under `/api`, plus server-rendered pages come in Phase 10. Every list is paginated (keyset by id, `limit ≤ 100`) and excludes private and soft-deleted photos.

- `GET /api/photos` — filters: `year`, `decade`, `person_id`, `place_id`, `album_id`, `has_no_date`, `has_untagged_faces`, `low_completeness`, `sort=recent|liked|incomplete`. Returns thumbs URLs, date + precision + confirmed flag, like count, completeness, physical ref.
- `GET /api/photos/:id` — everything: faces (with person, disputed flag), people, comments (non-hidden), place, likes, back(s) with transcription, suggestions **pending on this photo** (so the page can show "someone suggested 1962"), physical reference, rescan_wanted.
- `POST /api/photos/:id/suggestions` — kind date/person/place/description; payload validated per SCHEMA.md; `source='human'`, `user_id`. A date suggestion accepts free text (`"1962"`, `"March 1962"`, `"sometime in the 60s"`) and normalises to `{date, precision}` server-side; reject if unparseable with a helpful message.
- `POST /api/photos/:id/faces` — contributor draws a box: creates `faces` row `source='human'`, `person_id` set **as a suggestion**: the face row is created unassigned and a `suggestions` row kind `person` with `face_id` is created; admin accept assigns it. (Contributors never write `person_id` directly.)
- `POST /api/faces/:id/dispute` — sets `is_disputed`, `disputed_by`, `dispute_note`; audit row; queued for admin.
- `POST /api/photos/:id/comments`, `POST /api/comments/:id/hide` (admin).
- `POST /api/photos/:id/like` — toggle; unique on pair.
- `GET /api/people`, `GET /api/people/:id` (photos, names, relationships), `POST /api/people` (contributor may create; goes live immediately since a person is not a fact about a photo), `POST /api/relationships` → **suggestion** kind `relationship`.
- `GET /api/albums`, `GET /api/albums/:id`.
- `GET /api/people/autocomplete?q=` — display_name + variants, prefix + trigram, ≤ 10 results; used by tagging UI.
- `POST /api/photos/:id/rescan_wanted` (admin) toggle.

## Admin
- `GET /api/admin/suggestions?status=pending&kind=` list; `POST /api/admin/suggestions/:id/accept` — applies the fact: date → `photos.capture_date/precision/confirmed`; person → `faces.person_id` (+ clears dispute if any); place → `photo_places`; description → `photos.description_ai`; relationship → `relationships.confirmed`; classification `no_people` → `photos.has_no_people`. Each writes `audit_log` with previous value and calls `refresh_completeness`. `POST .../reject` with note. **Accepting never overwrites a confirmed fact silently**: if the target is already confirmed with a different value, return 409 with both values; admin must pass `force=true` (still audited).
- `GET /api/admin/disputes`, `POST /api/admin/faces/:id/resolve` (keep / unassign / reassign).
- `GET /api/admin/audit?entity_type=&entity_id=&limit=`.
- `GET /api/admin/report/monthly?month=YYYY-MM` — from `audit_log` and `likes`: per user — logins, tags, comments, likes, suggestions made; admin — suggestions accepted/rejected, dates confirmed.
- `GET /api/admin/rescan-list` — `rescan_wanted` photos grouped by `scan_batch`, ordered by `scan_sequence`; also as a printable page `/admin/rescan-list`.

## Contributions (uploads)
Migration: `contributions` (id, user_id, note, status pending/approved/rejected/partial, created_at, decided_by, decided_at, pulled_at) and `contribution_files` (id, contribution_id, original_filename, stored_path, sha256 unique, size, mime, width, height, exif_taken_at, phash, status pending/approved/rejected, decided_at, duplicate_of_photo_id nullable, duplicate_distance).

- `POST /api/contributions` → creates a pending contribution, returns id.
- `HEAD /api/contributions/:id/files?sha256=` → 204 if the server already holds that sha (anywhere: contributions or photos), 404 otherwise. Clients skip what exists.
- `POST /api/contributions/:id/files` — one file per request, multipart, ≤ 100 MB, JPEG/PNG/HEIC/TIFF (videos accepted, flagged, not processed). Stores under `uploads/<contribution_id>/<file_id>.<ext>`, computes sha256/pHash/EXIF, makes a thumb, checks duplicates (sha256 exact against `photos` and other contributions; pHash ≤ 10 against `photos`), records the result. Returns the file row. Rate-limit per user (e.g. 600/hour) rather than per IP.
- `POST /api/contributions/:id/finish` — marks upload complete; emails `ADMIN_EMAIL` a summary (count, uploader, note, how many flagged as duplicates) with a link to the approval page.
- `GET /api/contributions/mine` — uploader sees their own, with per-file status.
- Admin: `GET /api/admin/contributions?status=pending`, `POST /api/admin/contributions/:id/files/:file_id/approve|reject`, `POST /api/admin/contributions/:id/approve-all|reject-all`. Approval/rejection never deletes; rejected files stay on disk with status rejected.
- **Minimal pages for this phase** (mobile-first, plain JS, full versions come in Phase 10): `/upload` — camera/gallery picker on phones (`<input type=file accept="image/*" multiple>`; a second input with `capture="environment"`), folder picker on desktop (`webkitdirectory`), drag-drop; sequential uploads with per-file progress, retry, and the sha pre-check; note field; shows what's already been sent. `/admin/contributions` — thumbnails, uploader, EXIF date, duplicate badge with a link to the existing photo, approve/reject per file and per batch. Pull-to-laptop happens through desktop Sync.

Uploaded files are visible only to their uploader (`/mine`) and admins until approved **and** pulled and pushed back as a normal photo. There is no shortcut that makes an upload public.

## Tests (`node --test` + supertest on `TEST_DATABASE_URL`)
- Sync: upsert idempotence; 400 on a private record; file version handshake; pull-confirmed since-cursor.
- Privacy: a row inserted directly with `is_private=true` is absent from list, detail, image route, and counts.
- Suggestions: contributor creates; admin accept writes the fact + audit + completeness; 409 on conflicting confirmed value; force works and audits.
- Faces: contributor tag creates suggestion, not fact; dispute; admin resolve.
- Contributions: sha pre-check; duplicate detection against a seeded photo; approval flow; uploader isolation (user A cannot see user B's pending files); rejected file still on disk.
- Report: monthly counts from a seeded audit log.

Desktop: pytest for push resumability (kill mid-batch → resume sends only the rest), private exclusion at the selector, append-only pull into a temp root, guard behaviour on the `contrib` root.

## Verification, then stop
1. `npm test` and desktop `pytest` green.
2. Push the full keep set from the laptop to the local web instance (`PHOTO_DIR` on the laptop for now): report counts per table, elapsed, bytes; second push sends 0 files.
3. Confirm zero private photos on the web side (there are 0 today — mark one private on the laptop, push, confirm it never arrives; unmark).
4. From a phone on the LAN: upload 3 photos through `/upload`; approve 2 on `/admin/contributions`; Pull on the desktop; confirm the two files landed under `D:\Contributed\...`, were ingested with `uploaded_by`, and the rejected one did not move.
5. Suggest a date and a face tag as a contributor account; accept both as admin; confirm the facts, audit rows, and the completeness change.
6. Update `CLAUDE.md`, `shared/SCHEMA.md`, `web/README.md` (API summary). Commit and push: `Phase 9: web core, sync, contributions`.

---

## Answers to Claude Code's questions
(added as they come)
