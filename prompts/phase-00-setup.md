# Phase 0 — Project setup

Run this in Claude Code with `C:\Programming\Photos` as the working directory.

---

PROJECT: A private family photo archive. ~16,900 source images: 11,589 JPGs exported from a photo app (`D:\Photos`, folders `_YYYY-MM`) and 5,279 scanned prints (`D:\Scanned Photos`, `Batch NNNNN` and named event folders, JPG + TIFF). Full plan is in `PROJECT-PLAN.md`; original spec is in `photo-archive-build-prompts.md`. Read both before doing anything.

THREE TIERS, ONE MONOREPO:
- `desktop/` — Windows app, Python 3.12 + PySide6. Ingest, triage, dedupe, cleanup, faces, sync.
- `inference/` — FastAPI service, runs on a Mac mini over the LAN. Not built in this phase.
- `web/` — Node 24 + Express + EJS + Postgres. Deploys to an AWS VM the same way as George's other sites (git pull, systemd, Caddy).
- `shared/` — Postgres migrations (node-pg-migrate), nickname seed, API contract notes.

INVIOLABLE RULE: `D:\Photos` and `D:\Scanned Photos` are masters. No code in this repo ever writes to, moves, renames, or deletes anything under them. This phase creates no code that touches them at all.

TASKS:

1. Git. If `.git` does not exist, `git init` on branch `main`. Create `.gitignore` covering: `.env`, `.env.*` (but keep `.env.example`), `GC.md`, `node_modules/`, `__pycache__/`, `.venv/`, `*.pyc`, `working/`, `quarantine/`, `manual-fix/`, `thumbs/`, `*.log`, `.claude/settings.local.json`. Do NOT add a remote or push — George does that.

2. Directory skeleton with a `README.md` in each explaining its purpose in three lines or fewer:
   ```
   desktop/          (pyproject.toml, src/photoarchive/, tests/)
   inference/        (README only for now)
   web/              (package.json, server.js stub, routes/, views/, services/, public/)
   shared/migrations/
   shared/seed/
   prompts/          (already exists — leave it)
   ```

3. Desktop scaffold. `desktop/pyproject.toml` with dependencies: PySide6, Pillow, pillow-heif, imagehash, psycopg[binary], pydantic, pydantic-settings, exifread, piexif, numpy, opencv-python-headless, requests. Dev: pytest. A `src/photoarchive/__main__.py` that opens an empty PySide6 window titled "Photo Archive" and exits cleanly. A `desktop/.env.example` with these keys and a comment per line:
   ```
   MASTERS_PHOTOS=D:\Photos
   MASTERS_SCANS=D:\Scanned Photos
   WORKING_DIR=C:\PhotoArchive\working
   QUARANTINE_DIR=C:\PhotoArchive\quarantine
   MANUAL_FIX_DIR=C:\PhotoArchive\manual-fix
   THUMBS_DIR=C:\PhotoArchive\thumbs
   DATABASE_URL=postgresql://photos:CHANGEME@localhost:5432/photos
   INFERENCE_URL=http://mac-mini.local:8500
   INFERENCE_TOKEN=CHANGEME
   WEB_API_URL=http://localhost:8090
   WEB_API_TOKEN=CHANGEME
   ```
   Create the four `C:\PhotoArchive\*` directories. A `config.py` that loads these with pydantic-settings and refuses to start if `MASTERS_PHOTOS` or `MASTERS_SCANS` equals any of the writable directories.

4. Web scaffold. `web/package.json` (express, ejs, pg, connect-pg-simple, express-session, express-rate-limit, postmark, dotenv, archiver; dev: nodemon; engines node >= 24). `server.js` that boots, sets `trust proxy`, serves a `views/home.ejs` saying "Photo Archive — coming soon", listens on `PORT` (default 8090). `web/.env.example`:
   ```
   PORT=8090
   NODE_ENV=development
   BASE_URL=http://localhost:8090
   DATABASE_URL=postgresql://photos:CHANGEME@localhost:5432/photos
   SESSION_SECRET=CHANGEME
   POSTMARK_API_KEY=
   POSTMARK_FROM_EMAIL=
   ADMIN_EMAIL=georgefclay@gmail.com
   SERVICE_TOKEN=CHANGEME
   PHOTO_DIR=C:\PhotoArchive\working
   ```

5. Shared. `shared/package.json` with node-pg-migrate and pg; scripts `migrate:up`, `migrate:down`, `migrate:create`. No migrations yet — Phase 1 writes them. Empty `shared/seed/nicknames.csv` with header `canonical,variant,kind`.

6. Local database. Print the exact `psql` commands for George to run as the postgres superuser (do not run them yourself):
   ```
   CREATE ROLE photos LOGIN PASSWORD '<generate a 32-char random password and show it>';
   CREATE DATABASE photos OWNER photos;
   ```
   Then verify with `psql postgresql://photos:<pw>@localhost:5432/photos -c 'select 1'` once George confirms.

7. `GC.md` at the repo root from this template, filled in with what is known and `TODO` elsewhere. It is gitignored.
   ```
   # Photo Archive — Project Notes (George Clay)
   ## Brief description
   ## Status
   ## Change log
   ## Outstanding / TODO
   ## Passwords & keys        (table: item | location / value)
   ## System architecture     (laptop, Mac mini, AWS VM; ports; paths)
   ## How the pieces work together
   ## Maintenance
   ## Other things to remember
   ```
   Put the DB password from step 6 in it. Note the masters rule at the top of "Other things to remember".

8. Verification, then stop:
   - `cd desktop && python -m photoarchive` opens and closes a window.
   - `cd web && npm install && npm run dev` serves the home page on 8090.
   - `cd shared && npm install && npm run migrate:up` runs against the empty DB without error.
   - `git status` shows no `.env` or `GC.md`.
   - Commit: `Phase 0: monorepo skeleton`.

Report back with: the commands you ran, anything that failed, and the tree (`git ls-files`).

---

## Answers to Claude Code's questions

1. **Python toolchain.** Create `desktop/.venv` with `py -3.12 -m venv .venv`, then `pip install -e .[dev]` (plain pip; no uv/poetry/hatch). Add `.venv/` to `.gitignore` (already listed). Put the activate + install commands in `desktop/README.md`.

2. **Window behaviour.** Normal app: open and wait for the user to close it. Add a `--smoke` flag that self-closes after 1 second and exits 0; use `--smoke` for the step 8 check.

3. **Postgres ordering.** Yes. Do steps 1–7, then stop and print the two SQL commands. Wait for George to confirm before running `migrate:up` and the final commit.

4. **config.py guard.** Both. Normalize all six paths (resolve, case-fold on Windows) and refuse if either masters path equals, is a parent of, or lives underneath any of the four writable directories. Error message names the offending pair.

5. **GC.md content.** Fill in everything known now: brief, status ("Phase 0 complete"), change-log entry, DB role + password, all paths from `.env`, ports (web 8090, inference 8500), the three-machine architecture summarized from `PROJECT-PLAN.md`, and the masters rule at the top of "Other things to remember". Leave `TODO` only where nothing is known yet (VM details, domain, Postmark, service tokens).

6. **Backup check.** Add it to `GC.md` under "Outstanding / TODO" as item 1: "Confirm D:\Photos and D:\Scanned Photos are copied to a second drive before Phase 2 ingest runs. Copy in progress 2026-09-01." Not a code task.

7. **Lockfiles.** Yes, commit `package-lock.json` for `web/` and `shared/`.
