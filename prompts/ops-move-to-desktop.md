# Ops — move development from the laptop to the desktop

2026-11-09. D: is an external drive; the desktop already has Postgres, Python,
Node and git installed. Goal: the desktop becomes the one development machine
and the laptop retires as a cold backup. George runs Phase A in a Claude Code
session ON THE LAPTOP, moves the drive and the secrets by hand, then runs
Phase B in a new session ON THE DESKTOP with this same file.

**The one hard rule: at no moment may two live `photoorg` databases both look
authoritative.** This project already paid for a wrong-database incident
(Phase 9 fix-up 2). Phase A ends with the laptop's databases renamed; nothing
on the laptop must be able to push again. Do the phases in order, completely.

If the Phase 13 fix-up 3 rewrite or George's push has not happened yet when
this runs, that is fine — move first and run them on the desktop, which has
the RAM the writer keeps running out of. Just start Phase A at a quiet point:
no Code session mid-fix-up, app closed.

## Phase A — on the laptop
1. `git status` clean (commit or stash anything pending, push to GitHub).
2. Final sync: a pull, then a push to the VM with 0 failed stages. Record the
   `/sync/status` counts in this file under Notes below.
3. Fresh dump: `pg_dump -Fc photoorg` →
   `D:\PhotoArchive\migration\photoorg-final-<date>.dump` (create the
   folder). Size-check it.
4. Retire the laptop copies: `alter database photoorg rename to
   photoorg_retired_<date>`; same for `photoorg_web`. (Rename, not drop — it
   is the cold backup. The desktop app and push die against a missing DB by
   design.)
5. Confirm nothing scheduled on the laptop references the project (nothing is
   known to).
6. Safely eject D:.

## George, by hand (never through git or cloud)
- Copy `GC.md`, every tier's `.env` (`desktop/`, `web/`, `shared/` — whichever
  exist), and the VM SSH key to the desktop by USB stick.
- Plug D: into the desktop and, in Disk Management, **assign it the letter
  D:** — every working path in the database and the config is absolute.
- Repo access: make sure the desktop's git has his GitHub credentials.

## Phase B — on the desktop
1. Clone the repo to `C:\Programming\Photos` (same path as the laptop, so
   every doc and config stays true). Put the hand-carried `GC.md` and `.env`
   files in place; `git status` must show them ignored.
2. Postgres: create the role per `GC.md`, `createdb photoorg`, `pg_restore`
   the dump from `D:\PhotoArchive\migration\`. `migrate:up` must be a
   no-op at head. Counts match the Phase A notes (photos 12,686, etc.).
3. Recreate `photoorg_web` exactly by the documented laptop procedure
   (`PHOTOORG_DB_ROLE=web` in the migrate environment so the id-floor
   sequences move) and the test DBs via `test:setup`. Recreate
   `C:\Photos-web-local\` for the local web `PHOTO_DIR`.
4. Desktop venv from requirements; exiftool present (the writer is verified
   on 13.59); `npm ci` in `web/` and `shared/`.
5. D: integrity: `check_working_files --dry-run` post-conditions all 0. The
   `icacls` deny on the `D:\Contributed` root references the laptop's
   accounts — verify it binds on this machine's accounts and re-apply the
   documented deny if the SIDs are orphaned; `_incoming/` stays writable.
6. Full test suites: desktop `pytest` and web `npm test` green.
7. The mini: the inference client's host resolves and answers from the
   desktop (a status poll is enough; no job needed).
8. Sync from the new machine: pull, then push — the shared-DB guard's
   `/sync/status` identity check passes, metadata full-send, 0 failed stages,
   and the file count is whatever is genuinely due (the cursors travelled on
   D:, so no re-delivery storm is expected; say what you saw).
9. GUI: `run-desktop.bat` opens; Sync tab lists the groups; the metadata
   panel's due count reads as expected for wherever the writer queue stands.
10. VM access: ssh per `GC.md` and read `git log -1` in the deploy directory
    — read-only, no deploy.
11. Update `PROJECT-PLAN.md` (Progress entry: dev moved to the desktop, the
    laptop's databases renamed `photoorg_retired_<date>`, dump kept on D:)
    and `GC.md`'s machine notes; commit and push:
    `Ops: development moved to the desktop`.

## Afterwards (George)
- Link Cowork sessions to the desktop from now on (Claude desktop app there,
  add the `C:\Programming\Photos` folder).
- If the scanner stays on the laptop, decide where new scans land — they must
  end up on D:, which now lives at the desktop; scanning at the desktop is
  simplest.
- Keep the laptop's renamed databases a few weeks, then drop them.

## Notes (filled in during the run)

### Phase A — laptop, 2026-11-15
- **A1.** Pending plan/prompt docs (Phase 9 fix-ups 3–4 text, Phase 16
  backlog, this runbook, `.gitignore` `Claude outputs/`) committed and pushed;
  tree clean, `main` = `origin/main`.
- **A2.** Pull (groups, confirmed, contributions — the Sync tab's three
  buttons, headless): groups 0 / members 0 / photo_groups 4 (the "Testing 2"
  removal), facts 0, contributions 0 / files 0. Push (`tools/run_push.py`,
  grouped files only): exit 0, **0 failed stages**, 13,294 photos upserted,
  0 files sent, 297 s. Per table: album_photos 253, albums 6, faces 23,464,
  people 118, photo_backs 845, photo_groups 4, photo_masters 13,537,
  photo_places 3, suggestions 34,849; the rest 0.
  `/sync/status` after: albums 7, people 118, photo_groups 143,
  contribution_files 4, contributions 1, places 3, groups 1, group_members 2,
  photo_backs 845, photos 13,310, faces 23,464; `id_floor.ok` true; web DB
  identity `photoorg` / 7648012335739445998.
  The pull marks the "Testing 2" photos writer-due: the metadata write + push
  that scrubs them is for the desktop.
- **Laptop `photoorg` at dump time** (Phase B compares against these):
  system_identifier 7595436362122079188; migration head
  `1793369363869_phase-12-download-export`; photos 14,470 (live keep/private
  **12,690**), photo_masters 14,570, photo_backs 850, faces 23,464, people 118,
  person_name_variants 0, suggestions 34,901, albums 7, album_photos 258,
  places 3, place_aliases 0, photo_places 3, photo_groups 143, audit_log
  129,078, job_runs 55; 261 MB on disk. (The "photos 12,686" in B2 is the old
  live figure — 12,690 is today's.) Sync cursors are in
  `D:\PhotoArchive\sync-state` and travel with the drive.
- **A3.** `D:\PhotoArchive\migration\photoorg-final-2026-11-15.dump`, custom
  format, **56,300,575 bytes**, 575 TOC entries; `pg_restore -f NUL` decodes
  the whole archive (exit 0). Note: `DATABASE_URL`'s password is
  percent-encoded — decode it before handing it to `PGPASSWORD`.
- **A4.** `photo_user` owns the databases but has no CREATEDB, which
  `ALTER DATABASE … RENAME` needs, and the `postgres` superuser password is
  not recorded in `GC.md`, so George ran the renames as `postgres`. Both
  first failed with "being accessed by other users": **pgAdmin 4** held a
  session on each (and reconnected after a disconnect); quitting it fully
  let them through. Result: `photoorg_retired_20261115` (oid 21554, 261 MB)
  and `photoorg_web_retired_20261115`; `photoorg_test` left as is (not
  authoritative). `tools/run_push.py --dry` now dies with `PoolTimeout` —
  nothing on the laptop can push.
- **A5.** Task Scheduler (non-Microsoft tasks), both Startup folders and
  HKCU Run: nothing references the project.

### Phase B — desktop (`DESKTOP-2DP4KF5`), 2026-11-16
- **B1.** Done by copy, not clone (2026-11-15 PM, robocopy over RDP from
  `\\tsclient\C\Programming\Photos`, excluding `node_modules`, `.venv`,
  `__pycache__`, build output; `.git`, `GC.md` and the three `.env` files came
  across). The SSH key came across but not the `github-photoarchive` alias, so
  the `Host` block was added to `~/.ssh/config` (backup `config.bak-20261116`).
  `git fetch` → the laptop's A1 commits; `git diff --ignore-cr-at-eol
  origin/main` was empty for tracked files and the two untracked prompts
  matched origin apart from this note, so `git reset --hard origin/main`
  (`946dfdd`). `GC.md` and all `.env` files are ignored.
- **B2.** The desktop runs **Postgres 18.3**, not 16 (the dump is 16.11;
  `pg_restore` 18 reads it). No `photo_user` existed: created as `postgres`
  (login, no CREATEDB, password per `GC.md`), then `photoorg`, `photoorg_web`,
  `photoorg_test` owned by it. `pg_restore --exit-on-error` exit 0 in 21 s.
  **Every count matches A2's dump-time list exactly** (photos 14,470, live
  keep/private 12,690, photo_masters 14,570, photo_backs 850, faces 23,464,
  people 118, person_name_variants 0, suggestions 34,901, albums 7,
  album_photos 258, places 3, place_aliases 0, photo_places 3, photo_groups
  143, audit_log 129,078, job_runs 55); head
  `1793369363869_phase-12-download-export`; 215 MB (no bloat after restore);
  system_identifier **7618623477443291636**. `migrate:up`: "No migrations to
  run!".
- **B3.** `photoorg_web`: GC.md procedure with `DATABASE_URL` and
  `PHOTOORG_DB_ROLE=web` inline — 35 migrations, second run a no-op, 2,827
  nicknames, `node tools/id-floor.js` OK on all seven sequences.
  `photoorg_test`: `npm run test:setup`. Local web `PHOTO_DIR` is
  `D:\Photos-web-local` (moved off C: long ago, per GC.md), so it travelled on
  D: with its subfolders; nothing to recreate on C:.
- **B4.** **Python was not installed** (only the Store stub) and **neither was
  exiftool**: `winget install Python.Python.3.12 --scope user` (3.12.10) and
  `winget install OliverBetz.ExifTool --version 13.59`, which lands exactly at
  `desktop/.env`'s `EXIFTOOL_PATH`. `desktop\.venv` + `pip install -e .[dev]`;
  `npm ci` in `shared/` and `web/`.
- **B5.** `check_working_files --dry-run` (78 s): 12,690 live photos and 850
  backs `already_ok`, every post-condition 0 (247 bare pointers on
  soft-deleted photos, unresolved as designed). **ACLs: every deny on D: named
  the laptop's SID (`S-1-5-21-2069390313-…-1001`), so on this machine nothing
  was denied — not just `D:\Contributed`, the masters too.** Re-applied the
  documented denies for `DESKTOP-2DP4KF5\George` (no elevation needed):
  `D:\Photos` and `D:\Scanned Photos` `(OI)(CI)(WD,AD,DC)`, `D:\Contributed`
  and `D:\Contributed\georgefclay_gmail.com` `(WD,DC)`,
  `…\georgefclay_gmail.com\1` `(OI)(CI)(WD,AD,DC)` — each now mirrors the
  laptop entry beside it (left in place; harmless). `run_masters_guard`: all
  three roots and their sampled subfolders read-only; a probe file in
  `_incoming/` writes and deletes.
- **B6.** First desktop run: 288 passed then
  `test_a_sixteen_bit_greyscale_tiff_stays_sixteen_bit` failed — the fresh
  venv had **Pillow 13.0.0**, which refuses `fromarray(mode="I;16")`
  (`cleanup/render.py` `_save`; a real 16-bit cleanup accept would fail too).
  Pinned the venv to 12.3.0 and `pyproject.toml` to `Pillow<13`
  (PROJECT-PLAN §5 item 27). Then **desktop 784 passed, 1 skipped**
  (8.5 min); **web 256/256** (2.6 min), run after, never together.
- **B7.** `INFERENCE_URL` is `http://192.168.50.219:8500` (an IP, so nothing
  to resolve); `/health` → `ok`, all five batch jobs completed, inboxes empty,
  blackout inactive.
- **B8.** Pull contributions: 0 / 0. `/sync/status` before: web DB
  `photoorg` / 7648012335739445998 ≠ ours, `id_floor.ok` true — the
  shared-DB guard passes. `tools/run_push.py` (pulls first): exit 0, **0
  failed stages**, 13,294 photos upserted, **0 files**, 222 s; per table
  album_photos 253, albums 6, faces 23,464, people 118, photo_backs 845,
  photo_groups 4, photo_masters 13,537, photo_places 3, suggestions 34,849,
  the rest 0 — identical to A2. `/sync/status` after: identical to A2's. The
  cursors in `D:\PhotoArchive\sync-state\sync_state.json` travelled: no
  re-delivery.
- **B9.** `run-desktop.bat` opens ("Photo Archive" window; log: masters guard
  read-only on all three roots, `crash.log` empty). Metadata `count_due`:
  **4 due, 4 in a group** — the "Testing 2" photos from A2's pull, as A2
  said; the write + push is still to do. **George to eyeball the Sync tab's
  group list** (left open).
- **B10.** **Not done by Claude:** the read-only `ssh … git log -1` to the VM
  was refused by Claude Code's permission classifier (as GC.md already
  records for remote writes). George: `! ssh -i ~/.ssh/Tronkits.pem
  ubuntu@44.255.253.62 'cd ~/photoorg && git log -1 --oneline'`.
- **B11.** PROJECT-PLAN Progress 2026-11-16 + §5 item 27; GC.md machine notes
  (desktop PC entry, laptop retired); committed `Ops: development moved to the
  desktop`.

This runbook is pre-approved — **GO** for each phase on its machine — but if
anything does not match reality, append questions under `### Questions` here
and stop instead.
