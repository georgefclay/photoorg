# Phase 9 — Web: core resources, sync, and contributor uploads

Read `CLAUDE.md`, `PROJECT-PLAN.md` (§2 — especially Privacy, Physical location, Rescans, Contributor uploads, Mobile-first; Phase 9), `shared/SCHEMA.md`, `web/README.md` (Phase 8 auth: `requireUser`, `requireAdmin`, `requireService`, CSRF), and `photo-archive-build-prompts.md` §8. Work in `web/` and `shared/migrations/`; the desktop Sync mode is in `desktop/` (Python). Start with `git pull`.

GOAL: the VM's copy of the archive gets populated from the laptop by the desktop Sync button, exposes the resources the pages (Phase 10) need, accepts contributions (suggestions, comments, likes, face tags, disputes) from family, and accepts photo uploads that stay hidden until George approves them and pulls them back to the laptop. No pages in this phase beyond what is needed to test — JSON routes plus a minimal upload/approval page pair, since those need a browser to verify.

INVIOLABLE on the web side:
- **Private is private.** A photo with `is_private = true` is never served — not its metadata, not its file, not in counts. Sync never sends it; the API refuses it even if a row somehow exists.
- **Facts vs. suggestions.** Contributors write to `suggestions`, `comments`, `likes`, and `faces` (tags). Only an admin action writes to `photos.capture_date`, `faces.person_id` confirmation, `photo_places`, etc., and every such write goes through the suggestion-accept path with an audit row.
- **No real deletes.** Hide, soft-delete, reject — never remove.
- Expiries in SQL; POST for every state change; CSRF on session-authed POSTs; service token for the desktop.

## Groups — the access model (read `PROJECT-PLAN.md` §2 "Groups")
Build this first; every route below depends on it.

Migration: `groups` (id, name unique, description, created_by, created_at, is_deleted); `group_members` (group_id, user_id, role `member`|`moderator`, added_by, added_at; PK on the pair); `photo_groups` (photo_id, group_id, added_by, added_at; PK on the pair). Add `contributions.group_ids bigint[]` (target groups chosen by the uploader). Soft deletes only.

Visibility rule, implemented once as SQL fragments/helpers and used **everywhere** a photo, face, back, thumbnail, comment, like, suggestion, person-page photo list, search result, or count is produced:
- admin → all non-private, non-deleted photos;
- everyone else → photos that share at least one group with the user (`exists (select 1 from photo_groups pg join group_members gm using (group_id) where pg.photo_id = photos.id and gm.user_id = $me)`);
- photos with no group are visible to admins only;
- `is_private` still wins over everything.
Tests must prove a non-member gets 404 (not 403 — don't confirm existence) for detail, image, thumb, back, faces, comments, and that lists/counts/search never include invisible photos.

Roles: `requireAdmin` (exists); `requireModerator(groupId)` = admin or `group_members.role='moderator'` for that group.

Moderator endpoints (scoped to their group, audited): hide/unhide a comment on a photo visible in their group; remove a photo from their group (`photo_groups` row soft-removed; if the photo has no groups left it becomes unfiled — never a site-wide delete); add/remove members of their group (never role changes); approve/reject contribution files whose target groups include theirs (approval assigns those groups).

Admin endpoints: group CRUD; membership and role changes; bulk assign/unassign photos to groups by filter (album, scan_batch, person, year range, source folder, id list); `GET /api/admin/unfiled` (paginated); approval of access requests now also takes initial group ids (Phase 8's approve page gets a group multi-select).

Sync: `POST /sync/photo_groups` (desktop → web, upsert/soft-remove); `GET /sync/pull/groups` (groups + memberships + photo_groups changed since cursor, web → desktop). Conflicts: last-writer-wins by `updated_at`, audited.

Desktop: a **Groups** section in Sync mode (or its own mode): pull groups; bulk assign/unassign photos to groups by album, batch, person, year, source folder, or the current grid selection; show per-group counts and the unfiled count; push. This is how the 12,800 existing photos get filed.

Uploads: the uploader picks one or more of **their own** groups (server validates membership); the contribution and its files carry `group_ids`; approval assigns them.

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
- Groups: non-member 404s on every photo-scoped route; member sees; admin sees unfiled; moderator can remove-from-group but not from another group; moderator approve limited to own group; bulk assign by album/batch/person/year; sync round-trip of photo_groups both directions.
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
6. Update `CLAUDE.md`, `shared/SCHEMA.md`, `web/README.md` (API summary). Commit and push: `Phase 9: web core, groups, sync, contributions`.

---

## Answers to Claude Code's questions

1. **Join-table sync columns.** Yes: `updated_at` (trigger), `is_deleted`, `deleted_at`, `deleted_by` on `group_members` and `photo_groups`. Remove = soft-delete the row. Never hard-delete-and-recreate.
2. **contributions tables.** (b) extend via ALTER. Phase 8 doesn't depend on them, but drop-and-recreate on a production-bound migration path is a habit we don't want. Add what's missing; rename nothing.
3. **group_ids array.** App-enforced validation (every id exists, not deleted, uploader is a member). Fine.
4. **Embeddings.** Gate behind `SYNC_FACE_EMBEDDINGS=false` in `desktop/.env` for now. Always push bbox / person_id / disputed / review_status; embeddings only when the flag is on. Column stays nullable on the VM.
5. **Visibility everywhere.** Confirmed: people, albums, places pages and every count on them. If a contributor can't see a photo, it doesn't exist for them anywhere on the site.
6. **Moderator approval.** The fair one: a moderator's approval assigns **only their own group**; other targeted groups stay pending for their own moderators or an admin. A file becomes `approved` (and pullable) once any target group has approved it; later approvals just add `photo_groups` rows through the normal web → desktop pull. A moderator's reject removes only their group from the targets; the file is `rejected` only when no target group remains.
7. **Bulk assign.** Synchronous, one transaction, cap 20,000 rows per call, return counts.
8. **pull/confirmed comments.** Minimal: `{photo_id, comment_count, has_hidden}`. Bodies stay web-authoritative.
9. **Laptop-side PHOTO_DIR.** Yes, `C:\Photos-web-local\`. Note it in `GC.md`.
10. **Contrib root.** (a) One root: `contrib=D:\Contributed|contrib`, per-uploader subfolders inside. (b) Guard: under a `contrib`-kind root, creation is permitted only inside `<root>\_incoming\<contribution_id>\`; after that contribution is ingested the folder is renamed to `<root>\<uploader>\<contribution_id>\`, and the manifest check covers everything outside `_incoming`. Nothing may ever modify or delete an existing file under the root.
11. **Duplicates.** (c) badge, admin/moderator chooses. Approving a sha256-exact duplicate records it and creates no new photo; a pHash-near duplicate becomes a normal contribution file and laptop-side dedupe/rescan decides.
12. **Rate limits.** 300/hour per user combined on suggestions/comments/likes/tags; 600/hour on uploads; admins exempt.
13. **Suggester identity.** Display name to admins and to moderators of a group the photo is in; "someone" to everyone else. Never the email.
14. **Minimal pages.** Confirmed — functional, mobile-safe, no polish. Phase 10 replaces them.
15. **Real Express in pytest.** Yes, subprocess against `TEST_DATABASE_URL`; skip with a clear message if `node` or web deps are missing.

GO.

---

## Progress

- [x] Migration `phase-9-groups` — groups + group_members + photo_groups with soft-delete + updated_at trigger for LWW sync.
- [x] Migration `phase-9-contributions` — contributions + contribution_files with group_ids, sha256/phash dedupe fields, approved_group_ids per file, is_video flag.
- [x] Visibility helpers (`web/middleware/visibility.js`) — `photoVisibleSql`, `photoVisibleWhere`, `assertPhotoVisible`, `isModerator`, `userGroupIds`, `userModeratorGroupIds`. Admin sees all non-private non-deleted (including unfiled); contributor needs a live shared group; is_private / is_deleted always exclude. `requireModerator` middleware.
- [x] Image-serving route `/media/{thumbs,working,backs,faces}/:id` — session-gated, visibility-checked, Cache-Control: private max-age=86400, 404 (never 403) on non-membership.
- [x] Web tests 28/28 green (6 new visibility tests, existing auth tests untouched).
- [x] Read APIs: `/api/photos` list (year/decade/person/place/album/has_no_date/has_untagged_faces/low_completeness filters; recent/liked/incomplete sorts; keyset paginated) + detail (faces, comments, place, likes, backs, pending suggestions, physical ref, rescan_wanted; suggester identity shown to admins + group moderators only). `/api/people` list + detail + autocomplete + create (contributor). `/api/albums` list + detail. `/api/relationships` POST → suggestion. `POST /api/photos/:id/rescan_wanted` (admin).
- [x] CSRF middleware also accepts `X-CSRF-Token` header (`_csrf` form field still works). `GET /api/csrf` exposes the token to JS clients.
- [x] Web tests 35/35 green (7 new API read tests).
- [x] Contributor writes: `POST /api/photos/:id/{suggestions,faces,comments,like}` and `POST /api/faces/:id/dispute`. Date parser handles "1962" / "March 1962" / "1962-03" / "sometime in the 60s" / etc. Face-tag creates unassigned face + person suggestion (admin acceptance sets the person). 300/hour per-user rate limit; admins exempt.
- [x] Moderator write: `POST /api/comments/:id/{hide,unhide}` — admin OR moderator of any group the photo is in.
- [x] Admin API: `GET/POST /api/admin/suggestions/*` with 409-on-confirmed + force override + refresh_completeness on accept; date/person/place/description/relationship/classification(no_people) accept paths. Disputes list + resolve (keep/unassign/reassign). Audit browser. Monthly report. Rescan list. Unfiled queue.
- [x] Web tests 44/44 green (9 new write tests).
- [x] Groups routes: `GET /api/groups` (mine, with counts + my_role) + `GET /api/groups/:id`. Admin CRUD (`POST/PATCH/POST-delete`). Membership add/remove/role-change (admin — any role; moderator — member only). Moderator `POST /api/groups/:groupId/photos/:photoId/remove`. Bulk assign at `POST /api/admin/photos/bulk-assign-groups` (synchronous, cap 20 000, one transaction, filters: album_id / scan_batch / person_id / year / decade / source_folder / ids; adds and removes both).
- [x] Web tests 48/48 green (4 new groups tests).
- [x] Sync endpoints under `/sync` (service token). `POST /sync/photos` (batch 200, 400-on-private, `need_files` handshake driven by `synced_file_version`); `PUT /sync/photos/:id/file` (writes `working/`, generates 320px thumb via sharp, records `synced_file_version`); `PUT /sync/photo_backs/:id/file`; `PUT /sync/faces/:id/crop`. Batched metadata upserts for `photo_masters` / `people` / `person_name_variants` / `relationships` / `places` / `photo_places` / `albums` / `album_photos` / `faces` (embeddings optional; refuses to touch faces on private photos as a belt-and-braces) / `photo_backs` / `suggestions`. `POST /sync/photo_groups` with LWW-by-updated_at + soft-delete round-trip. `GET /sync/pull/{groups,confirmed,contributions}` since-cursor pulls; `GET /sync/pull/contributions/:id/files/:file_id` returns file bytes; `POST /sync/pull/contributions/:id/pulled`. `GET /sync/status`.
- [x] Filesystem helpers in `web/services/photo-storage.js`: PHOTO_DIR/{working,thumbs,backs,faces,uploads}, working basename `<id:08>_<sha8>.<ext>`, thumb `<id:08>.jpg` at 320px, and upload staging under `uploads/<contribution_id>/`.
- [x] Web tests 55/55 green (7 new sync tests: token gate, upsert idempotence, 400-on-private, file write + thumb + version handshake, file_version bump re-requests, photo_groups LWW + soft-delete, pull/confirmed since-cursor).
- [x] Pre-contributions guard-rails: `photo-back-orphan` migration `down` now aborts with a clear message when orphan backs exist (never deletes them); added a visibility test for a non-admin getting 404 on `/media/*` for an unfiled photo. Total 56/56.
- [x] Contributions API: `POST /api/contributions`, `HEAD /api/contributions/:id/files?sha256=`, `POST /api/contributions/:id/files` (multipart ≤100 MB, sha256 + pHash + EXIF + duplicate flag, 600/hour per user), `POST /api/contributions/:id/finish` (emails ADMIN_EMAIL via `admin-contribution` template), `GET /api/contributions/mine`. Admin/moderator queue + per-file approve/reject + bulk approve-all/reject-all with per-answer-#6 moderator scoping (approval assigns only the moderator's group; reject removes only their group from targets — file becomes rejected only when no target remains).
- [x] Minimal EJS pages: `/upload` (mobile-first — gallery/camera/webkitdirectory, sequential per-file upload with sha pre-check + progress + retry, group multi-select limited to uploader's groups, note field). `/admin/contributions` (thumbnails, dup badge, per-file + batch actions). Phase 10 replaces both.
- [x] Web tests 61/61 green (5 new contributions tests: create + upload + finish + email, sha256 exact dedupe, uploader isolation, approve→pull→rejected-on-disk, `/sync/pull/contributions/:id/files/:file_id` streams bytes).
- [x] Desktop `MASTER_ROOTS` gains `|contrib` kind; masters guard now skips `_incoming/` when picking a probe subfolder so an in-progress pull doesn't flag the root as writable, while a committed uploader subfolder still must be read-only. `desktop/.env.example` documents the new kind and adds `SYNC_FACE_EMBEDDINGS=false`.
- [x] Desktop Sync mode (`desktop/src/photoarchive/modes/sync/`): `client.py` (bearer-token HTTP wrapper), `push.py` (photos → files → 10 batched metadata tables → photo_groups; private/junk excluded at selector; resumable via `synced_at` + `synced_file_version`; face embeddings gated by `SYNC_FACE_EMBEDDINGS`), `pull.py` (groups + memberships + photo_groups LWW; confirmed values as `source='web'` audit rows; contributions into `<contrib>/_incoming/<cid>/` with pre/post `ContribManifest` invariant check, then rename to `<uploader>/<cid>/`), `bulk.py` (local resolve+apply for scan_batch/album/person/year/decade/source_folder, plus unfiled count + group summary), `__init__.py` PySide6 panel with Push / Pull / Groups tabs, background worker thread, status bar.
- [x] Desktop pytest 188/188 passing (1 skipped). New: `test_sync_guard.py` (3 — contrib probes skip `_incoming/`, contrib config parse), `test_sync_pull_contrib.py` (3 — `snapshot_contrib_root` ignores `_incoming/`, equal manifests after staging-only writes, differs when a committed file is touched), `test_sync_bulk.py` (3 — resolve_by_scan_batch + bulk_apply, remove soft-deletes, unfiled excludes grouped), `test_sync_push_resume.py` (1 — real Express subprocess against TEST_DATABASE_URL; first push writes 3 non-private non-junk metadata rows + 3 files, second push writes 3 metadata rows + 0 files).
- [x] Verification setup (2026-09-14):
  - `D:\Contributed\` created; `_incoming\` under it; icacls deny on the root only (no `(OI)(CI)` — inherited deny would break the pull writer). `_incoming/` inherits the parent's allow via `Authenticated Users:(I)(M)` and stays writable. `contrib=D:\Contributed|contrib` added to `desktop/.env` `MASTER_ROOTS`.
  - `web/.env` `PHOTO_DIR=D:\Photos-web-local\` (originally C:\ per answer #9; moved to D:\ after the first push exhausted C: — same lesson as Phase 2's `WORKING_DIR` move); shared `SERVICE_TOKEN` == `desktop/.env` `WEB_API_TOKEN` (`NfgcKtxtFww2pMQ52Nk0o59UhUut8SbhdrLoTd9WibQ` — dev only). Recorded in `GC.md`.
  - Web app running on `http://localhost:8090` and reachable at `http://192.168.50.95:8090` via Windows Firewall rule `PhotoArchive-8090-Private` (Private profile only).
  - `Clay Family` group (id=1) created; `georgefclay@gmail.com` = moderator, `test-contributor@example.com` = member. Magic link for the test contributor recorded in the setup checkpoint.
  - Cosmetic push fix-up: `photo_files` progress `done` now bounded by the per-batch length instead of the running total (was displaying 866/200 etc).
- [x] Verification steps 2 and 3 executed on the laptop:
  - **Step 2 — full Push**. First push (embeddings off): 13 257 photos upserted, **12 602 files uploaded**, **27.29 GB** transferred, elapsed **24 min 10 s**. Per-table: photo_masters 13 501, people 109, albums 6, album_photos 253, faces 23 461, photo_backs 826, suggestions 5 433, photo_groups 0. Second push (metadata-only): **0 files**, 0 bytes, 42.6 s — file-version handshake confirmed. Note: partway through the first attempt the C: drive filled at ~4600 files (`ENOSPC` on file 8304); `PHOTO_DIR` moved to `D:\Photos-web-local\` (D: has 2 TB free), C: partial delete performed, push restarted from scratch and completed cleanly. Missing-working-file warnings (655) are laptop-side pointer drift, not a Sync bug — recorded for a future `check_working_files` pass.
  - **Step 3 — private-photo test** (`tools/verify_private.py`, photo id=1): baseline files present; after `is_private=true`, a full push's wire batches (n=13 256) do **not** include id=1; `POST /sync/photos` synthesised with `is_private=true` returns 400; `/media/thumbs/1` returns 404 to an admin session; `/api/photos` list omits it; after unmark, a push re-includes it and files remain on the web. **VERIFY-PRIVATE: PASS**.
  - Verification scripts committed under `desktop/tools/`: `run_push.py`, `verify_private.py`.


## Phase 9 fix-up 3 — Groups panel: assign by people, in priority order (2026-11-05)

George starts the group bulk-assignment (PROJECT-PLAN §5 item 6) and his
workflow is a cascade of person rules: *"photos with any of THESE people go to
Arbuckle; then photos with any of THOSE people go to Clay Family — unless a
photo is already in Arbuckle."* The Sync → Groups tab cannot say that today:
one numeric `person_id` at a time, no way to keep already-grouped photos out
of a later rule, and silent zeros on a miss. Read `sync/bulk.py` and the
Groups tab in `modes/sync/__init__.py`, and PROJECT-PLAN §5 items 21 and 24.
Work in `desktop/`. Start with `git pull`. Run before George assigns anything.

### A. The Groups tab
1. **A people picker: two lists side by side** (George's design,
   2026-11-05). The left list holds every live person, sorted by display
   name, with a search box above it to narrow; each row shows the name, its
   nicknames, and how many photos carry that person's tagged face, and
   identically named people (the father/son case, #2608) are told apart by
   the id on the row. **Double-click moves a name to the right list;
   double-click in the right list sends it back.** The right list is the
   rule: Assign takes every photo with a live, undisputed face of ANY person
   in the right list — the same face clause `resolve_photo_ids` already
   uses. After a successful apply the right list clears, ready for the next
   rule in the cascade; the group dropdown, "only unfiled" checkbox and
   preview below apply to the picker exactly as to the other filters.
2. New filter kind **unfiled** (value box disabled): the backend's existing
   `unfiled_ids()`.
3. **"Only unfiled" checkbox, default checked, applying to every Assign**:
   restrict the resolved photos to those in no live group. This is what makes
   the cascade "first rule wins" — George runs his rules in priority order.
   Unassign ignores the checkbox.
4. **Preview, then apply**: Assign first shows "N match the filter; M after
   the only-unfiled restriction → add M to <group>?" with OK / Cancel. An
   empty result says plainly "no photos match <what was searched>" — never a
   silent 0 (item 21's lesson).
5. `scan_batch` keeps exact match, but an all-digits value gets `Batch `
   prefixed first, and the message says what was searched.
6. `resolve_photo_ids` adds `is_private = false` (item 24a — `unfiled_ids`
   beside it already has it).
7. The `photo_group.bulk` audit row gains the filter that produced it: kind,
   value (for people: the names given and the resolved person ids),
   only_unfiled — so any rule can be reconstructed later.

### B. Item 21 while you are in there
The Metadata panel's batch box and the CLI `--batch` get the same all-digits
`Batch ` prefix and a "no photos in batch '…'" message instead of an
ordinary-looking "0 due".

### C. One-line housekeeping
`job_runs` row 48 — the metadata write killed for low memory on 2026-11-05 —
still says `running`; mark it `failed` the way row 42 was.

### Tests
The picker lists every live person with a photo count and the search box
narrows it; identically named people are distinguishable; double-click moves
a name both ways; the rule matches photos of any person in the right list and
skips disputed and deleted faces; the right list clears after an apply; only-
unfiled restricts an Assign and leaves Unassign alone; private photos never
resolve (24a); a bare batch number matches its batch; the unfiled kind equals
`unfiled_ids()`; preview counts equal what apply then does; the audit carries
the filter.

### Verification, then stop
1. `pytest` green.
2. On the live DB, previews only — assign nothing: paste the counts for one
   real people rule George can sanity-check (two or three well-tagged people in the right list),
   the unfiled count, and a bare-number batch.
3. One tiny round trip: assign a single photo to Clay Family through the new
   preview, show the `photo_groups` row and the audit row, then Unassign it
   (before any push, so no file is uploaded).
4. `PROJECT-PLAN.md` (§5 items 21 and 24 closed, Progress entry), `CLAUDE.md`
   (Groups tab section), commit: `Phase 9 fix-up 3: people-cascade bulk
   assign`.

This fix-up is pre-approved — **GO** — but if anything above does not match
the code's reality, append questions under `### Questions (fix-up 3)` here
and stop instead.

## Phase 9 fix-up 4 — Sync tab: one Pull button, plain results (2026-11-10)

**DEFERRED to Phase 16 (George, 2026-11-10) — do not build this as a fix-up.** George wants the Pull rework done inside the full desktop UI rework rather than piecemeal. The text below stays as the spec and is collected in `prompts/phase-16-desktop-ui.md`. Phase 9 fix-up 3 above is still live and runs next.

George's verdict on the Pull tab, after the Phase 13 fix-up 2 live loop: the
UI is horrible. He accepted a description on the site, went to Sync → Pull,
pressed **Pull approved contributions** (the button for new uploads) and got
`done: {'contributions': 0, 'files': 0}`. That was true, but useless: the
accept he was waiting for comes down through a *different* button,
**Pull confirmed values**, which Claude Code then ran and which applied 8
facts. Two faults: (1) three pull buttons whose names do not say what
George is waiting for, so he has to know the sync internals to pick one;
(2) results printed as raw Python dicts (`_on_job_finished` appends
`f"done: {result}"`, and the status line says the same). Read the Pull tab
and `_on_job_finished` in `modes/sync/__init__.py`, and `pull_groups`,
`pull_confirmed`, `pull_contributions` in `modes/sync/pull.py`. Work in
`desktop/`. Start with `git pull`. Desktop only — no web change, no deploy.

### A. One button
1. **"Pull from the site"** replaces the three as the main action. It runs
   the same sequence a push begins with, plus contributions: `pull_groups`,
   then `pull_confirmed` (which already runs `pull_web_origin` and
   `pull_web_edits` first), then `pull_contributions` when a `|contrib` root
   is configured. One job slot, same `_run_worker`, same
   `with_metadata_due` count at the end.
2. The three single-purpose buttons move under a collapsed **Advanced**
   section on the same tab (they are still useful when one stage fails), and
   their labels say what they bring: *Groups and memberships*, *Accepted
   suggestions and edits*, *New uploads (contributions)*.
3. A stage that raises does not stop the others (the push's
   `failed_stages` rule): the summary names what failed and why, the rest
   still run.

### B. Plain results
1. **No dict reaches the screen.** `_on_job_finished` / the status line
   render a sentence per job type; the raw dict may go to the log file.
2. For a pull, the sentence says what came down **by kind**, e.g.
   *"Brought down 8 changes from the site: 1 description, 6 person tags,
   1 place. No new uploads. 4 photos need their metadata re-written."*
   with zeros said plainly (*"Nothing new on the site."*). That needs
   `pull_confirmed` to return a per-action breakdown instead of one int
   (a `Counter` keyed by audit action, mapped to words: `photo.description.set`
   / `.edit` → description, `face.assign` → person tag, `photo.place.set` →
   place, `photo.capture_date.set` / `.unconfirm` → date, …), plus the
   counts `pull_web_origin` and `pull_web_edits` already compute. Keep the
   int available to existing callers (`run_push`, tests) or update them.
3. The existing **Write metadata for N changed photos** button stays where
   it is; the sentence ends by pointing at it when N > 0. **It never just
   disappears**: today `_show_metadata_due` hides the button at 0, and George
   went looking for it (2026-11-10) with no way to tell "nothing to do" from
   "broken". At 0 the line says *"All photo files are up to date."*; it also
   shows on opening the Sync mode (the due count is 1.3 s, off the GUI thread
   as now), not only after a pull or push.
4. Push gets the same treatment: *"Sent 13,294 photos' details and 5 files
   (8.7 MB). Pulled first: 9 changes (…)."* instead of `done: {...}`.

### C. Housekeeping
Phase 9 fix-up 3's item C (`job_runs` 48 → `failed`) is already done
(2026-11-10, with row 52 too); drop it from that fix-up if it has not run.

### Tests
One button runs all three stages in order and a failing stage does not stop
the rest; the summary sentence for: nothing new, a mixed batch (description +
person tags + place), a failed stage, new contributions; no `{` / `}` dict
text in the status line or log widget after any job; `pull_confirmed`'s
breakdown matches the audit actions applied; the metadata-due sentence
appears when the pull made photos due. GUI tests follow the existing
`@Slot` / job-slot rules (CLAUDE.md, Phase 7 "Two UI rules").

### Verification, then stop
1. `pytest` green.
2. On the live site: George makes one small change (accept a suggestion or
   edit a description), then presses **Pull from the site** — paste the
   sentence it shows. Then a second press — paste "Nothing new on the site."
3. `CLAUDE.md` (Sync tab), `PROJECT-PLAN.md` (Progress entry), commit:
   `Phase 9 fix-up 4: one Pull button, plain results`.

**Not yet approved** — George reviews this with Cowork and adds GO.
