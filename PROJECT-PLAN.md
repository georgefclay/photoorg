# Family Photo Archive — Project Plan

Owner: George Clay. PM: Claude (this Cowork chat). Coding: Claude Code on the Windows laptop and on the Mac mini.
Spec: `photo-archive-build-prompts.md` (the 12 build prompts). This file records what changed after looking at the real data, the decisions made, and the phase plan. Phase prompts are written one at a time from this file when each phase starts.

Last updated: 2026-09-14

---

## 1. What the data actually looks like

Surveyed 2026-09-01. The spec assumed ~5,000 images with ~3,000 scans. Reality:

| Source | Files | Notes |
|---|---|---|
| `D:\Photos` | 11,589 JPG | Export from a photo app. Folders `_YYYY-MM` (2004-01 → 2024-11) plus one `Scanned` folder (19 files). All filenames are `f<number>.jpg`. No videos, no RAW. 2005–2010 hold ~10,800 of them. |
| `D:\Scanned Photos` | 5,279 (5,112 JPG + 167 TIFF) | `Batch 00001`…`Batch 00045` plus named folders (`Chuck and Lola Wedding`, `Shannon and George Wedding`, `Carol and John Wedding`, `George Clay - Navy`, a `High Quality` subfolder). Three filename styles (`2025-10-29-07-11-0001.jpg`, `IMG004.JPG`, `IMG_20251111_0002.tif`). Scan resolution varies. Scanning continues. |

Total ≈ 16,900 files, roughly 3× the spec's estimate.

**Progress (2026-09-07):** Ingest complete — 1,944 files in `D:\Photos` were byte-identical to scans and recorded once under the scan's provenance. Triage complete — **12,821 keep, 1,016 junk, 0 private**. Back pairing review complete (826 accepted, 570 rejected). Phase 4 dedupe built and running against the keep set with back-shaped photos excluded: **129 pending groups** over 12,232 eligible photos (119 pairs, 6 triples, 2 quads, 2 fives; min-distance histogram peaks at 0/6/8). Elapsed ~4 min. George's 30-group review pending.

**Progress (2026-09-07):** Dedupe complete (129 groups resolved). Back pairing complete — **826 backs**. Phase 5 inference service live on the M4 (`Georges-Mac-mini.local:8500`, LaunchAgent, Qwen3-VL-8B-4bit + InsightFace buffalo_l). Measured throughput on the M4 after per-endpoint image sizing (1024 px for classify/describe/date, 1536 for backs/faces): classify 8.5 s (~30 h), describe 9.1 s (~32 h), estimate-date 11.9 s (~42 h), transcribe-back 18.7 s (~4 h over 826 backs), detect-faces 0.19 s (~40 min). Batches run unattended on the mini (upload → inbox → queue survives restarts → results collected by the laptop when it is on). Blackout `Tue/Fri 04:30–07:30` set provisionally — no scheduled job was found on the mini; George to confirm with `sudo crontab -l`. Memory guard pauses batches if Ollama or anything else takes the RAM.

**Progress (2026-09-14):** Phase 9 landed on `main` in eight code commits (visibility → read APIs → write APIs → groups → sync → contributions → desktop → verification). Web tests 61/61 green, desktop pytest 188/188 (1 skipped), shared migrations smoke 3/3. New migrations `phase-9-groups` (`groups`, `group_members`, `photo_groups` with soft-delete + `updated_at` triggers for LWW sync) and `phase-9-contributions` (`contributions`, `contribution_files` with dedupe fields, `approved_group_ids` per file, `is_video` flag). The `photo-back-orphan` migration's `down` now aborts with a clear message when orphan backs exist so they can never be silently deleted. Verification setup: `D:\Contributed\` with icacls-deny on the root only so `_incoming/` stays writable for the append-only pull; web `PHOTO_DIR=C:\Photos-web-local\`; shared service token recorded in `GC.md`; `Clay Family` group created with George as moderator and a test-contributor member; port 8090 open on the Private firewall profile. Full push in flight against the shared local Postgres (`photoorg`) — 13 257 non-private/non-junk photos scheduled to sync.

**Progress (2026-09-15):** Phase 9 closed. The phone-on-LAN upload test could not connect and was **deferred to the live domain** — it is now verification step 2/4 of Phase 14. **Phase order changed: 14 (deploy) runs next, before 10 (pages)**, so every remaining phase is tested on a phone against `https://cyberdinosaurs.com`. Prompt: `prompts/phase-14-deploy.md`. **Phase 14 deploys the site with metadata + one test group's files only.** The full 28 GB file push is deferred until the mini's classify/describe/date jobs finish and Phase 7 (cleanup) has replaced the working copies it will replace — otherwise most files would be pushed twice. VM disk (30 GB, 19 free) is enough for the test set; grow to 80 GB before the full push.

**Progress (2026-09-27):** **Phase 7 (scan cleanup) built and green — not yet run on real scans.** Desktop pytest 291/291 (+1 skipped), web 151/151, one new migration `phase-7-cleanup` (up and down both exercised). New `desktop/src/photoarchive/modes/cleanup/` (analyse / geometry / ops / render / repo / accept / split / job / report / remote / ui), two CLI entry points (`run_cleanup`, `cleanup_report`), and 6 new test files (geometry, analyse, accept, split, report, ui, remote) plus tombstone tests on both sides. Scope measured against the live DB: **4 095** scans in scope (3 947 JPEG / 148 TIFF, up to 93.7 MP), 3 666 with faces, 4 187 labelled faces on scans, 54 batches, 639 back-shaped scans excluded, `orientation` NULL on every in-scope scan (so display frame == raw frame). **Verification steps 2–4 are blocked on the `D:` drive being attached** — masters, `WORKING_DIR` and `CLEANUP_DIR` all live there and only `C:` is mounted; batches 00001–00005 (~380 photos, not the ~250 the prompt guessed) are the first run once it is back. Three real bugs found by the tests on the way: a black scanner bed masking in as one whole-scan print (HSV saturation on near-black noise), a live `QThread` dropped when a preview render was replaced, and a preview landing wiping the last decision off the status bar.

**Progress (2026-10-02, evening):** Phase 7 fix-up 7 pushed and verified on the site (orientation root cause fixed; see §5 item 13). Bulk accept done: 991 accepted, 2,391 clean, 62 pending (61 needs-manual, 1 split). **Phase 9 fix-up 2 done** — the `Canaca` → `Canada` correction is live on the VM (0 rows left, album 6 renamed, search matches 140 photos). Diagnosis: the original `update` had been run against **`photoorg_web`**, the laptop's web DB, not `photoorg`, so push (which only ever reads `photoorg`) had nothing to carry. Both triggers and selectors were already correct — the metadata push is a full re-send every run. Three real faults found and fixed on the way: `/sync/person_name_variants` never updated `person_id`, so a desktop person merge left the web attributing the nickname to the soft-deleted loser; one refused stage aborted every stage after it, so a 500 on `photo_masters` silently stopped `albums`/`suggestions` from ever re-syncing; and `tools/run_push.py` had been broken since fix-up 1 added `state_dir`. **The VM is a migration behind** — see §5 item 17. Next: **Phase 13** (`prompts/phase-13-metadata-writer.md`), then Phase 12, then George's group bulk-assignment.

**Progress (2026-10-02):** Phase 7 closed (see its section). **Order change:** Phase 13 (metadata writer) comes before Phase 12 (download) and before the big group assignment — the writer bumps `file_version` on nearly every photo, so doing it first avoids re-pushing the archive. There is no separate "full push" step any more: `files_only_for_grouped` means files flow to the VM as George assigns groups; the one-time full push happens implicitly when the last group is assigned. Prompt: `prompts/phase-13-metadata-writer.md`.

**Progress (2026-09-30):** Phase 7 built (62282d8), fix-ups 1–3 (colour cast → highlight agreement → tonal ops opt-in; deskew from print edges not the enclosing box; crop content guard + calm mask) and a low-memory loader (505cf3d). Full scope analysed: 3,456 scans → 2,392 clean, 1,064 pending (981 geometric-only bulk-acceptable, 17 splits, 68 needs_manual). Mini VLM jobs all complete and collected. M6 arrived; service move pending. Next: George's Cleanup review, then the one-time full file push, then Phase 12.

**Progress (2026-09-17):** Phase 9 fix-up 1 (web-origin ids) live on the VM: `WEB_ID_FLOOR` = 10¹², all seven tables bigint, sequences moved only under `PHOTOORG_DB_ROLE=web`; push pulls first; web never re-opens accepted suggestions. Found on the way: back images and face crops had never been pushed (0 on the VM) — push now sends backs, web cuts face crops on demand; `/media/display` serves ≤1600 px for phones; two slow queries fixed. Phase 10 foundation committed locally (shared query layer, group switcher, layout, Browse + attention strip); page work in progress.

**Progress (2026-09-16, later):** Phase 6 fix-up 11 landed. Root cause of the `classify` hand-over crash: the Phase 9 local verification push ran the laptop web server against the desktop's own `photoorg` DB, and the sync upsert rewrote every `working_path` (12,677 photos + 826 backs) as a bare basename. Fixed with one working-file resolver, skip-not-abort hand-overs, `check_working_files` repair (now covers backs, post-condition "still bare = 0"), a shared-DB guard in `push()` (`/sync/status` identity check), and a separate `photoorg_web` DB on the laptop. **`classify`, `describe`, `estimate_date` handed over and running on the mini** (~104 h on the M4; M6 swap on 9/22 resumes the queue). Next: Phase 10.

**Progress (2026-09-16):** **Phase 14 done — `https://cyberdinosaurs.com` is live.** Root grown to 80 GB (66 free). 28 migrations on the VM; metadata for 13,257 photos, 23,461 faces, 5,433 suggestions synced; files for one test group (139 photos, Clay Family / Album 6). Postmark round-trip verified (request → approve → magic link) from a phone. Nightly `pg_dump`, logrotate, fail2ban whitelist, Caddy + systemd in place; templates under `ops/vm/`. Open: UptimeRobot monitor, off-site backup, the full 28 GB file push (after Phase 7 + mini jobs). Next: **Phase 10 (pages)**, prompt `prompts/phase-10-web-pages.md`.

**Progress (2026-09-16):** Phase 14 landed. `https://cyberdinosaurs.com` live over TLS, admin bootstrapped, deploy key on GitHub, Caddy site + systemd unit + sudoers + logrotate installed, root FS grown 30 → 80 GB (66 GB free), nightly `pg_dump` cron with 14-day retention, fail2ban whitelist updated. Postmark end-to-end (request-access → approve → magic-link → sign-in) confirmed with `georgefclay+phase14@gmail.com`. Desktop pointed at prod; new `files_only_for_grouped` push flag (default on) sends full metadata but files only for photos in a live `photo_groups` row — 13 257 metadata rows + 139 Clay Family files (Album 6: Summer 1992 — Canada) uploaded in 11 min 43 s (260.5 MB), second push 0 files in 135 s. `/sync/status` counts on prod match the laptop exactly. Off-site backup + UptimeRobot monitor + full-file push queued as TODOs in `GC.md`. Ops templates committed under `ops/vm/`.

Consequences of the survey:

- **Junk is common in `D:\Photos`** (sample: a phone photo of a Windows product-key sticker). A cull pass is required before anything expensive runs.
- **Sensitive images exist** (keys, documents, IDs). They must never reach the web server.
- **Front/back pairing is sporadic.** Backs were scanned only when there was writing, immediately after the front. Most batches are fronts only. Odd/even pairing is wrong; pairing must be detected and reviewed.
- **Folder names carry information.** `_YYYY-MM` is a weak date hint for digital files. Named scan folders describe the event. Both are stored, neither is trusted as fact.
- **TIFF vs JPG duplicates are likely** in the named folders and Batches 32/33/45. Dedupe must match across formats and prefer the TIFF.
- **Both source roots are the masters.** Read-only, never written. The working set is derived from both.

## 2. Decisions

| Topic | Decision |
|---|---|
| Repo | One monorepo at `C:\Programming\Photos`: `desktop/` (Python + PySide6), `inference/` (Python + FastAPI, runs on the Mac), `web/` (Node + Express + EJS), `shared/` (schema, migrations, API contract, nickname seed). George creates the GitHub repo. |
| Database | Local Postgres on the laptop owns everything the desktop app does. The VM has its own Postgres, populated only by the manual Sync button. Same migrations on both. |
| Web frontend | **Express + EJS + vanilla JS**, not React. Matches every other site George runs; no build step. |
| Hosting | Existing AWS VM (Ubuntu, Caddy, systemd, Postgres, Node 24), same deploy pattern as CraftTags: `git pull` + `systemctl restart`. Main disk enlarged for the working set. |
| Domain and email | **cyberdinosaurs.com** (George owns it). `BASE_URL=https://cyberdinosaurs.com` on the VM; Postmark sender signature + SPF/DKIM for the domain at Phase 14; from address `archive@cyberdinosaurs.com`. Site name in the UI: "Cyber Dinosaurs". |
| Inference hardware | Develop on the M4 Mac mini (16 GB) now; move to the M6 (32 GB) on 2026-09-22. Model, host, and quantization are config, not code. |
| Face embeddings | InsightFace (ArcFace) via ONNX. VLM is never used for identity. |
| Privacy | `is_private` flag on photos. Sync skips private rows and files; the API refuses to serve them even if present. |
| Triage | New desktop mode: keep / junk / private, keyboard-driven, with AI pre-sort once inference exists. Runs before dedupe. |
| Folder hints | Ingest stores `source_root`, `source_folder`, `source_filename`, `scan_batch`. Named folders create a virtual album of the same name and a low-confidence event/date suggestion. `_YYYY-MM` becomes a capture-date suggestion with precision `month`, source `import`. Folder name is also written to the working copy's XMP by the metadata writer (Phase 13). |
| Physical location | Every scan keeps a **physical reference**: `scan_batch` (the source folder name, e.g. `Batch 00012` or `Chuck and Lola Wedding`) plus `scan_sequence` (its order within that folder by filename) and `source_filename`. This is how George finds the physical print. It is immutable, shown on the photo detail page and in every desktop review view, searchable, written to the working copy's XMP, and included in the download zip's metadata. When dedupe keeps a phone original over a scan, the scan's physical reference is copied onto the keeper (`physical_ref_note`) so the print can still be found. Backs inherit the front's reference. |
| Rescans | Prints live in envelopes of ~50 per batch in roughly scan order, so batch + approximate sequence is enough to find one. George will rescan important prints at 600/1200 DPI to TIFF later. A rescan is **not a new photo**: ingest matches it by pHash to the existing scan (or George picks the original in the pairing grid), the new file becomes the preferred master for that photo row, the old file is kept as a prior version, and every tag, date, face, comment, and like stays attached. A `rescan_wanted` flag on photos, settable from the desktop and the web (admin), feeds a **Rescan list** report grouped by batch envelope so George can pull them in one pass. |
| Growing archive | The archive is never "done". Master roots are a configurable list (`label=path[:kind]`), so a new drive, a new scan folder, or a phone export can be added in Settings. Ingest, triage, AI jobs, cleanup, sync, metadata writing, and downloads are all incremental per photo; re-running any of them touches only what is new or changed. New material is added by dropping it into a master root (or adding a root) and pressing Ingest. |
| Contributor uploads | Any signed-in user can upload — one photo from a phone camera or hundreds from a desktop folder. Uploads land in a holding area on the VM (`uploads/`, originals never modified; they are masters) with `contributions` / `contribution_files` rows, status pending, visible only to admin and the uploader. Resumable: server skips sha256s it already holds. Admin approval page (mobile-friendly) shows thumbnails, uploader, note, EXIF date, and duplicate warnings (sha256 / pHash vs existing photos); approve or reject per file or batch; rejected files are flagged, never deleted. Approved files are **pulled by desktop Sync** into an append-only master root `D:\Contributed\<uploader>\<contribution id>\` (the pull may create files, never modify or remove; guard + manifest enforce), then go through the normal pipeline: ingest (provenance = uploader) → triage (pre-set keep) → dedupe → AI jobs → sync push. Nothing an uploader sends is public until George approves it and the pipeline pushes it. |
| Groups (access model) | **Groups** are the unit of visibility. Admins create groups (`Clay Family`, `Boots Family`, `<Church>`). Users belong to one or more groups (`group_members`, role `member` or `moderator`); photos belong to one or more groups (`photo_groups`). A user sees a photo if they share at least one group with it; admins see everything. **Unfiled photos (no group) are admin-only** — the 12,800 existing photos start unfiled and George assigns groups in bulk from the desktop (by album, batch, person, year, folder) and from an admin "unfiled" queue on the web. People, places, albums, comments, and likes are global: a person page or search shows only the photos the viewer can see, but comments on a visible photo are shown to everyone who can see it, whichever group the commenter is in. **Moderators** (per group) can hide comments and **remove a photo from their group** (it stays in other groups; if it was only in theirs it becomes unfiled) and can approve uploads targeted at their group; they never affect other groups. **Admins** have total control. Uploaders choose one or more of their own groups for each contribution. New users are approved by an admin, who assigns their initial groups at approval time; a group moderator can later add members to their group. `is_private` remains separate and stronger: a private photo is in no group and never leaves the laptop. Groups, memberships, and photo-group assignments sync both ways (desktop bulk tools ↔ web admin/moderator edits) through the existing pull/push. |
| Mobile-first web | Every page is designed for a phone first (thumb-reachable actions, large tap targets, one column), then widens for desktop. Upload, tagging, dating, and liking must be comfortable one-handed. |
| Backs | Ingest runs a "looks like a back" heuristic (mostly blank, handwriting-like ink, low colour) and proposes pairing with the previous file in scan order. Every proposed pair is reviewed before commit. |
| Ops notes | `GC.md` in the repo root, gitignored, same convention as every other site. Own DB role, own secrets. Nothing copied from CraftTags. |
| Deletes | Never. Quarantine + soft-delete flag everywhere. |

Carried over from CraftTags lessons (go into every web prompt): compute expiries in SQL with `NOW() + INTERVAL`; token links land on a POST-confirm page, never act on GET; `app.set('trust proxy', 1)`; register specific routes before wildcards; watch fail2ban when smoke-testing.

## 3. Who does what

| Work | Tool | Why |
|---|---|---|
| Desktop app, web app, schema | Claude Code on the Windows laptop | Needs the GUI, local Postgres, and the D: drive. |
| Inference service | Claude Code on the Mac mini | Needs Apple Silicon + MLX. |
| Phase prompts, code review, unit-test runs, spec upkeep | This chat (Cowork) | Repo is mounted here; non-GUI Python and Node tests run in the sandbox. |
| Repo creation, VM ops (disk, Caddy, DB role, .env), Postmark, domain | George | Credentials and infrastructure. |

Hand-off loop per phase: I write the prompt → George runs it in Claude Code → George reports back (or I read the diff) → I review against the acceptance checks → fix-up prompt if needed → phase closed.

## 4. Phases

Order: **0 → 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11 → 12 → 13 → 14**. Phases 5 (Mac) can run in parallel with 2–4 (Windows).

### Phase 0 — Setup (George + Claude Code, Windows)
- Create GitHub repo; monorepo skeleton; `.gitignore` covering `.env`, `GC.md`, working directories.
- `GC.md` from a template: paths, DB role, service URLs, deploy steps.
- Local Postgres database `photos` and role. `.env.example` for each tier.
- Directory layout on the laptop: `working/`, `quarantine/`, `manual-fix/`, `thumbs/`. Masters stay on D:.
- **Backup check:** confirm both D: roots have a second copy somewhere before ingest starts. If not, that is the first task.
- Accept when: `git status` clean, both `.env.example`s present, `psql photos` connects, backup confirmed.

### Phase 1 — Schema and migrations (Claude Code, Windows) — spec §1
- node-pg-migrate in `shared/`. Applied to local and VM databases identically.
- Spec tables plus: `photos.source_root`, `source_folder`, `source_filename`, `scan_batch`, `scan_sequence`, `physical_ref_note`, `is_private`, `triage_status` (untriaged/keep/junk/private), `is_deleted`, `file_version`, `rescan_wanted`; `mime` covers TIFF; `suggestions.source` gains `import`. Index on `(scan_batch, scan_sequence)`.
- New table `photo_masters` — id, photo_id FK, master_path, sha256, width, height, dpi, mime, is_preferred, ingested_at. One photo, many master files (original 300 DPI JPG, later 1200 DPI TIFF). `photos.master_path` becomes a convenience pointer to the preferred row.
- Nickname seed script. Completeness function. Indexes per spec.
- Accept when: migrate up/down clean on an empty DB; seed loads; a smoke script inserts one photo, one person, one face, one suggestion, one audit row.

### Phase 2 — Desktop shell + Ingest (Claude Code, Windows) — spec §3
- PySide6 shell with sidebar: Ingest, Triage, Dedupe, Cleanup, Faces, Sync. Settings screen.
- Ingest walks **both** roots. sha256, copy to `working/` under a stable name, EXIF, pHash/dHash, thumbnails. Folder hints stored as described in §2.
- Back detection + pairing review grid. Pairs commit only after review.
- Resumable by sha256. Progress, counts, log pane. Masters opened read-only; refuses to run if it can write to them.
- Every scan gets `scan_batch` + `scan_sequence` at ingest; the pairing grid and every later review view show "Batch 00012 #017" next to the thumbnail.
- Rescan detection: a new file whose pHash is within threshold of an existing scan is proposed as a rescan of it in the review grid (accept → new `photo_masters` row, preferred; reject → new photo). Nothing auto-commits.
- Rescan list: report/screen listing `rescan_wanted` photos grouped by batch, ordered by sequence, with thumbnails. Printable.
- Accept when: full run over both roots completes; re-run ingests 0 new files; row count = file count − committed backs; no file under D: changed (verified by a before/after hash manifest); a random scan's batch/sequence in the DB matches its folder and filename position.

### Phase 3 — Triage (Claude Code, Windows) — new
- Grid + single-view, keyboard: K keep, J junk, P private, arrows, undo. Filters: untriaged, by source folder, by year.
- Heuristic pre-sort without AI: screenshots (dimensions/EXIF software), near-blank frames, documents (high edge density, low colour). Later phases add VLM classification.
- Junk → quarantine + soft delete. Private → flag only, stays in working set, excluded from sync.
- Accept when: George triages 500 photos in one sitting without touching the mouse; counts in the DB match; quarantine browser restores one.

### Phase 4 — Dedupe (Claude Code, Windows) — spec §4 — **DONE 2026-09-07**
- pHash/dHash candidate groups, configurable threshold. Cross-format matching (TIFF vs JPG, phone vs scan).
- Keeper pre-selection: EXIF original > TIFF > higher resolution > larger file. Groups of 3+.
- When the discarded copy is a scan, its batch/sequence is copied onto the keeper's `physical_ref_note` before quarantine. The physical reference is never lost.
- Side-by-side synced zoom, keyboard flow, "not duplicates" memory, resumable progress.
- Accept when: seeded test set of known pairs (including one TIFF/JPG pair) is all found; a false pair marked "not duplicates" never reappears.
- **Delivered:** multi-index Hamming search (16 bands × 16 bits, exhaustive to D=15) with a brute-force fallback; 7 rotation/mirror variants hashed from the thumbnail; union-find grouping; keeper reason chain shown in the UI. Resolve reuses `triage.apply_decision`; carries `physical_ref_note`, `photo_backs`, non-preferred `photo_masters`, albums, and suggestions to the keeper; promotes `is_private`. Session undo reverses everything from the `dedupe.resolve` audit payload. Back-shaped photos (`possible_back` hint or any `ingest_pairings.back_photo_id`) are excluded from scan. New tables `dedupe_groups`, `dedupe_members`, `dedupe_exclusions`. First live run: **129 pending groups over 12,232 eligible photos in ~4 min**. Commit `6ac1769`.

### Phase 5 — Inference service (Claude Code, Mac) — spec §2
- FastAPI, MLX, Qwen3-VL 8B (4-bit on the M4, larger on the M6), InsightFace for faces.
- Endpoints: transcribe-back, describe, estimate-date, detect-faces, match-faces, plus `classify` (photo / document / screenshot / receipt / blank) for Triage.
- Batch endpoints stream results; `/health`; JSON logs with timing; interruptible and resumable.
- Accept when: each endpoint returns valid JSON on 5 sample images from each root; 100-image batch survives a kill and resume; `/health` reports model + free memory.

### Phase 6 — Inference client, batch runner, Faces mode (Claude Code, Windows) — spec §6
- Client interface with LAN implementation; retries, timeouts, offline handling.
- Jobs: classify, transcribe-backs, describe, estimate-date, detect-faces. Per-photo status, each re-runnable alone.
- Faces UI: cluster, bulk label, exclude disputed from references, human confirm.
- All AI output lands in `suggestions` with `source='ai'`. Back transcriptions flagged high confidence.
- **Transcribe-back orientation retry** (deferred from Phase 2 fix-up 5): when the first pass returns low confidence, retry with the horizontally flipped and 180°-rotated variants of the image, keep the best, and record `details.orientation ∈ {"upright","mirrored","rot180","rot180+mirrored"}` on the suggestion so the reviewer can tell the scanner had it wrong.
- Accept when: overnight run over the keep set completes; killing at N resumes at N; a disputed tag is provably absent from the reference set.
- **Fix-up 11 (2026-09-16)** — classify hand-over died on a bare `working_path`. Cause: the Phase 9 local verification push ran the laptop web against the desktop's `photoorg` DB and the sync routes wrote basenames over every `photos`/`photo_backs.working_path`. Fixed: one resolver (`resolve_working_path`) used by every uploader and reader; hand-over skips missing/undecodable files (`job_items` failed rows, "N uploaded, M skipped" + Skipped files… button); `check_working_files` repairs backs too and reports a "still bare" post-condition (now 0/0); push pre-flight refuses a web that reports our own DB identity; laptop web moves to `photoorg_web`.

### Phase 7 — Scan cleanup (Claude Code, Windows) — spec §5
- Deskew, crop, multi-print split, colour-cast and fade correction. Always a new derived file; `file_version` bumps so sync re-pushes.
- Review queue: accept / reject-to-manual-fix / remote enhance. Provider interface; Claid + null implementations; cost counter.
- Accept when: 50 scans processed and reviewed; rejected ones sit in `manual-fix/`; master hashes unchanged.
- **Closed 2026-10-02 (George's call: diminishing returns).** Shipped: analyser + review mode, fix-ups 1–6 (colour cast gated three ways then switched off by default; deskew from print edges; crop content guard + calm mask; canvas never grows; queue filters, go-to, hand-split; gutter cut, relative gate, document veto; region editor with grid frame + nudges). All 3,456 scans analysed: 2,392 clean, ~1,000 minor crop proposals **left pending by choice** (visible bed slivers judged not worth 1,000 keypresses), 26+ accepted, splits accepted where the detector was right. Remaining hand-splits (#2651, #2716–2718, #3817) are optional and findable via Go to #. Code works; archive value was smaller than the spec assumed because the scans were already well made.
- **Decisions taken in the prompt's Answers (2026-09-27), all recorded in CLAUDE.md § Cleanup:**
  - **Split children need an identity, not a file hash.** `photos.sha256` is
    `sha256(master_sha256 + ':' + region_key)`; `photo_masters` grows `region` +
    `region_key` and its global uniques on `master_path` / `sha256` become
    per-region — three unique constraints made the spec as written unstorable.
    The parent keeps the real filename (children get `#pN`) so re-ingest is a no-op.
  - **Analysis stores a plan, not 8–10 GB of derivatives.** A ~2000 px preview per
    proposal; the full-resolution render is cut on demand at 1:1 zoom and at Accept
    with exactly the ticked ops.
  - **Tombstones.** Push now sends a photo the web already holds but that has since
    been junked / made private / soft-deleted once more with `is_deleted = true`
    (`photos.tombstoned_at` is the marker). This fixes a standing gap for every
    dedupe loser and triage-to-junk since Phase 14, not just split parents.
  - Scope is `is_scan` (so the 19 scans under the digital root are included) minus
    back-shaped photos; `needs_manual` proposals stay in the queue with the
    geometric ops disabled; splits are never bulk-accepted; undo is session-scoped
    (cross-restart recipe in `GC.md`); `detect_faces` is marked done on split
    children that inherited faces; pHash/dHash are recomputed at Accept rather than
    flagged stale; every threshold lives in `desktop/.env`.
  - **Face boxes keep their width and height**, with the centre mapped exactly
    through the affine — exactly invertible, and truer than growing the box to the
    rotated corners' bounds, since the face rotates with the image. Only honest for
    small angles, hence `CLEANUP_MAX_DESKEW_DEG` → `needs_manual`.
  - **File moves sit inside the accept transaction**, unlike Triage's after-commit
    rule: a committed `file_version` bump with no bytes is unrecoverable, while a
    failed triage move is not.
- Found while building: a **black scanner bed masked in as one print covering the
  whole scan**, because HSV saturation is `(max-min)/max` and a near-black pixel
  with scanner noise reads as highly saturated. The colour-is-print clause now has
  a brightness floor.
- Two more found by the GUI tests: replacing a running `BackgroundJob` reference
  let Qt destroy a live thread (now parked until `finished` fires, and joined on
  close), and a preview render landing wiped the last decision off the status bar
  (now two halves, the decision persisting — the Phase 3 no-flash-messages lesson).
- Also hardened on the way through: `check_working_files` now restores a missing
  working file from `WORKING_DIR/_versions/` before falling back to the master,
  and a cleaned photo rebuilt from the master is reported as
  `cleanup_version_lost` rather than silently reverted.
- Found by the first real run on batches 1–5: every multi-print scan was also
  flagged `needs_manual: print_too_small`, because the whole-scan size gate was
  judging the largest single print. The split gates now run first and exempt it.
- Also corrected: invariant 3's "a 16-bit TIFF stays 16-bit" holds for
  *greyscale* only — Pillow has no 16-bit RGB mode at all, so colour is reduced
  to 8 bits once on load instead of raising at save time.
- **Memory was the fifth bug, and the worst.** A preview render loaded the full
  image and put it through float32 copies — 1.1 GB for one copy of the 93.7 MP
  scan — and the first report run was killed for low memory. Both tonal ops now
  compose into one LUT per channel (mapped in place when the array is a warp
  result nobody else holds), and previews reduce the source *before* the warp
  via `Transform.scaled_by`. Measured on that 93.7 MP scan: preview **7 MB**
  peak (was ~1.5 GB), accept-time full render 188 MB, analysis 75 MB. The LUT is
  within one level of the direct arithmetic and rounds once instead of twice.
- Sixth: re-analysing a `clean` photo added a second `clean` row instead of
  superseding the first, double-counting it in the report. `supersede_pending`
  now covers `clean` too; decisions are still never superseded.
- **Invariant 2 verified on real data.** Photos 169 (4 labelled faces) and 120
  (2) accepted with deskew+crop, then every moved box's crop compared against
  the same face cut from the kept `_versions/` copy with its old box: mean
  difference **2.7–4.3 / 255**, i.e. JPEG noise. `diagnose_face_box` on both
  reports "DB dims agree with file display dims". Pairs written to
  `CLEANUP_DIR/_report/face-check/`.
  (A first pass at that check compared `previous_value.working_path` against
  `new_value.working_path` — the *same* path, since the working name never
  changes across an accept — and appeared to show the boxes slipping. The kept
  `_versions/` file is the only handle on the old pixels.)
- **Colour estimator changed after the batch 1–5 review (PM, 2026-09-27).** The
  answer-13 estimator (grey-world over every mid-tone) over-corrected: it fired
  on 270 of 444 scans, median shift 23 levels, with the worst cases pinned at
  both gain clamps — and photo #27 showed it turning winter grass cyan. Against
  an estimator restricted to the least-colourful 40 % of mid-tones, 152 of the
  270 were overstated more than twofold and 113 would lose the colour op
  entirely. Now `CLEANUP_CAST_NEUTRAL_PCT=40` (100 restores grey-world);
  `cast_gains` shares the selection; `grey_world_magnitude` is recorded on every
  proposal for comparison. "Answer 13 described a measurement, not a mandate —
  over-correction is exactly what the review was for."
- **Fix-up 1 (2026-09-27)** — George's verdict on the re-analysed sheet: #27
  better, #166 much better, **#8 worse**, **#19 a white shirt turned blue**. The
  neutral-mid-tone estimator was still fooled by prints whose *subject* is one
  colour. A cast now has to show on the **paper** as well: the highlights (top
  3 % of luminance, blown pixels excluded) must point the same way as the
  mid-tones and be at least half their magnitude, the correction uses the
  smaller of the two readings, gains are blended toward 1.0 by
  `CLEANUP_CAST_STRENGTH=0.7`, and a white-point guard scales anything back
  that would push the paper further from neutral. Colour proposals on batches
  1-5: 270 -> 168 -> **50** (49 rejected as "highlights cast the other way",
  25 as "paper white is clean").
  Diagnosis that drove it - **#8**: mid-tone cast 10.3 but paper white
  242.8/239.0/238.0, magnitude 1.5, ratio 0.14 -> the room was warm, the print
  was not. **#19**: mid-tone 1.7, highlights 3.1 pointing the *other* way
  (already slightly blue at 233/233/239) -> correcting it is what turned the
  shirt blue. Worth recording: **#19 already had no colour op** under the
  neutral estimator (1.7 is below the 6.0 threshold; its only op was
  `crop 23%`) - the blue shirt was on the *first*, grey-world sheet, which read
  15.3 and proposed R-18 G+4 B+19. Two mechanisms now agree on it rather than
  one having been needed; **#8** is the case fix-up 1 was genuinely required
  for. The highlight rule also overturned my own reading of **#106**, which I
  had called a genuine cast on both earlier estimators (43.3 -> 33.5): its
  paper white measures 3.0, so it was scene colour too.

- **Fix-up 2 (2026-09-27)** — George's verdict on the fix-up 1 sheet: #8 fine,
  #25 added red to a print that already had an orange tint, #306's deskew
  **tilted the image**. Two changes.
  **(a) Tonal ops opt-in.** `CLEANUP_COLOUR_ENABLED` / `CLEANUP_LEVELS_ENABLED`
  default false; the analyser still measures both and records them under
  `ops_disabled`. The queue is geometry only. All of fix-up 1's code and tests
  stay — this is a switch, not a revert. The 122 existing pending proposals had
  their tonal ops moved to `ops_disabled` rather than being re-analysed.
  **(b) Deskew direction.** Diagnosed first: on #306 `minAreaRect` returned
  centre (359, 513), size 1055x709, angle -87.728 -> +2.272 after
  normalisation, with three of its four corners *outside* the 732x1028 image.
  The mask is not a rectangle — a torn bottom-right corner and edges running
  off the scan — so the minimum *enclosing* rectangle is pinned by the
  outliers, not the print. Not a sign-convention bug: the wrong **source**.
  The angle now comes from the outline's length-weighted edge consensus
  (`edge_orientation`), which reads +0.038° on #306, and a confidence floor
  blocks the deskew when nothing wins the vote. Wrong-direction deskews across
  batches 1-5: **10 of 41 -> 0 of 45**; 7 are now skipped as "print edges
  disagree". Worst offender was #304 (proposed -3.761° on a print tilted
  +0.389°, edge confidence 0.17).
  A second hole closed: the deskew test asserted `abs(abs(angle) - 3.0) <= 0.3`
  — `abs` twice — so a sign error passed. Tests now check direction and
  re-measure the residual tilt after applying the rotation, and there is a
  real-scan fixture (`tests/fixtures/scan_306_straight.jpg`, 299x420) that
  still reproduces the misleading enclosing rectangle.
  Batches 1-5 after both changes: 389 pending, 66 clean, 8 needs_manual,
  2 splits; ops crop 372, deskew 45, split 2.

- **Fix-up 3 (2026-09-27)** — #15's crop removed the left side of a person.
  Diagnosed: the print mask keyed on "darker than bed - 25", so a white curtain
  and a grandmother's white cardigan read as bed; the rectangle started at
  x=383 in a 2000-wide frame against a true edge of x~95 (column profile: bed
  std 1.19-1.55 at x=0-80, print std 7.5-20 from x=100). Two lines of defence:
  a **calm** print mask (near the bed tone AND locally flat AND reachable from
  the scan border) and a **content guard** that pushes every crop edge outward
  until what lies beyond it is genuinely bed.
  Two deviations from the brief, both approved: the specified rule-2
  conjunction ("inside busy AND outside not bed") would not have caught #15 —
  its inside strip is smooth white fabric, energy 18.7 against a 38.6 interior
  — so outside-is-not-bed drives the push alone; and a fixed calmness threshold
  cannot work, because genuine bed runs std 1.2 to 10 across the archive while
  #15's curtain sits at 10.5. Calmness is judged against each scan's own bed.
  The PM's calm-mask pilot initially failed on #15 (edge at 379.6) — **the
  window has to scale with the image**: 7 and 11 px put it at 379, 15 px and up
  put it at 85-91. Now 0.013 x long edge. A 520 px fixture passed while the
  2000 px frame failed, which is the trap.
  Two bugs found by the tests: the guard pushed 36.8 px on **every** edge of a
  clean synthetic scan (the first strip straddles the print's antialiased
  boundary — it now starts one depth out), and edges already at the scan
  boundary were counted as unresolved, which would have sent 356 of 455 scans
  to needs_manual.
  Batch check: crop edges inside the picture **30 of 455 -> 12**; edges
  misplaced at all **106 -> 31**; #15 no longer appears. Crop proposals fell
  372 -> 111, which the stored rectangles justify: of the 325 now-clean scans,
  **323 leave a bed margin of 0.00%** (p99 0.04%) - the print reaches the scan
  edge, so there was never bed to crop - while the 130 pending ones have real
  margins (median 4.84%, p90 13.9%). #15 now removes 18% instead of 36% with
  the grandmother intact.
  Batches 1-5 after: 130 pending, 325 clean, 7 needs_manual (4
  print_edge_unclear), 1 split; ops crop 111, deskew 41.

### Phase 8 — Web: auth (Claude Code, Windows) — spec §7
- Express + EJS. Request-access form → admin email with Approve/Deny (POST-confirm pages, 72 h tokens) → magic links → long-lived HTTP-only session in Postgres.
- Roles admin/contributor; suspension kills sessions, keeps contributions. Service-account token for the desktop app.
- Accept when: full flow works locally with Postmark in dev-log mode; GET on a token link changes nothing; suspended user's session is dead on the next request.
- **Done 2026-09-08.** All 22 tests green (`npm test`). Manual dev-mail smoke: request-access → admin email → POST approve → welcome email → magic-link sign-in. Audit rows produced in order: `auth.request_access` → `auth.approve` → `user.create` → `auth.magic_link.sent (welcome_after_approval)` → `auth.login` → `auth.magic_link.expired_attempt` on replay. Migration 22 adds `access_requests.display_name`. Env additions: `TEST_DATABASE_URL` (shares `photoorg_test`), `BASE_URL`, `ADMIN_EMAIL`, `SESSION_SECRET`, `SERVICE_TOKEN`, `POSTMARK_*`.

### Phase 9 — Web: core API + sync (Claude Code, Windows) — spec §8
- Photos, suggestions, faces, people, comments, likes, albums, audit log, monthly activity report.
- Sync endpoints: push (skips `is_private`, uses `file_version`), pull confirmed values, **pull approved contributions** (file bytes + metadata, marks them pulled). Resumable. Desktop Sync mode wired to them, including the append-only write into `D:\Contributed`.
- **Groups**: `groups`, `group_members` (user, group, role member|moderator), `photo_groups`; visibility middleware applied to every photo list/detail/image/face/search route (`visible_to(user)` = shares a group, or admin); `requireModerator(group)`; moderator endpoints (hide comment, remove photo from group, add member, approve contribution into own group); admin group CRUD; admin bulk assign/unassign; unfiled queue. Sync pushes photo-group assignments from the desktop and pulls web-side changes.
- Contributions API: `contributions` and `contribution_files` tables; chunk-free per-file upload endpoint with sha256 pre-check (`HEAD`) so clients skip what the server has; admin list/approve/reject; duplicate warnings computed on upload (sha256 exact, pHash near).
- Accept when: desktop pushes a 200-photo sample and a re-push sends 0; a private photo is absent from the VM's disk and DB; audit rows exist for every state change in a scripted run.
- **Done 2026-09-14.** All web tests green (61/61) and desktop pytest green (188/188 + 1 skipped). Two new migrations (`phase-9-groups`, `phase-9-contributions`) plus a fix-up to `photo-back-orphan` migration `down` (aborts on orphan rows rather than deleting). Web routes cover `/media/*`, `/api/photos`, `/api/people`, `/api/albums`, `/api/relationships`, contributor writes, admin suggestions/disputes/audit/report/rescan-list/unfiled, groups (with 20 k-cap bulk assign), full `/sync/*` surface, contributions API + minimal `/upload` and `/admin/contributions` pages. Desktop `sync/` mode (push, pull confirmed, pull contributions with append-only manifest check, groups bulk-assign panel). Web `PHOTO_DIR=C:\Photos-web-local\` on the laptop for local end-to-end. Shared `SERVICE_TOKEN` recorded in `GC.md`. Contrib master root `D:\Contributed|contrib` in `MASTER_ROOTS` with icacls deny on the root only so `_incoming/` stays writable.

### Phase 10 — Web: pages (Claude Code, Windows) — spec §10, EJS instead of React
- Group switcher in the header (All my groups / one group); every page respects visibility.
- Browse grid (infinite scroll), photo detail (tag box, date field, like, comments, back + transcription), person page, albums, admin screens, "needs attention" feed.
- **"Who is this?"** — faces George marked `unknown` in the desktop are surfaced to contributors (on the photo page and in a dedicated feed); a contributor's answer becomes a `person` suggestion for admin acceptance.
- **Upload page**, mobile-first (group picker limited to the uploader's groups): camera / gallery picker on phones; drag-drop and folder picker (`webkitdirectory`) on desktop; sequential uploads with per-file progress, retry, and resume; batch note. **Admin approval page**: thumbnails, uploader, dup warnings, approve/reject per file or batch.
- Mobile-first layout throughout; desktop widens the same pages.
- Plain JS only: grid loader, drag-to-tag box, name autocomplete, date parser.
- Accept when: George tags a face and suggests a date from a phone in under 10 seconds each; admin accepts both and the photo's completeness rises.
- **Done 2026-09-17** — George's phone and desktop pass accepted. During the pass fail2ban banned George's IP (admin Browse requested thumbnails for metadata-only photos → 20 × 404); fixed with 200 placeholder images and `has_file` in lists (rule now in CLAUDE.md). Browse + needs-attention strip, photo detail with tagger / date field / like / comments / back, people, albums (read-only), small search, Who is this?, upload + My uploads, full admin area with moderator subset; header group switcher. Shared query layer in `web/services/`; composite keyset cursors; `/media/display` and on-demand face crops; back images now pushed. Web tests 127, desktop 201 + 1 skipped. Lighthouse (mobile, local): accessibility 100, CLS 0 on `/` and a photo page.

### Phase 11 — Search (Claude Code, Windows) — spec §9
- Names incl. maiden and suffix (Jr./II/III — `people.suffix`), nickname table, Metaphone, full text over comments/descriptions/transcriptions, tolerant date ranges, places, attention filters.
- Accept when: "Peggy" finds Margaret; "Schmitt" finds Schmidt; a decade-only photo appears in its decade search.
- **Built 2026-09-17** (George's five searches pending). Two trigger-maintained tables (`photo_search`, `person_search`) plus `place_aliases`, all SQL so desktop and VM stay correct with no app code; Phase 1's `search_tsv` / `search_key` dropped. One parser (`services/search-parse.js`), one service (`services/search.js`): terms AND-ed, layers OR-ed, `why` per hit, relevance + Browse sorts on composite cursors, header autocomplete. Phonetic hits guarded by trigram ≥ 0.3 or a shared 4-letter prefix. Deferred-sweep escape hatch for bulk writers (off by default; trigger cost measured at 4.8 s / 10 000 rows). Web tests 148, desktop 201 + 1 skipped (unchanged — the migration is side-neutral). Deployed to the VM 2026-09-17: migration 4.7 s over 13 257 photos, worst query `Clay` 121 ms, smoke searches all 200 in the caddy log, `/sync/status` id_floor still ok.

### Phase 12 — Download and export (Claude Code, Windows) — spec §11
- Background zip job with emailed link; Year/Month tree rendered from the DB; Unsorted; optional People tree; backs and private excluded.
- Admin full export (images + `pg_dump`).
- Accept when: a 500-photo zip has the right tree and no backs; whole-archive job runs to completion without the API blocking.

### Phase 13 — Metadata writer (Claude Code, Windows) — spec §12
- Writes confirmed date, people (full names incl. suffix in description, common name in keywords), place/GPS, AI description marked auto-generated, back transcription, and the physical reference (`Batch 00012 #017`, original filename) into XMP so it survives outside this system. Working copies only, asserted in code. Temp-file + atomic replace. Dry-run.
- Accept when: dry-run report matches what exiftool reads back after a real run; an attempt to point it at a master raises.

### Phase 14 — Deploy to the VM (George + Claude Code, Windows) — new
- Disk resize, DB + role, `.env`, Caddy site file, systemd unit, log path, Postmark domain verification, nightly `pg_dump`, fail2ban whitelist. Documented in `GC.md`.
- Can be done right after Phase 9 so sync has a real target; pages and search deploy incrementally after.
- Accept when: site answers over HTTPS, desktop sync to the VM succeeds, backup cron has produced one file.
- **Done 2026-09-16.** `https://cyberdinosaurs.com` live, TLS OK, admin bootstrapped, full email round-trip via Postmark. Root FS 30→80 GB. Systemd + Caddy + logrotate + sudoers + cron all installed (templates in `ops/vm/`). Metadata for the full keep set + files for one test group (Clay Family / Album 6) pushed and idempotent (second push 0 files). `/sync/status` matches the laptop.

## 5. Open items for George
- **Candidate fix-up 12 — shared VLM inbox on the mini.** classify, describe and estimate_date all upload the same 1024-px JPEGs to three per-job inboxes (3 × ~2.5 GB, ~40 min each). One shared inbox with per-job result files, swept only when all three results are collected, would make it one upload. Do it before the next model / prompt-version rerun; not while a queue is running. (2026-09-16)

1. Create the GitHub repo and push the skeleton (Phase 0).
2. Confirm the masters have a backup.
3. ~~Pick the archive's domain name~~ — cyberdinosaurs.com.
4. Move the inference service to the M6 (config change + model re-download). M6 arrived and being set up 2026-09-28.
5. ~~Hand over `classify`, `describe`, `estimate_date` to the mini~~ — all three complete and collected (confirmed 2026-09-29).
6. Bulk-assign the ~12,800 unfiled photos to groups from the desktop Groups panel.
7. Full 28 GB file push once Phase 7 cleanup and the mini jobs are done (uncheck `files_only_for_grouped`).
8. UptimeRobot monitor on `https://cyberdinosaurs.com/healthz` — no hurry.
9. Off-site backup of the VM's nightly pg_dump (S3) — no hurry.
10. Refresh the fail2ban whitelist IP in GC.md if the ISP changes it.
11. Album editing on the web needs album changes pulled back to the desktop (Phase 10 shipped albums read-only) — Phase 12. `place_aliases` (Phase 11) needs the same treatment: no sync route yet.
15. **Backs are out of scope for Phase 7 cleanup** (639 back-shaped scans excluded, same rule Dedupe uses). Deskew/crop for backs needs its own pass — the transcription is already captured, so this is cosmetic; schedule after Phase 12.
16. Cleanup's cross-restart undo is manual (from `WORKING_DIR/_versions/` + the `cleanup.accept` audit row); the recipe is in `GC.md`. A UI for it only if George ever wants one.
12. ~~Phase 10 phone checklist~~ — done 2026-09-17.
17. **The VM has not run `phase-7-cleanup`** (its last migration is `phase-11-search`), so `photo_masters.region` / `region_key` and `photos.tombstoned_at` don't exist there and `/sync/photo_masters` answers 500 on every push. Everything else now pushes past it (fix-up 2), but masters metadata, split children and tombstones cannot reach the site until Phase 7's migration and the current web code are deployed. George's call on when, since it is a production deploy: `git pull && npm ci && npm run migrate:up` in `/home/ubuntu/photoorg/` then `sudo systemctl restart photoorg`. Also worth checking: the `photoorg` journal has no entries since Sep 17, so the service's stdout is going somewhere other than journald and 500s leave no trace.
14. Search name strip shows phonetic-only people for ordinary words ("Christmas" → Christina). Harmless (score 50, never displaces hits); if it annoys anyone, hide phonetic-only people from the strip when the same term produced full-text hits. Phase 11 fix-up when convenient.
13. ~~**Resolved 2026-10-02 (Phase 7 fix-up 7).**~~ Root cause: ingest parsed exifread's `Rotated 90 CW` against an exiftool-vocabulary table, so `orientation` was NULL for every photo ever ingested; the fix-up 6 backfill had nothing to key on. Now: 8,672 orientations backfilled, 235 photos' dims corrected, 280 boxes rescaled, 6 cleanup-mapped boxes fixed (Samara restored), `check_working_files` post-condition = 0. Needs one push to reach the site. Original: 235 photos (EXIF 6/8, `orientation` NULL, raw dims stored) slipped past the fix-up 6 backfill; 280 face boxes in the wrong frame, 95 labelled. Fix-up 7 in the Phase 7 prompt repairs them. Original note — check `photos.orientation` on the desktop: Code found no photo with orientation set in the 408-photo local copy. Either the sync omits the column or the fix-up 6 backfill missed. `select orientation, count(*) from photos group by 1` on `photoorg`; phone photos should show 6/8 as well as 1.

## 6. Risks

- Volume: ~17k files means every review UI must be keyboard-first and resumable, or George will not finish. Triage exists to shrink the set before faces and cleanup run.
- Inference throughput: 5–15 s per image × ~10k keep-set images is days, not a night. Batch runner must be restartable and jobs prioritised (backs first, then faces, then describe, then date).
- Disk: full-resolution working copies plus derived versions plus thumbnails. Budget 2× the masters' size on the laptop and on the VM.
- Scanning continues: ingest, triage, and sync are all incremental by design; new batches are just another run.
- **Id-range partitioning (Phase 9 fix-up 1).** Desktop-born rows use low ids, web-born rows start at `WEB_ID_FLOOR`. George's DBA instinct: this will bite eventually. Known failure modes: a third writer appears (second web instance, a script inserting directly); a DB restore or `setval` resets the VM sequences below the floor; an int4 table gets close to the floor; someone forgets the rule. Mitigations in place: one shared constant, sync refuses to cross the floor in both directions, tests seed overlapping ids. Escape hatch if it bites: add `origin text` + `origin_id` columns and migrate to UUID/ULID primary keys — contained because every cross-machine id passes through the sync layer. Check `/sync/status` sequence positions after any VM restore.
