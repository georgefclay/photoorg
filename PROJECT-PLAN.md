# Family Photo Archive — Project Plan

Owner: George Clay. PM: Claude (this Cowork chat). Coding: Claude Code on the Windows laptop and on the Mac mini.
Spec: `photo-archive-build-prompts.md` (the 12 build prompts). This file records what changed after looking at the real data, the decisions made, and the phase plan. Phase prompts are written one at a time from this file when each phase starts.

Last updated: 2026-09-01

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
| Email | Postmark, new sender signature for the archive's domain. Domain TBD before Phase 8. |
| Inference hardware | Develop on the M4 Mac mini (16 GB) now; move to the M6 (32 GB) on 2026-09-22. Model, host, and quantization are config, not code. |
| Face embeddings | InsightFace (ArcFace) via ONNX. VLM is never used for identity. |
| Privacy | `is_private` flag on photos. Sync skips private rows and files; the API refuses to serve them even if present. |
| Triage | New desktop mode: keep / junk / private, keyboard-driven, with AI pre-sort once inference exists. Runs before dedupe. |
| Folder hints | Ingest stores `source_root`, `source_folder`, `source_filename`, `scan_batch`. Named folders create a virtual album of the same name and a low-confidence event/date suggestion. `_YYYY-MM` becomes a capture-date suggestion with precision `month`, source `import`. Folder name is also written to the working copy's XMP by the metadata writer (Phase 13). |
| Physical location | Every scan keeps a **physical reference**: `scan_batch` (the source folder name, e.g. `Batch 00012` or `Chuck and Lola Wedding`) plus `scan_sequence` (its order within that folder by filename) and `source_filename`. This is how George finds the physical print. It is immutable, shown on the photo detail page and in every desktop review view, searchable, written to the working copy's XMP, and included in the download zip's metadata. When dedupe keeps a phone original over a scan, the scan's physical reference is copied onto the keeper (`physical_ref_note`) so the print can still be found. Backs inherit the front's reference. |
| Rescans | Prints live in envelopes of ~50 per batch in roughly scan order, so batch + approximate sequence is enough to find one. George will rescan important prints at 600/1200 DPI to TIFF later. A rescan is **not a new photo**: ingest matches it by pHash to the existing scan (or George picks the original in the pairing grid), the new file becomes the preferred master for that photo row, the old file is kept as a prior version, and every tag, date, face, comment, and like stays attached. A `rescan_wanted` flag on photos, settable from the desktop and the web (admin), feeds a **Rescan list** report grouped by batch envelope so George can pull them in one pass. |
| Growing archive | The archive is never "done". Master roots are a configurable list (`label=path[:kind]`), so a new drive, a new scan folder, or a phone export can be added in Settings. Ingest, triage, AI jobs, cleanup, sync, metadata writing, and downloads are all incremental per photo; re-running any of them touches only what is new or changed. New material is added by dropping it into a master root (or adding a root) and pressing Ingest. |
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

### Phase 7 — Scan cleanup (Claude Code, Windows) — spec §5
- Deskew, crop, multi-print split, colour-cast and fade correction. Always a new derived file; `file_version` bumps so sync re-pushes.
- Review queue: accept / reject-to-manual-fix / remote enhance. Provider interface; Claid + null implementations; cost counter.
- Accept when: 50 scans processed and reviewed; rejected ones sit in `manual-fix/`; master hashes unchanged.

### Phase 8 — Web: auth (Claude Code, Windows) — spec §7
- Express + EJS. Request-access form → admin email with Approve/Deny (POST-confirm pages, 72 h tokens) → magic links → long-lived HTTP-only session in Postgres.
- Roles admin/contributor; suspension kills sessions, keeps contributions. Service-account token for the desktop app.
- Accept when: full flow works locally with Postmark in dev-log mode; GET on a token link changes nothing; suspended user's session is dead on the next request.

### Phase 9 — Web: core API + sync (Claude Code, Windows) — spec §8
- Photos, suggestions, faces, people, comments, likes, albums, audit log, monthly activity report.
- Sync endpoints: push (skips `is_private`, uses `file_version`), pull confirmed values. Resumable. Desktop Sync mode wired to them.
- Accept when: desktop pushes a 200-photo sample and a re-push sends 0; a private photo is absent from the VM's disk and DB; audit rows exist for every state change in a scripted run.

### Phase 10 — Web: pages (Claude Code, Windows) — spec §10, EJS instead of React
- Browse grid (infinite scroll), photo detail (tag box, date field, like, comments, back + transcription), person page, albums, admin screens, "needs attention" feed.
- Plain JS only: grid loader, drag-to-tag box, name autocomplete, date parser.
- Accept when: George tags a face and suggests a date from a phone in under 10 seconds each; admin accepts both and the photo's completeness rises.

### Phase 11 — Search (Claude Code, Windows) — spec §9
- Names incl. maiden, nickname table, Metaphone, full text over comments/descriptions/transcriptions, tolerant date ranges, places, attention filters.
- Accept when: "Peggy" finds Margaret; "Schmitt" finds Schmidt; a decade-only photo appears in its decade search.

### Phase 12 — Download and export (Claude Code, Windows) — spec §11
- Background zip job with emailed link; Year/Month tree rendered from the DB; Unsorted; optional People tree; backs and private excluded.
- Admin full export (images + `pg_dump`).
- Accept when: a 500-photo zip has the right tree and no backs; whole-archive job runs to completion without the API blocking.

### Phase 13 — Metadata writer (Claude Code, Windows) — spec §12
- Writes confirmed date, people (full names in description, common name in keywords), place/GPS, AI description marked auto-generated, back transcription, and the physical reference (`Batch 00012 #017`, original filename) into XMP so it survives outside this system. Working copies only, asserted in code. Temp-file + atomic replace. Dry-run.
- Accept when: dry-run report matches what exiftool reads back after a real run; an attempt to point it at a master raises.

### Phase 14 — Deploy to the VM (George + Claude Code, Windows) — new
- Disk resize, DB + role, `.env`, Caddy site file, systemd unit, log path, Postmark domain verification, nightly `pg_dump`, fail2ban whitelist. Documented in `GC.md`.
- Can be done right after Phase 9 so sync has a real target; pages and search deploy incrementally after.
- Accept when: site answers over HTTPS, desktop sync to the VM succeeds, backup cron has produced one file.

## 5. Open items for George

1. Create the GitHub repo and push the skeleton (Phase 0).
2. Confirm the masters have a backup.
3. Pick the archive's domain name before Phase 8.
4. On 2026-09-22, move the inference service to the M6 (config change + model re-download).

## 6. Risks

- Volume: ~17k files means every review UI must be keyboard-first and resumable, or George will not finish. Triage exists to shrink the set before faces and cleanup run.
- Inference throughput: 5–15 s per image × ~10k keep-set images is days, not a night. Batch runner must be restartable and jobs prioritised (backs first, then faces, then describe, then date).
- Disk: full-resolution working copies plus derived versions plus thumbnails. Budget 2× the masters' size on the laptop and on the VM.
- Scanning continues: ingest, triage, and sync are all incremental by design; new batches are just another run.
