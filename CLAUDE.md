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

## Web (CraftTags lessons — always apply)
- Token links land on a POST-confirm page. GET on the token changes nothing.
- `app.set('trust proxy', 1)` before any middleware that reads client IP.
- Register specific routes before wildcards.
- Watch fail2ban when smoke-testing from a new IP.

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
