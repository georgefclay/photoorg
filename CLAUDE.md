# Standing rules for this repo

Read before doing anything.

## What this project is
Family photo archive. Monorepo: `desktop/` (Python + PySide6 + local Postgres),
`inference/` (FastAPI on the Mac mini), `web/` (Express + EJS on an AWS VM),
`shared/` (Postgres migrations, nickname seed, API contract). Same schema on
laptop and VM. See `PROJECT-PLAN.md` for the current state; `photo-archive-build-prompts.md`
for the original 12-phase spec; `prompts/phase-NN-*.md` for the one-per-phase
prompts (add answers to the prompt file's `## Answers` section, wait for "go").

## Inviolable
- **Masters are read-only.** `D:\Photos` and `D:\Scanned Photos` are never
  written by any code in this repo. Working copies live under `working/`.
  Ingest must refuse to run if it can write to a master.
- **No real deletes, ever.** Soft-delete flags (`is_deleted`, `deleted_at`) and
  quarantine directories (`quarantine_path`). Restores must be possible.
- **Facts vs. suggestions.** AI output and family input go to `suggestions`
  with `status='pending'`. Only an admin promoting a suggestion writes to the
  fact columns (`photos.capture_date`, `faces.person_id`, `photo_places`, …).
  Nothing else writes AI values to fact columns.
- **Private is private.** `is_private` photos are excluded from sync (skipped
  by push, absent from the VM's disk and DB). The web API refuses to serve
  them even if they somehow arrive.

## Database
- Local DB `photoorg`, role `photo_user`. Test DB `photoorg_test` (separate,
  never equal to `DATABASE_URL`; created once with `createdb -U postgres -O
  photo_user photoorg_test`). Passwords in `shared/.env` (gitignored).
- Migrations live **only** in `shared/migrations/`. Same files run on the VM.
  Create with `npm run migrate:create -- <name>`. Both `up` and `down` must be
  complete.
- **Expiries are computed in SQL, never in Node**: `expires_at = now() +
  interval '15 minutes'`. Applies to magic links, access-request tokens,
  anything time-bounded.

## Desktop (Phase 2 onwards)
- **Master roots are a list**, not two fields. `MASTER_ROOTS=label=path[|kind];…`
  in `desktop/.env`. Labels match `[a-z0-9_]+`, are unique, and become
  `photos.source_root`. `kind` defaults to `digital`; `|scan` opts a root into
  scan-only handling (scan_batch/scan_sequence, back detection, rescan
  detection, folder-name album). Add roots without touching code.
- **Working-name scheme:** `WORKING_DIR/{photo_id:08d}_{sha256[:8]}.{ext}`
  (flat, no subfolders). Thumbnails: `THUMBS_DIR/{photo_id:08d}.jpg`.
  Held (unaccepted) proposals stage under `WORKING_DIR/_staging/{sha256}.{ext}`
  and `THUMBS_DIR/_staging/{sha256}.jpg` until accepted.
- **Staging tables (migration 12):** `ingest_pairings` and `ingest_rescans`
  hold proposed backs / rescans until George reviews. Ingest never writes to
  `photo_backs` or promotes rescans directly. `ingest_failures` records files
  that failed ingest (unreadable, undecodable) because `job_items` requires a
  non-null `photo_id`.
- **Masters guard:** at ingest start, ingest attempts to write a probe file to
  every master root and one random subfolder. Any writable root → refuse to
  run and print the `icacls` deny command. The status bar shows a red WRITABLE
  warning even when ingest isn't running. `attrib +R` on a directory is
  advisory and does NOT block writes; use icacls (see `GC.md`).
- **Videos:** whitelisted in the extension list but log-and-skip until the
  first video actually appears. The full video code path is deferred.

## Triage (Phase 3 onwards)
- **Four states:** `triage_status` is one of `untriaged | keep | junk | private`.
  Every transition writes an `audit_log` row (`actor='desktop'`,
  `action='triage.decision'`) with previous and new state, plus the hint that
  the reviewer saw.
- **Post-condition invariants** (all enforced by
  `modes/triage/decisions.apply_decision`):
  - `junk`: file at `quarantine_path`, `working_path=NULL`, `is_deleted=true`,
    `deleted_at=now()`. Restorable — this is the only thing junk means.
  - `keep`/`private`/`untriaged`: file at `working_path`, `quarantine_path=NULL`,
    `is_deleted=false`, `deleted_at=NULL`. `is_private` is only true for
    `private`.
- **Quarantine path:** `QUARANTINE_DIR/{photo_id:08d}_{sha256[:8]}.{ext}`, flat
  (same scheme as working). `QUARANTINE_DIR` is set in `desktop/.env`.
- **Thumbnails stay put** at `THUMBS_DIR/{photo_id:08d}.jpg` regardless of
  triage state, so the quarantine browser stays cheap.
- **File moves happen AFTER the DB commit.** DB is the source of truth. A
  move failure logs and leaves the DB row as-decided; the quarantine browser
  is where mismatches get reconciled.
- **Hints are hints.** `triage_hints` is written by the `triage_presort` job
  and read by the UI to pick the default decision key. Hints never make a
  decision on their own; only a keypress does.

## Dedupe (Phase 4 onwards)
- **Scope:** operates on `triage_status in ('keep','private')` and
  `is_deleted=false`. Private photos participate in dedupe; junk does not.
  Back-shaped photos are excluded — any photo with a `possible_back`
  triage hint or any `ingest_pairings.back_photo_id` row (regardless of
  pairing status) is skipped. Near-blank scans cluster falsely otherwise.
  Photos with `photo_backs` rows attached (they are fronts of a scanned
  back) are NOT excluded — they are real photos.
- **Detection:** 256-bit pHash and dHash (16×16) via multi-index Hamming
  search — 16 bands of 16 bits, guaranteed exhaustive up to distance 15.
  Thresholds `DEDUPE_PHASH_MAX` / `DEDUPE_DHASH_MAX` in `desktop/.env`
  (default 10). Above 15 the scan falls back to brute force. Rotation and
  mirror variants (7 total transforms) are hashed from the thumbnail at
  scan time.
- **Grouping tables (migration 20):** `dedupe_groups` (status pending /
  resolved / not_duplicates), `dedupe_members` (per photo distance,
  transform, keeper flag, keeper_reason), `dedupe_exclusions` (pairwise,
  stored `(least, greatest)`, unique). Pending groups are re-built by
  `dedupe_scan`; resolved and not_duplicates groups are left alone. At
  most one pending group per photo is enforced procedurally by the
  orchestrator (delete-and-rebuild).
- **Keeper scoring** (in strict tuple order, lower id last):
  EXIF DateTimeOriginal + camera > TIFF > pixels > file size > scan
  over digital when neither has EXIF. Reason chain shown in the UI.
- **Resolve rules** (`modes/dedupe/resolve.py`):
  * losers go through `triage.apply_decision('junk',
    hint='dedupe_loser_of <keeper>')` — a normal triage transition with a
    dedupe-tagged audit row;
  * a loser's scan-locator is appended to the keeper's
    `physical_ref_note` (`| ` separator) when the loser is a scan and
    the keeper is not — physical references are never lost;
  * `photo_backs` re-pointed; `photo_masters` re-parented as
    non-preferred (keeper's preferred master and `photos.sha256`
    untouched); album memberships moved (skip if the keeper is already
    in the album); suggestions moved (skip identical
    `(kind, source, payload)`);
  * if any group member is private, the keeper becomes `is_private=true`.
- **Undo (Z, session-only):** reverses every carry-over from the
  `dedupe.resolve` audit row's `new_value`, then restores losers via
  `triage.apply_decision(prior_status)`. Cross-restart undo is by the
  quarantine browser.
- **Not duplicates (N):** inserts `(least, greatest)` exclusions for every
  pair in the group; scan honours them forever.

## Inference client / jobs (Phase 6 onwards)
- **`INFERENCE_URL` / `INFERENCE_TOKEN`** in `desktop/.env`. The single-image
  endpoints, `/health`, and the unattended-batch surface
  (`/batch/upload/{job_name}`, `POST /batch/{endpoint}` with `from_inbox`,
  `GET /batch/results/{job_name}?after=<cursor>`, `/summary`, sweep,
  cancel) all live behind `inference_client.LanInferenceClient`.
- **Client-side downscale + JPEG Q85** before every upload. Per-endpoint
  edge: `classify`/`describe`/`estimate-date` at 1024, `transcribe-back` and
  `detect-faces` at 1536. The multipart filename stem is the `ref` (photo id,
  or `b<photo_backs.id>` for backs). 401 is fatal; connection errors and 503
  retry with exponential backoff.
- **Hand over, then collect.** The batch runner (`jobs/`) selects eligible
  items, uploads in chunks of 50 with progress, then POSTs
  `/batch/{endpoint}` with `from_inbox` — laptop can be closed after that.
  Collect polls every 5 minutes (and on app start): reads NDJSON from the
  mini's per-job results file after the stored cursor, applies the writer
  once per line (idempotent), advances `job_cursors.line_no`, updates
  `photo_job_status`, then sweeps `?done=true`.
- **Selectors gate on `photo_job_status`, not on presence of downstream
  rows.** A legitimately zero-face photo has no `faces` rows — but its
  detect_faces status is 'done', so it isn't re-processed.
- **Blackout lives on the mini** (Phase 5 follow-up). Laptop has no
  blackout logic; the Jobs panel just reads `/health.blackout`.
- **Queue order:** `transcribe_backs → detect_faces → classify → describe →
  estimate_date`. Hand-overs run sequentially in that order.
- **Everything the models produce is a suggestion.** Writers insert
  `suggestions` rows with `source='ai'`, `model`, and `prompt_version`.
  Fact columns (`photos.capture_date`, `photos.description_ai`,
  `faces.person_id`, `photo_places`) are only written by an admin
  promoting a suggestion — or by George's own decisions in the Faces mode
  (which count as admin actions and get audit rows tagged `source='human'`).
  The two facts the writers set directly are observational and low-risk:
  `photo_backs.transcribed_text/transcription_confidence` (with
  `transcription_confirmed=false`) and `faces` rows themselves.
- **transcribe_backs low-confidence retry (< 0.5)** re-runs the flipped
  and rot180 variants **inline via single-image calls**, not through the
  batch queue. Keeps the writer's state machine simple; the retry rate is
  small.
- **classify → `back_of_print` on a scan** inserts a pending
  `ingest_pairings` row exactly like the B key: `back_photo_id` = this
  photo, `back_score` = confidence, `front_photo_id` = immediate
  predecessor by `scan_sequence` (unless the predecessor is itself a
  back / pending back → orphan), `details.source='ai_classify'`.
- **AI-junk hint precedence: presort wins.** If a photo already has a
  `triage_hints` row from the presort job, the AI's label goes into
  `details.also.ai_classify`; the hint stays as-is. Only photos with no
  presort hint get `hint='ai_junk'`.
- **Private photos are sent to the mini.** LAN-only, no external egress;
  the "private is private" rule is about the web VM. Sweep removes the
  inbox copy once the result is in the DB.

## Faces mode (Phase 6)
- **Clustering is in-memory**, scipy **average-linkage** cosine distance
  (fix-up 2 — single linkage chained ~3,500 faces into one blob),
  threshold `FACE_CLUSTER_DIST` (default 0.45). Recomputed on demand from
  the button. Never stored in the DB.
- **Quality gate before clustering AND before reference-set means.** Faces
  with `det_score < FACE_MIN_SCORE` (0.7) or bbox short-edge <
  `FACE_MIN_PX` (40) are excluded; rows kept, surfaced via the
  "Include low-quality" toggle. Low-quality faces poison reference means
  too, so the same gate applies there.
- **Recursive split** for any cluster larger than `FACE_MAX_CLUSTER`
  (300): re-cluster the members at threshold × 0.8, iteratively (cap 5
  levels). Sub-clusters are tagged "split from a larger cluster" in the
  header.
- **Queue order**: big first, but clusters with < 3 faces push to the
  back — the meaty ones get handled first; singletons are the long tail.
- **Cluster grid ordering**: within each cluster, faces sort by cosine
  distance from the cluster centroid (closest first). Outliers land at
  the tail so a Shift-range-select picks off the "other person" in a
  mixed-sibling cluster.
- **B key — Split by nearest person.** For a cluster that mixes two
  siblings, once both are labelled, `B` assigns every face to whichever
  of the two nearest labelled people (measured against the cluster
  centroid) it is closer to; preview + confirm before it commits.
- **Reference embeddings exclude `is_disputed=true` AND low-quality
  faces.** One wrong tag on a blurry crop must not quietly poison every
  future match.
- **Multi-prototype references** (fix-up 3). Each labelled person's
  reference set is up to 5 prototypes produced by scipy k-means over
  their non-disputed, quality-gated faces (fewer for tiny samples;
  single-mean fallback for < 3 faces). Suggested match is nearest
  prototype, not the mean, so a lifetime doesn't split across age
  bands. Under the primary suggestion the side pane also shows the
  next 2 candidates ("Also probably: …"). Unlabelled clusters within
  `FACE_CLUSTER_DIST` of any labelled person's nearest prototype are
  badged "likely <name>" in the cluster header.
- **Full-photo preview** (fix-up 5). Space or double-click on a face
  tile opens a right-hand pane with the whole photo, the current face
  outlined in yellow, every other detected face outlined and labelled
  with its person name where known, plus year / batch#sequence /
  folder / back transcription in the caption. Left/Right step through
  the cluster; Esc closes. Space-and-hold is a peek (release closes
  if held > 300 ms); Space-tap locks it open. Clicking another face in
  the preview jumps to that face's cluster (unlabelled) or opens the
  person editor (labelled).
- **Face coordinate frame** (fix-up 6). Every face bbox in `faces.bbox`
  is in the EXIF-transposed (display) orientation of the working copy
  at full resolution. `photos.width/height` are display dims;
  `photo_masters.width/height` are the raw file dims. Ingest reads
  EXIF via `image_io.probe_image()` and stores `photos.orientation`
  (1..8). Client-side downscale, face-crop generation, and the preview
  all `ImageOps.exif_transpose` before drawing so coordinates match.
  Pre-fix-up-6 rows are repaired by
  `python -m photoarchive.tools.repair_face_boxes`
  (backfills orientation, swaps dims for {5,6,7,8}, and rescales every
  stored bbox by (H_raw/W_raw, W_raw/H_raw)).
  `python -m photoarchive.tools.diagnose_face_box PHOTO_ID` prints
  everything relevant for one photo in one report.
- **Working-file integrity** (fix-up 7). Some scans went through
  `_staging/` in Phase 2 and later got released via rebuilds /
  rejections, leaving `photos.working_path` stale. Every non-deleted
  photo's `working_path` should be `WORKING_DIR/{id:08d}_{sha[:8]}.{ext}`
  and the file must exist. `python -m photoarchive.tools.check_working_files`
  scans every photo; for anything missing it looks under the standard
  name (updates the pointer only), then under `_staging/{sha}.{ext}`
  (moves into place, bumps `file_version`), then copies from the
  preferred master as a last resort (masters are read-only — copy,
  never move). Audit row per repair; truly-missing photo ids are
  listed. `--dry-run` for a report. Run this in the Phase 9 push
  pre-flight and after any `_staging/` reshuffle.
- **"Not a face"** soft-deletes the row (`is_deleted=true`, `deleted_at`,
  `delete_reason`) and writes an audit row. Every selector filters out
  deleted rows.
- **Face crops are precomputed** at collect time to
  `THUMBS_DIR/faces/{face_id}.jpg` (padded ~15% around the box, 256 px
  edge). The cluster grid renders straight from those files.
- **George's assignments in Faces mode are facts** — `faces.person_id`
  is set, `source='human'`, audit row `face.assign`. Accepting the AI's
  suggestion is the same. Contributor-side suggestions from the web
  arrive later in `suggestions` (Phase 9).
- **Merge two people:** faces and name variants move onto the winner
  (variants deduped by lower(variant)); loser is soft-deleted; audit rows
  written both directions (`person.merge`, `person.merged_into`).

## Web (CraftTags lessons — always apply)
- Token links land on a POST-confirm page. GET on the token changes nothing.
- `app.set('trust proxy', 1)` before any middleware that reads client IP.
- Register specific routes before wildcards.
- Watch fail2ban when smoke-testing from a new IP.

## Web auth (Phase 8 onwards)
- **No signup.** Strangers use `/request-access` → George approves via an
  emailed confirm-page link (or `/admin/access`) → the new user gets a
  first magic link → session cookie. Only `tools/create-admin.js` creates
  admins; there is no other path.
- **Expiries are computed in SQL.** Access-request tokens
  `token_expires_at = now() + interval '72 hours'`; magic-link
  `expires_at = now() + interval '15 minutes'`. Never a JS `Date`.
- **Magic-link tokens are stored hashed.** The raw 32-byte hex token
  goes only into the emailed URL; `magic_links.token_hash` stores
  `sha256(token)`. Lookups hash-then-compare.
- **loadUser gates suspension.** On every request, if the user's status
  is not `active`, `loadUser` destroys the session and treats them as
  anonymous. Suspending in `/admin/users` also deletes their `session`
  rows for tidiness.
- **CSRF policy:** per-session `_csrf` token on every authed POST form
  (`req.user` set). Pre-auth POSTs (`/request-access`, `/login`) rely on
  the honeypot + rate limiter. Token-URL POSTs (`/a/:token`,
  `/admin/access/:token/{approve,deny}`) rely on the unguessable token.
- **Rate limiter:** 5 / 15 min per IP on both `/request-access` and
  `/login`, with independent counters.
- **Never reveal whether an email exists.** `POST /login` for a
  suspended/unknown email returns the same "check your inbox" page and
  sends no email; only the internal log records the miss.
- **Audit namespace `auth.*`:** `auth.request_access`,
  `auth.request_access.duplicate`, `auth.approve`, `auth.deny`,
  `auth.login`, `auth.suspend`, `auth.reactivate`, `auth.role_change`,
  `auth.magic_link.sent`, `auth.magic_link.expired_attempt`. `actor` is
  the acting user's email (or `system` for automatic steps, `bootstrap`
  for `create-admin.js`).
- **Service token:** desktop → web sync uses
  `Authorization: Bearer <SERVICE_TOKEN>` with constant-time compare and
  no `users` row. `is_service` on `users` is unused for now.
- **Dev mail** goes to `web/tmp/mail/` when `POSTMARK_API_KEY` is unset —
  the sink is gitignored; sign-in links there are clickable.

## Ops notes
- `GC.md` (gitignored) at the repo root holds per-machine paths, DB
  passwords, service URLs, deploy steps. Same convention as every other site
  George runs. Never commit its contents; never copy from other projects.

## Where things are
- `PROJECT-PLAN.md` — current phase, decisions, phase list.
- `photo-archive-build-prompts.md` — original 12-prompt spec.
- `shared/SCHEMA.md` — table-by-table schema notes.
- `shared/migrations/` — ordered Postgres migrations.
- `prompts/phase-NN-*.md` — the prompt for each phase; George adds answers
  under `## Answers` and says "go".
