# Phase 3 — Triage

Read `CLAUDE.md`, `PROJECT-PLAN.md` (§1–§2, Phase 3, Risks), `shared/SCHEMA.md`, then this file. Work in `desktop/`. Phase 2 is done: ~13,700 photos ingested, ~9,600 of them digital from `D:\Photos`.

GOAL: a fast, keyboard-only cull pass so George can decide keep / junk / private for every photo, plus heuristic pre-sorting so the obvious junk is a single keystroke. Triage is the step that decides how much work every later phase (AI jobs, dedupe, cleanup, faces, sync) has to do, so speed matters more than anything else in this phase.

RULES:
- Junk = quarantine the working copy + `is_deleted = true`, `triage_status = 'junk'`. Never a real delete; masters untouched; restorable.
- Private = `triage_status = 'private'`, `is_private = true`. Stays in the working set; excluded from sync forever.
- Keep = `triage_status = 'keep'`.
- Every decision writes an audit row (previous status → new status, actor `desktop`).
- Undo is unlimited within the session and also possible later from the quarantine browser.

## Pre-sort (no AI yet)
Run once as a job (`job_name='triage_presort'`, re-runnable, incremental) over every untriaged photo. Compute and store in a new table `triage_hints` (migration: photo_id PK, `hint text`, `confidence real`, `details jsonb`, `computed_at`):
- `screenshot`: dimensions match a known phone/desktop screen size, or EXIF software names an OS/app and there is no camera model, or PNG with no EXIF.
- `document`: high fraction of near-white pixels, high edge density in text-line structure, low saturation, no faces (reuse the Phase 2 face cascade and mid-tone features). Photos of receipts, labels, product keys, whiteboards.
- `blank_or_dark`: mean L < 0.08 or > 0.95 with very low variance.
- `burst`: 3+ photos within 2 s by EXIF time with pHash distance ≤ 4 (mark all but the sharpest, by Laplacian variance, as burst extras).
- `tiny`: long edge < 600 px.
- `exact_dup_of`: sha256 already present on another photo (should be none after ingest; safety net).
No hint → `photo`. Hints are hints: they set the *default* key in the UI, never a decision.

Report the hint distribution after the run.

## UI — Triage mode
Two views, same keys.

**Grid view** (default): thumbnails, 6–8 across, virtualised so 13k rows scroll smoothly. Filters: status (untriaged/keep/junk/private), hint, source root, source folder / batch, year (from `capture_date` or the import folder). Sort: sequence, capture date, hint. Multi-select with Shift/Ctrl-click and Ctrl-A within the current filter; a decision key applies to the selection.

**Single view** (Enter on a thumbnail): image fitted to the pane, EXIF strip (date, camera, dimensions, folder, batch/sequence), hint badge, position "n / N in filter".

Keys in both views: `K` keep, `J` junk, `P` private, `U` undo last, `Space` toggle selection, arrows/PageUp/PageDown move, `Enter` open/close single view, `Esc` back to grid, `1–5` jump to hint filters, `/` focus the filter box. After a decision the cursor advances automatically. Show the mapping in a strip at the bottom.

**"Apply hints" button**: for the current filter, pre-selects every photo whose hint is `screenshot`, `document`, `blank_or_dark`, `tiny`, or `burst` extra, so George reviews the selection in the grid and presses J once. Nothing applies without the keypress.

Status bar: counts for untriaged / keep / junk / private, decisions this session, decisions per minute.

## Quarantine browser
A dock or dialog listing junk photos (thumbnail, folder, when, why = hint), filter and search, `Restore` moves the file back to `working/` and sets status `untriaged`. Same for private → keep (flag off).

## Performance targets
- Grid scroll and filter changes: < 100 ms perceived on 13k rows (query with LIMIT/OFFSET or keyset; thumbnails via a cache with lazy loading).
- A decision keypress commits and advances in < 50 ms perceived (commit in a worker; UI advances optimistically; failures surface in the log and revert).

## Tests
- Hint classifiers on synthetic images (screenshot-size PNG, white page with text lines, black frame, 400 px image).
- Burst grouping on synthetic EXIF timestamps + hashes.
- Decision state machine: keep→junk→undo→keep leaves the file in `working/`; junk moves it to `quarantine/` and restore moves it back; private never touches files.
- Audit row written for every transition.

## Verification, then stop
1. `pytest` green.
2. Pre-sort job run over all untriaged photos; paste the hint distribution.
3. George triages for 10 minutes; report decisions per minute from the status bar and anything that felt slow.
4. Quarantine one photo, restore it, confirm file location and status each step.
5. Update `CLAUDE.md` (triage states, quarantine rules) and `shared/SCHEMA.md` (triage_hints).
6. Commit: `Phase 3: triage`.

---

## Answers to Claude Code's questions

1. **Quarantine.** `QUARANTINE_DIR` already exists in `desktop/.env` from Phase 0 (`D:\PhotoArchive\quarantine` on George's machine) — use it. Filename: same as working, flat. On junk: `quarantine_path` = new path, `working_path` = NULL (a non-null `working_path` must always mean "file is in working/"), `is_deleted`, `deleted_at`, `triage_status='junk'`. Restore reverses all of it. Keep the thumbnail in `THUMBS_DIR`.
2. **Hint precedence.** Yes: exact_dup_of → screenshot → blank_or_dark → tiny → document → burst. Store the losing hints in `details.also` so nothing is lost.
3. **Screenshots.** Soft rule carries the weight: no camera make/model AND (PNG, or EXIF software names an OS/app, or dimensions in a small curated list of exact phone/desktop sizes — iPhone family, common Android, 1920×1080, 2560×1440, 3840×2160, 1366×768, 1440×900, 2560×1600). Keep the list in one constant. A JPG of a real photo with no EXIF is *not* a screenshot unless its dimensions match the list exactly.
4. **Year.** Yes, union of `capture_date` year and `_YYYY-MM` from `source_folder`. Show which one it came from in the single view.
5. **Sequence sort.** `photos.id`. Scans already sort by batch/sequence because they were ingested in that order.
6. **Undo.** One step per keypress.
7. **Hint keys.** 1 untriaged (all), 2 screenshot, 3 document, 4 blank_or_dark, 5 tiny + burst. Picklist for the rest. Fine.
8. **Pre-sort trigger.** Button only, with the count of photos lacking a hint shown on it.

Minor items: all agreed. One addition to the audit payload: include `hint` at decision time so we can later measure how often each hint was right.

---

## Phase 3 fix-up 1 — grid goes black after first decision

George opened Triage, pressed K on the first photo, and the whole grid went black. Nothing else was touched.

1. Read `%LOCALAPPDATA%\PhotoArchive\logs\photoarchive.log` and any Qt/Python traceback from that moment; paste it in the report.
2. Reproduce with a scripted QTest (offscreen platform) against `TEST_DATABASE_URL`: load the grid with the untriaged filter, send K on the first item, assert the model still has N-1 rows, the view paints, and the current index is valid.
3. Likely suspects, check each: the model resets while the decision worker is still running and the view paints with an invalid index; the worker touches a QWidget/QPixmap from the non-GUI thread (must signal back to the GUI thread); an unhandled exception in the paint delegate (thumbnail missing for the next item) that leaves the viewport unpainted; removing the row from the model before the optimistic commit returns.
4. Required behaviour after a decision under the untriaged filter: the decided item leaves the list, the cursor lands on the item that took its place (or the previous one at the end), and the view repaints immediately. Under any other filter, the item stays and shows its new status badge.
5. Wrap the delegate's paint in a try/except that draws a grey placeholder and logs once per photo, so a bad thumbnail can never blank the grid again.

Commit: `Phase 3 fix-up 1: grid blank after decision`. Report the root cause.

---

## Phase 3 fix-up 2 — "this is a back" key in Triage

During triage George found a `blank_or_dark` photo that is really the back of a print with only a date written on it. The back detector missed it (too little ink). Triage needs a way to say so.

1. Key **B** = "this is a back". For a scan-root photo: create a pending `ingest_pairings` row with `back_photo_id` = this photo, `front_photo_id` = the immediately preceding photo in the same folder by `scan_sequence` (null if none or if that one is itself a back/pending back), `back_score` = 1.0, `details.source = 'triage'`. Set `triage_status='keep'` so it does not get junked meanwhile. Cursor advances. The pair is then decided in the Phase 2 review grid like any other (A/R/S/F/N). For a digital-root photo, B does nothing but show "not a scan" in the status bar.
2. Add `blank_or_dark` handling: when the hint fires on a **scan-root** photo, run the back scorer's ink/stroke measure; if there is *any* ink (fraction > 0.001), change the hint to `possible_back` instead. Add `possible_back` to the hint CHECK and to the picklist; key 4 now shows blank_or_dark + possible_back together.
3. Re-run the presort for scan-root photos currently hinted `blank_or_dark` and report how many moved to `possible_back`.
4. Show the key in the bottom strip and the status bar count of pending pairings.

Commit: `Phase 3 fix-up 2: B key and possible_back hint`.

---

## Phase 3 fix-up 3 — B on a photo that already has a pairing row

George pressed B on a back and got "Photo already has ingest pairings ...". Many scans have old rows from the Phase 2 rounds (rejected while fronts were wrong, or still pending). B must resolve, not refuse:

- Existing row **pending** for this photo as back: leave it, status bar "already queued for review (front: Batch X #N)", advance.
- Existing row **rejected**: reopen it — status back to pending, `front_photo_id` recomputed as the immediate predecessor (null if none/back), `back_score` 1.0, `details.source='triage'`, `details.reopened_from=<old status>`; audit row. Advance.
- Existing row **accepted**: this photo should already be a back and not in triage; log a warning with the ids and show "already a back", no change.
- Multiple rows: apply the rule to the most recent; if any is pending, treat as pending.

Add a test for each branch. Commit: `Phase 3 fix-up 3: B reopens rejected pairings`.

Also in fix-up 3: status-bar messages triggered by a keypress must persist until the next keypress (no timeout). Any message that means "nothing was done" (refusal, error, "not a scan") shows as a coloured banner above the grid that stays until dismissed with Esc or the next decision key.
