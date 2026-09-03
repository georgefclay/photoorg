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
