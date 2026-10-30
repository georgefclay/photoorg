# Phase 12 — Download and export

Read `CLAUDE.md` (Inviolable; Web core / sync — visibility, id ranges, `pull_web_origin`, `files_only_for_grouped`, the fail2ban rule; Web pages — services layer, group scope, media routes; Corrections — the `edited_on_*` pair and the soft-deleted join tables), `PROJECT-PLAN.md` (§2 decisions, Phase 12, §5 item 11), `shared/SCHEMA.md`, and `photo-archive-build-prompts.md` §11. Work in `web/` and `desktop/`. Start with `git pull`. Run after Phase 13 fix-up 1 (date hygiene) has landed — the zip tree is built from dates, and they have to be right first.

GOAL: **everyone holds a copy, so the archive survives the server dying.** Any signed-in user can download any number of the photos they can see — one, an album, a person, a search, a year, or everything — at full resolution, with the embedded metadata Phase 13 wrote, in a real folder tree rendered from the database. George can take the entire archive out of this system in open formats. And the one album gap left from Phase 10 closes: albums become editable on the web and flow back to the desktop.

## A. Downloads (web)

### What can be selected
A download is a **selection** expressed as a filter the existing `services/` already understand — never a list of ids posted by the browser: one photo; an album; a person; a place; a search query; a year or year range; the current group scope; or "everything I can see". Every selection goes through `middleware/visibility.js` and the group scope exactly as the pages do. `is_private` and `is_deleted` never appear (private never reaches the VM anyway). **Backs are excluded** — the transcription is already embedded in the front's description. A visible photo whose file is not on the VM (`has_file = false`, i.e. not yet grouped and pushed) is **listed in `MISSING.txt`** inside the zip with its id, date and scan locator — never silently absent. For contributors that list is empty by construction; for an admin browsing unfiled photos it is the honest answer.

### Two paths by size
- **Immediate** (≤ `DOWNLOAD_SYNC_MAX_PHOTOS`, default 200, **and** ≤ `DOWNLOAD_SYNC_MAX_BYTES`, default 1 GB by `file_size` sum): stream the zip straight to the response, `store` method, never touching the disk and never buffering a file. One photo is the degenerate case and gets the single file, not a zip.
- **Background job** (anything larger): a `download_jobs` row (`user_id`, the selection as jsonb, `status` queued|running|done|failed|expired, `photo_count`, `bytes_estimate`, `zip_path`, `zip_bytes`, `error`, `created_at`, `started_at`, `finished_at`, `expires_at = now() + interval '72 hours'` **computed in SQL**). A child process (`node tools/zip-worker.js <job id>`) builds the zip under `PHOTO_DIR/downloads/<job id>.zip` so the API never blocks; one job runs at a time; one *queued or running* job per user (admins exempt). When it finishes, Postmark sends "your download is ready" (text + html templates like the others) with a link to `/downloads/:id` — the link carries no secret because the page requires sign-in and ownership (or admin); an expired session goes through the normal magic-link flow with `return_to`. A failed job emails that too, with a one-line reason.
- **Disk guard, before anything starts:** refuse a job when `bytes_estimate × 1.1 + 5 GB` exceeds free space on `PHOTO_DIR`'s volume, with a plain message telling the user to pick a smaller selection; admins see the numbers. A sweeper on the existing cron deletes expired zips and marks the rows `expired` (the row stays — audit, not a delete of archive content; zips are derived files and may be deleted, like `MANUAL_FIX_DIR`). Restart safety: a `running` job whose process is gone is marked `failed` at startup and the user is told to request it again.

### The tree inside the zip
Rendered at zip time from `capture_date` + `capture_date_precision`, **only when `capture_date_confirmed`** — a pending date suggestion is not a date:
- `YYYY/MM/` for `exact` and `month`;
- `YYYY/` (files at the top of the year folder) for `year` — **never invent a January**;
- `1960s/` for `decade`;
- `Unsorted/` for `unknown` or unconfirmed.
Optional **`People/<common name>/`** parallel tree (checkbox, default off), a photo under every person tagged in it — duplication inside the zip is fine. File names inside the zip: `{YYYY-MM-DD|YYYY-MM|YYYY|undated}_{photo_id:08d}.{ext}` — sortable, unique, and the embedded metadata carries everything else. Every zip also holds `README.txt` (what this is, who requested it, when, how the tree was built, counts) and `manifest.csv` (photo id, path in zip, master sha256, scan locator, date + precision, people, albums). Album, person and place names come from live rows only (`is_deleted = false` on `album_photos`, `photo_places`, `person_name_variants`).

### Where it is offered
A **Download** action on: the photo page (this photo, full-res, synchronous); the album, person and place pages, the search results page and Browse (this selection); and the account menu ("Everything I can see"). Phones first — one tap, then a confirmation that says photo count, estimated size, and whether it will arrive now or by email. `/downloads` lists the user's jobs with status and expiry. **Nothing here may answer 4xx during normal use** (the fail2ban rule): not-ready and expired are 200 pages that say so.

### Audit
`download.request`, `download.ready`, `download.fetched` (per zip fetch), `download.failed`, `download.expired` — with user, selection, counts, bytes. The monthly activity report gains a downloads line.

## B. Admin full export (desktop)
The spec puts this on the web, but the complete archive lives on the laptop — `photoorg` is authoritative and `WORKING_DIR` holds every working copy, while the VM holds files only for grouped photos. So the export is a **desktop tool**: `python -m photoarchive.tools.export_archive --to <dir> [--masters]` writes `images/` as the same tree as §A (built by the same rules — share the tree logic as a small pure module ported once, with the same fixtures on both sides so the two cannot drift), `db/photoorg.sql` (`pg_dump --format=plain`), `db/csv/<table>.csv` for `photos`, `photo_masters`, `people`, `person_name_variants`, `faces`, `albums`, `album_photos`, `places`, `photo_places`, `photo_backs`, `comments`, `suggestions`, `audit_log`, and `README.txt` naming the schema version (migration head) and the date. `--masters` adds `masters/` with the original files under their source paths (read-only copy, masters guard first). Resumable: existing files with matching size are skipped. The Sync tab gets an **Export archive…** button that runs it with a progress line. Chunked and memory-safe like every whole-archive pass (CLAUDE.md).

## C. Albums on the web — closes §5 item 11
Admins create, rename, describe, soft-delete and re-order albums and add/remove photos from the album page and from the photo page's album strip, mobile-first. Web-born albums get web ids (≥ `WEB_ID_FLOOR`) and come down to the desktop through `pull_web_origin` (`albums` + `album_photos`), exactly as web-born people and places do; renames of desktop-born albums ride the Phase 15 `edited_on_web_at` pair; removals ride `album_photos.is_deleted`; `position` syncs with the row. Audit `album.create` / `album.update` / `album.delete` / `album_photo.add` / `album_photo.remove` / `album.reorder` with previous/new. Contributors still see albums read-only — a contributor's "this belongs in album X" stays a suggestion. The desktop Corrections > Albums tab stops saying the web is read-only.

## Not in this phase
Resized downloads (full only, by rule). Download of backs. Per-photo download counters on the web beyond the audit. Download of private photos (never on the VM). The disk itself: see the question below.

## Tests
Selection goes through visibility (a non-member's selection never includes a photo they cannot open; the count matches the page); backs never in a zip; a photo without a file lands in `MISSING.txt`; tree rules — `exact`/`month` → `YYYY/MM/`, `year` → `YYYY/` top, `decade` → `1960s/`, unknown or unconfirmed → `Unsorted/`; People tree duplicates; file names sortable and unique; `README.txt` and `manifest.csv` present and correct; immediate path never writes to disk; background path runs in a child process and the API answers a page request while it runs; one job per user; expiry computed in SQL; disk guard refuses; sweeper expires and deletes the zip but keeps the row; restart marks orphaned `running` jobs failed; download pages are 200 for not-ready and expired; export tool produces sql + csv + images and is resumable; the shared tree logic passes identical fixtures on both sides; web-born album comes down through `pull_web_origin`, a desktop rename and a web rename resolve by the Phase 15 rule, a web removal reaches the laptop, a re-order syncs.

## Verification, then stop
1. `npm test` and `pytest` green.
2. Deployed to the VM (fresh dump → pull → `migrate:up` → `npm ci` → restart → `/healthz`; `/sync/status` `id_floor.ok`), then **from George's phone**: download Album 6 (139 photos, immediate path) — open the zip on the laptop, tree and `manifest.csv` correct, one photo opened in Windows Photos shows its people.
3. Admin "Everything I can see" as a **background job** on the VM: the email arrives, the link works after re-sign-in, `README.txt` counts match `/sync/status`, `MISSING.txt` lists exactly the ungrouped photos, and a page request during the job answered normally (paste the timing). Then wait for the sweeper or run it: zip gone, row `expired`.
4. `export_archive` on the laptop into a scratch folder: `db/photoorg.sql` restores into a fresh database (`createdb` + `psql < …`, then `select count(*) from photos`), CSVs open, tree matches §A for three spot-checked photos. Report the folder size and elapsed time.
5. Create an album on the web, add three photos, re-order, rename; pull on the desktop; the album is there with the order and the name; remove one on the web, pull, it is gone on the laptop; rename on the desktop, push, the site shows it. Paste the audit rows.
6. `CLAUDE.md` (Downloads section: the two paths, the tree rules, the disk guard, the 200-not-4xx rule for download pages; Albums now editable on the web), `shared/SCHEMA.md` (`download_jobs`), `PROJECT-PLAN.md` (Phase 12 done, §5 item 11 closed), commit and push: `Phase 12: download and export`.

## A question for George, not for Code — answer before the whole-archive path is exercised
**The VM disk.** 80 GB root, ~66 GB free at Phase 14. After the group bulk-assignment the VM holds ~28 GB of working files; a whole-archive zip is another ~28 GB (`store`, JPEG does not compress); plus the database, nightly dumps and 72 hours of retention. That is tight. Either grow the root to 160 GB before the full file push, or accept the disk guard refusing whole-archive jobs until it is grown. Say which.

---

## Answers to Claude Code's questions
(added as they come)

### Claude Code's questions (2026-10-30)

Measured on `photoorg` first: 12,686 photos in scope, **28 GB** of working
files (`file_size` sum), **31 GB** of masters, **850** `photo_backs`, **0**
private photos today, 6 live albums, **0** rows in `comments` (bodies are
web-only), no two live people share a nickname-or-given + surname. The VM's
only cron line is the nightly `pg_dump`; the login flow has no `return_to`
today; `/sync/pull/web_origin` carries people / places / relationships /
faces, not albums.

**Q0. The VM disk** (the prompt's own question, still open) — 160 GB root
before the full file push, or the disk guard refusing whole-archive jobs
until then? Nothing in the build depends on the answer; verification 3 does
(today the VM holds only 139 files, so a whole-archive job is small).

**Q1. A web-born album renamed on the desktop — verification 5 contradicts
the id-range rule.** Step 5 creates an album on the web, then "rename on the
desktop, push, the site shows it". But a web-born album has an id ≥
`WEB_ID_FLOOR`, which is web-authoritative: the push selects `id < floor`,
`/sync/albums` 400s on anything ≥ floor, and the next pull would overwrite a
desktop edit anyway. Options:
- **(a) Recommended:** web-born albums (name, description, membership,
  order) are **read-only on the desktop**, with the reason on screen in
  Corrections > Albums, and the Corrections find & replace skips them (each
  tier sweeps only the text it owns). Step 5's desktop rename is done on a
  desktop-born album (Album 6) instead — that is the case the Phase 15 pair
  actually decides.
- (b) Make albums an exception: the push sends guarded text for ≥ floor
  albums and the web takes it under `sync_web_edit_wins`. That breaks
  "sync never crosses the ranges" for one table, which is the kind of
  special case the risks section warns about.

**Q2. The full export can't hold what only the web holds.** `export_archive`
reads `photoorg`, whose `comments` table is empty: comment bodies, likes,
users, group memberships made on the web, contributions and web-born
suggestions live only on the VM. An export named "the entire archive" would
silently lack the family's written memories. Options:
- **(a) Recommended:** `export_archive` also calls a new service-token
  endpoint `GET /sync/export/web_only` (paged) and writes
  `db/web/<table>.csv` for `comments` (bodies, incl. hidden, flagged),
  `likes`, `groups`, `group_members`, `users` (name, email, role, status —
  never tokens, sessions or hashes) and `contributions` / `contribution_files`
  metadata. Read-only on the VM; README says what came from where.
- (b) README points at the VM's nightly `pg_dump` and the export stays
  laptop-only.
- (c) The export also copies the latest VM nightly dump into `db/vm/` over
  SSH — complete, but couples a laptop tool to SSH access.

**Q3. Private, junk and backs in the full export.** The zips exclude backs
and never see private; the full export is George's own copy on his own disk.
**Recommended:**
- **Private** photos included, under a separate top-level `Private/` tree
  (same §A rules inside), so a copy can be handed to a relative by deleting
  one folder; README says so. (0 today, but the rule should exist.)
- **Junk** (quarantined) not in `images/`; their rows are in the dump and
  CSVs, and `--masters` still copies their masters (masters are masters).
- **Backs included in the export only**, as `backs/{front_id:08d}_back{n}.{ext}`
  (orphan backs as `backs/orphan_{back_id:08d}.{ext}`) — 850 images, the
  only copy outside the master disk of what the transcriptions were read
  from. Zips stay back-free as the prompt says.

**Q4. Egress cost.** After the bulk-assignment a whole-archive zip is ~28 GB;
AWS bills internet egress at roughly $0.09/GB, so ~$2.50 per relative who
takes everything (and again if they re-download). **Recommended: no cap** —
the goal is everyone holding a copy; the audit rows and the monthly report's
downloads line (with bytes) make it visible. Alternative: a per-user cap of
N whole-archive jobs per 30 days, admins exempt. Say if you want one.

### Decisions I'll take unless you say otherwise
1. **Sweeper:** a new hourly line in the photoorg block of the shared crontab
   (`node tools/download-sweep.js`), installed with the documented
   sed-replace, never `crontab file`; the same sweep also runs at server
   start and marks orphaned `running` jobs `failed`.
2. **`return_to` through sign-in:** `/login?return_to=` (local paths only,
   same `safeReturnTo` as the scope switcher) is stored on the
   `magic_links` row (migration column) and honoured after the link is
   consumed, so `/downloads/:id` → expired session → email → lands back on
   the download page.
3. **Web edits to desktop-born album membership/order come down by the
   Phase 15 rule, not raw `updated_at`.** Every push stamps
   `album_photos.updated_at = now()` on the VM, so pulling "rows changed
   since" would echo every push back down. `album_photos` gains
   `edited_on_desktop_at` / `edited_on_web_at` (human edits only),
   `WEB_EDITABLE.album_photos = ['position', 'is_deleted']`, and
   `/sync/pull/web_edits` carries them; web-born albums and their rows come
   down through `pull_web_origin` as the prompt says.
4. **People tree folder name** = the Phase 13 keyword form (nickname-or-given
   + surname + suffix); a collision (none today) gets ` (b. YYYY)`, else
   ` #id`. Characters illegal on Windows are replaced, names trimmed to 80.
5. **One-photo download** is the working file under its zip-style name
   (`1962-07-04_00001234.jpg`); the button is not shown when the file isn't
   on the VM (no 4xx path to reach).
6. **Immediate-path concurrency**: no global limit on streams (they are
   disk-to-socket, no memory growth); the background queue is one job at a
   time as specified.
### Answers (PM, 2026-10-30)

**Q0 — deferred; the guard stays on and the phase proceeds.** George decides
the disk before the whole-archive path is real, i.e. before the group
bulk-assignment lands the 28 GB. One build rule either way: `bytes_estimate`
(and the size shown in the user's confirmation, and the immediate-path byte
threshold) sums `file_size` only over photos whose file is on the VM —
`MISSING.txt` entries add no bytes and must never trip the guard.

**Q1 — (a).** Web-born albums (name, description, membership, order) are
read-only on the desktop, with the reason on screen in Corrections > Albums,
and the desktop find & replace skips them: each tier corrects only the text
it owns, and sync never crosses the id ranges. Verification 5 is amended —
the web-born album proves the web-to-desktop direction only (create, add
three, re-order, rename, remove one; pull; all of it lands on the laptop);
the desktop-rename half runs on Album 6: rename on the desktop, push, the
site shows it (and a web rename of Album 6 still rides the Phase 15 pair
down).

**Q2 — (a).** `GET /sync/export/web_only`, same service-token guard as the
other `/sync` routes, read-only, paged; the export writes
`db/web/<table>.csv` for comments (including hidden and flagged, with their
flags), likes, groups, group_members, users (name, email, role, status —
never tokens, sessions or hashes), contributions and contribution_files.
Two additions: **web-born suggestions join the set** if your reading holds
that they never come down (verify first — they are family-entered text with
the same standing as comments; if they do come down through a pull, skip
this); and **an unreachable VM must not fail the export** — finish the
local part and write `db/web/UNAVAILABLE.txt` with the reason. README states
that `db/` holds personal data (emails) and is George's own backup: a copy
handed to a relative is `images/` (minus `Private/`), never `db/`.

**Q3 — all three as recommended.** `Private/` top-level with the same §A
rules inside it and a README line saying to delete that one folder before
handing a copy on (0 private today; the rule exists from now). Junk out of
`images/`, present in the dump and the CSVs, masters still copied under
`--masters`. The 850 backs as `backs/{front_id:08d}_back{n}.{ext}`, orphans
as `backs/orphan_{back_id:08d}.{ext}`, counted in README; zips stay
back-free as specified.

**Q4 — no cap.** Copies in many hands is the goal; one queued-or-running
job per user and the 72-hour expiry already bound the worst case, and the
audit bytes plus the monthly report's downloads line keep the spend
visible. A cap is a small fix-up if the report ever shows abuse.

**Decisions 1–6: approved as written.** Notes: the startup sweep and
"orphaned `running` marked `failed`" are one code path, not two; `return_to`
lives on the `magic_links` row and never in the emailed URL; the
`album_photos` migration is additive and its `down` drops only the new
columns, touching no rows; ties still go to the web, and `position` and
`is_deleted` changes both stamp the edit pair.

GO.
