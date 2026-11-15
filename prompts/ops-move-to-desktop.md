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
(added as they come)

This runbook is pre-approved — **GO** for each phase on its machine — but if
anything does not match reality, append questions under `### Questions` here
and stop instead.
