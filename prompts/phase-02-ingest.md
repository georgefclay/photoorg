# Phase 2 — Desktop shell and Ingest

Read `CLAUDE.md`, `PROJECT-PLAN.md` §1–§2 and Phase 2, `shared/SCHEMA.md`, and `photo-archive-build-prompts.md` §3 first. Work in `desktop/`. Phase 1 schema is applied to `photoorg`.

GOAL: a PySide6 app with a working Ingest mode that walks both master roots, derives working copies, records provenance (batch, sequence, folder hints), detects backs and rescans, and lets George review pairings before anything commits. Other modes are empty placeholders.

INVIOLABLE: `D:\Photos` and `D:\Scanned Photos` are never written. Before the first file is touched, ingest must prove it cannot write to either root (see "Masters guard"). If the guard cannot prove it, ingest refuses to run.

## Package layout
```
src/photoarchive/
  __main__.py          app entry; --smoke
  config.py            (exists)
  db.py                psycopg connection pool, helpers, audit()
  app/                 main window, sidebar, mode registry, settings dialog
  modes/ingest/        scanner, hasher, exif, thumbs, back_detect, pairing, ui
  modes/{triage,dedupe,cleanup,faces,sync}/  placeholder panels
  workers.py           QThread/QRunnable helpers with progress signals
tests/                 pytest, no GUI
```

## Shell
- Sidebar with six modes. Status bar showing DB connection, masters guard state, and counts (photos, untriaged, backs).
- Settings dialog editing the `.env` values with validation; changes reload config. Masters paths are shown read-only in the dialog once any photo exists.
- Long operations run in worker threads; the UI never blocks. Every long operation has a Cancel that finishes the current file and stops cleanly.
- A log pane (dock widget) mirroring Python `logging` at INFO; also writes `%LOCALAPPDATA%\PhotoArchive\logs\photoarchive.log` rotating at 5 MB.

## Masters guard
At ingest start, for each root: attempt to create a probe file `._photoarchive_write_probe` in the root and in one random subfolder. Success means the root is writable → refuse to run, show the exact command to fix it (`icacls` deny-write for the current user, or set the folder read-only), and log it. Also verify the roots do not overlap any writable dir (config already does this). Record the guard result and timestamp in `job_runs.params`.

Also produce a **manifest** before and after each run: `tools/manifest.py` walks a root and writes `path,size,mtime,sha256` to a CSV under `%LOCALAPPDATA%\PhotoArchive\manifests\`. The "no master changed" acceptance check diffs two manifests. sha256 of 17k files is slow; cache by `(path,size,mtime)` so repeat runs are quick.

## Master roots are a list, not two fields
Replace `MASTERS_PHOTOS` / `MASTERS_SCANS` in `config.py` and `.env.example` with one setting:
```
MASTER_ROOTS=photos=D:\Photos;scans=D:\Scanned Photos
```
`label=path` pairs separated by `;`. Labels are `[a-z0-9_]+`, unique, and become `photos.source_root`. A root's `kind` is inferred from its label for now (`scans*` → scan handling with batch/sequence/back/rescan detection; anything else → digital handling) and can be overridden with a third part: `navy=E:\Navy Scans:scan`. George will add roots over the years; adding one must never require a code change. The masters guard, manifest tool, and ingest all iterate the list. Settings dialog edits the list (add/remove/relabel; a label cannot be changed once photos reference it).

## Scanning and provenance
Walk every root recursively. Accept `.jpg .jpeg .tif .tiff .png .heic .mp4 .mov` (case-insensitive). Anything else is logged and skipped.

For every file record:
- `source_root`: `photos` or `scans`.
- `source_folder`: path relative to the root, forward slashes (`_2005-04`, `Batch 00012`, `Chuck and Lola Wedding/High Quality`).
- `source_filename`.
- `scan_batch`: for `scans` only, the **top-level** folder name (`Batch 00012`, `Chuck and Lola Wedding`). Subfolders like `High Quality` stay in `source_folder` only.
- `scan_sequence`: for `scans` only, 1-based position within `source_folder` using natural sort of filenames (so `IMG004` < `IMG026`, and `…-0001` < `…-0010`).
- `is_scan`: true for everything under `scans`; for `photos`, true when there is no EXIF `DateTimeOriginal` and no camera make/model.

Stable working name: `{photo_id:08d}_{sha256[:8]}.{ext}` in `WORKING_DIR` (flat, no subfolders). Working copy is a byte-for-byte copy of the master for now; Phase 7 derives cleaned versions.

Thumbnails: 320 px long edge JPEG in `THUMBS_DIR/{photo_id:08d}.jpg`; used by every grid in the app. TIFF and HEIC decode via Pillow (+ pillow-heif). Video: store the row with the right mime, extract no thumbnail (placeholder icon), skip hashes other than sha256.

## Hashes and EXIF
- sha256 of the master file (streamed).
- pHash and dHash (imagehash, 16×16 → hex strings) of the decoded image, orientation-corrected.
- EXIF via piexif/exifread: `DateTimeOriginal` → `exif_taken_at` (assume local time), camera make/model → `exif_camera`, GPS → lat/lon, orientation. When `DateTimeOriginal` exists: `capture_date`, precision `exact`, `capture_date_confirmed = true`. Otherwise leave `capture_date` null.

## Folder hints → suggestions (never facts)
- `photos` root, folder `_YYYY-MM`, no EXIF date: insert a `suggestions` row, kind `date`, source `import`, confidence 0.3, payload `{"date":"YYYY-MM-01","precision":"month","evidence":"export folder _YYYY-MM"}`.
- `scans` root, top-level folder that is **not** `Batch NNNNN`: create (once) an album with that name, `source='import'`, add the photo; and insert a `suggestions` row kind `description`, source `import`, confidence 0.5, payload `{"text":"<folder name>","evidence":"scan folder name"}`.
- Batch folders create no album and no suggestion.

## Resumability and idempotence
- Unit of work is one file. Before processing, look up sha256 in `photo_masters` and `photo_backs`; if present, skip.
- A file that fails (corrupt, undecodable) gets a row in `job_items` with the error and is skipped; the run continues.
- Each run is a `job_runs` row (`job_name='ingest'`) with per-file `job_items`. Ctrl-C / Cancel leaves the DB consistent (one transaction per file).
- Re-running after new scans are added ingests only the new files.

## Back detection
For `scans` only, after hashing, score each image as a probable back: mostly light background, low colour saturation, ink-like dark strokes covering a small fraction, no face-like regions (cheap OpenCV Haar cascade is enough here), aspect ratio close to the previous file's. Output `back_score` 0–1. Any file with score ≥ 0.6 **and** a previous file in the same folder that is not itself a probable back becomes a **proposed pair** (front = previous file). Proposed pairs are held in a staging table `ingest_pairings` (create migration 12 in `shared/migrations/`: id, front_master_path, back_master_path, back_score, status pending/accepted/rejected, decided_at) — nothing is written to `photo_backs` until accepted.

## Rescan detection
For `scans` only: after pHash, look for an existing `photos` row (any root) whose pHash Hamming distance ≤ 6 and whose `is_scan` is true. If found, stage it in `ingest_rescans` (same migration: id, existing_photo_id, new_master_path, distance, status). Nothing commits until accepted. Accepted → new `photo_masters` row for the existing photo; it becomes preferred if its pixel count is larger; `photos.sha256`, `working_path`, `file_version` update accordingly. Rejected → normal new photo.

## Two-stage commit
Stage 1 (scan): everything above runs; ordinary files commit immediately as photos; proposed backs and rescans are held.
Stage 2 (review): a grid showing each proposal — thumbnails side by side, `Batch 00012 #017 → #018`, score/distance — with keyboard A accept, R reject, arrows, undo, "accept all above 0.9". Accepting a back creates the `photo_backs` row and **removes** the back's provisional `photos` row if one was created (so: don't create one — hold the file until decided). Rejected backs become ordinary photos.

The status bar shows how many proposals are pending; the app nags on exit if any remain but never auto-decides.

## Ingest UI
Root selector (both / one), Start, Cancel, progress bar with files/sec and ETA, running counts (new, skipped, failed, backs proposed, rescans proposed), log pane. After completion: summary dialog and a button to open the review grid.

## Tests (pytest, no GUI)
- Natural sort and `scan_sequence` assignment on a synthetic folder listing.
- `scan_batch` derivation for nested folders.
- Folder-hint parsing (`_2005-04` → suggestion; `Batch 00012` → none; `Chuck and Lola Wedding` → album).
- Masters guard: a temp dir that is writable is refused; a read-only one passes (use `os.chmod`/`icacls` in a temp dir).
- Idempotence: ingesting the same temp tree twice yields the same row counts.
- Back-detect scorer on 4 fixture images you generate synthetically (blank with scribbles vs. a colour photo).
Use `TEST_DATABASE_URL` for DB tests; refuse if it equals `DATABASE_URL`.

## Verification, then stop
1. `pytest` green.
2. Manifest of both roots taken.
3. Full ingest of both roots. Report: elapsed, files/sec, counts by root and by outcome, number of proposals of each kind, failures with reasons.
4. Second run: 0 new.
5. Manifest again; diff is empty.
6. Spot check: pick 5 random scans; their `scan_batch`/`scan_sequence` match the folder and filename order.
7. Update `CLAUDE.md` with anything a future session must know (working-name scheme, staging tables, guard). Update `shared/SCHEMA.md` for migration 12.
8. Commit: `Phase 2: desktop shell and ingest`.

Do not review the proposals yourself — George does that in the grid. Report back with the numbers from steps 3–6.

---

## Answers to Claude Code's questions

1. **MASTER_ROOTS delimiter.** (b): `label=path|kind`, kind optional. Example: `photos=D:\Photos;scans=D:\Scanned Photos|scan;navy=E:\Navy Scans|scan`. Drop the "infer kind from label" rule; default kind is `digital`, and `scans` in the example gets an explicit `|scan`. Update `.env.example` accordingly.
2. **Staging shape.** Yes, exactly as proposed: `WORKING_DIR/_staging/{sha256}.{ext}`, `THUMBS_DIR/_staging/{sha256}.jpg`, all computed values on the staging row, accept = insert rows → rename → move thumb, in one transaction with the file ops last. Reject on a back proposal = same path as a normal new photo.
3. **front_photo_id.** Switch to `front_photo_id bigint references photos on delete restrict`. Same for `ingest_rescans.existing_photo_id` (already an id).
4. **Videos.** Whitelist + log-and-skip with a count in the summary. Build the real path when the first one appears.
5. **Guard remediation.** Print the `icacls` deny form: `icacls "D:\Photos" /deny "%USERNAME%:(OI)(CI)(WD,AD,DC)"` — include `(OI)(CI)` so it inherits to files and subfolders, and `DC` so files can't be deleted. Mention `attrib +R` is not sufficient. Note the exact command in `GC.md` under "Other things to remember" with the matching `/remove:d` to undo when George needs to add a new batch.
6. **TEST_DATABASE_URL.** (a): load `shared/.env` via a tiny helper in `tests/conftest.py`. Single source of truth. Skip DB tests with a clear message if it is missing; refuse if it equals `DATABASE_URL`.
7. **Working-name order.** (c). One transaction: insert `photos` (placeholder `working_path`), insert `photo_masters`, take the id, copy master → final name, update `working_path`, commit. Copy failure rolls back and deletes any partial file.
8. **is_scan under a digital root.** Correct. `is_scan=true`, nothing else.
9. **Audit.** Record: `job_runs` start/finish (with counts), every proposal accept/reject (previous = proposal row, new = resulting ids), and rescan acceptance's change of preferred master. Do **not** audit each photo insert — `job_items` already records that and 17k audit rows say nothing.
10. **Migration filename.** `npm run migrate:create -- ingest-staging-tables`; timestamps are the convention.

Note on the guard vs. adding new batches: George will periodically write new scans into a master root. The workflow is: `icacls … /remove:d` → add files → `icacls … /deny` → run ingest. Put a "Masters are currently writable" warning (not a refusal) in the status bar at app start so it's obvious when the deny is off; ingest itself still refuses.


---

## Phase 2 fix-up 1 — back detector is wrong

Observed in the review grid on `Batch 00001`:
- Proposal "#19 → #20": left is a handwritten back (#19), right is a studio portrait (#20). The portrait was scored as a back (0.61) and paired with the handwriting as its "front".
- Proposal "#21 → #22": two portraits, right one scored 0.61 as a back.
- Nearly every proposal seen scores 0.61. A constant score means at least one component returns a fixed value and dominates.
- 914 proposals out of 5,279 scans is far too many; George scanned backs only when there was writing.

Do this, in order:

1. **Diagnose before changing anything.** Add `tools/backscore.py <root-label> <batch>` that prints, per file in the batch: sequence, filename, every component of the score (light-pixel fraction, mean saturation, ink-stroke fraction, face count, aspect-ratio match) and the final score. Run it on `Batch 00001` and paste the table for #17–#23 in your report. Identify which component is broken (likely: saturation computed on a greyscale thumbnail, lightness threshold inverted, face cascade never loading so "no faces" is always true, aspect-ratio term always 1).

2. **Fix the scorer.** Requirements on Batch 00001: #19 (handwriting on white) scores >= 0.8; the studio portraits (#20, #21, #22) score <= 0.2. A dark-background photo must never look like a back. Components: fraction of pixels with L > 0.85 (expect > 0.6 for a back), mean saturation (expect < 0.08), ink fraction (dark strokes 0.5-15 % of pixels), face count = 0. Combine multiplicatively or with a hard veto when any component fails - not by averaging; averaging is how a portrait gets 0.61.

3. **Fix the pairing direction.** A back's front is the **preceding** file in scan sequence. The grid shows the front on the left and the back on the right, labelled `Batch 00001 #18 (front) <- #19 (back)`. If the preceding file is itself a probable back or does not exist, propose nothing for that file.

4. **Rebuild proposals.** Add an Ingest action "Rebuild back proposals" that re-scores **every** file under a scan-kind root, not just the pending ones — real backs like #19 were never proposed and are already committed as photos.
   - Pending `ingest_pairings` rows: re-score; drop those that no longer qualify (held file becomes an ordinary photo through the normal path); keep those that still qualify with the new score and corrected front.
   - Committed `photos` rows (scan roots, not already a front of an accepted pairing, not deleted): if the new score qualifies, create a pending `ingest_pairings` row whose back is the existing photo (add nullable `back_photo_id` to the table; `back_master_path` stays for held files). Accepting such a proposal creates the `photo_backs` row, moves the working file and thumb to the back's location, and marks the old `photos` row `is_deleted=true` with `physical_ref_note='converted to back of photo <id>'` — no real delete. Rejecting leaves the photo as it is and records the rejection so it is never proposed again.
   - Never touch accepted/rejected rows. Rescan proposals are untouched.
   Run it. Report the new count and a score histogram (0.1 buckets).

5. **Review grid usability.** Add a Swap key (S) that re-pairs the back with the *following* file instead, for the rare reversed scan; a score filter so George can view only >= 0.9 first; show both thumbnails at full height, uncropped.

6. **Tests.** Regression tests with synthetic fixtures: white image with dark scribbles -> >= 0.8; dark image with a face-sized light oval -> <= 0.2; grey mid-tone image -> <= 0.3.

7. Add `run-desktop.bat` at the repo root that launches `desktop\.venv\Scripts\python -m photoarchive`.

Commit: `Phase 2 fix-up 1: back detector, pairing direction, rebuild`.
Report: the diagnostic table, root cause, new proposal count and histogram.

---

## Phase 2 fix-up 2 — don't skip on aspect mismatch

The rebuild skipped 129 candidates for aspect mismatch. A back is frequently cropped differently from its front, so aspect is evidence, not a veto. Change: when the back score (without the aspect term) is >= 0.8, propose the pair regardless of aspect and show an "aspect differs" tag in the grid. Keep aspect as a veto only for scores below 0.8. Re-run the rebuild and report the new pending count and how many carry the tag. Commit: `Phase 2 fix-up 2: aspect is evidence, not veto`.

---

## Phase 2 fix-up 3 — scan order is wrong in some batches

George reviewed ~100 proposals in order; they were right. After that: backs paired with the wrong front in both directions, runs of 5 backs in a row with no front. The detector is fine now; the **sequence** is wrong for some folders. Do not change the scorer.

1. **Diagnose first.** `tools/scanorder.py` prints, per scan folder: file count, distinct filename styles (timestamp / IMG / other), whether natural-sort order == mtime order, the number of positions that differ, and any runs of >= 2 consecutive proposed backs. Run it over both scan roots and paste the folders that disagree. Also print the first folder after which the review order went bad (proposals are reviewed in batch/sequence order; find the batch of proposal ~#100).

2. **Sequence by scan time.** `scan_sequence` = order by file mtime, then natural filename as tie-break. Rationale: the scanner writes files in the order prints went through it; that is the envelope order. Recompute `scan_sequence` for every scan photo and back (new tool action "Recompute scan order"), rebuild back proposals (pending only; accepted/rejected untouched), and re-run the diagnostic to show the disagreements are gone. If mtime order is *also* nonsense for some folder (all identical mtimes from a copy), fall back to filename order for that folder and flag it in the report.

3. **Manual pairing in the grid.** Under the two images, a filmstrip of the batch from #N-5 to #N+5 with sequence numbers and a back-score badge. Clicking a thumbnail makes it the front for this proposal. Keys: A accept, R reject, S swap to following, F pick front from filmstrip (then arrows + Enter), N = orphan back.

4. **Orphan backs.** Migration 13: make `photo_backs.photo_id` nullable. "N" accepts the file as a back with no front: `photo_backs` row with `photo_id = null`, working file/thumb moved as for any back, the old photo row soft-deleted if it was committed. It will still be transcribed in Phase 6; the writing often identifies the front later. Add "Attach to front" later; not now.

5. Grid order is strictly `scan_batch`, then `scan_sequence` of the back. Show "batch N of M" in the header.

Commit: `Phase 2 fix-up 3: scan order by mtime, manual pairing, orphan backs`.
Report: the diagnostic before and after, and how many pending proposals changed front.

---

## Phase 2 fix-up 4 — B&W photos on white backgrounds are scored as backs

Observed: `Batch 00005 #235 (front) <- #240 (back)`, score 1.00. #240 is a black-and-white studio photo of a baby on a white background. Every current feature passes for it: saturation 0 (it is B&W), light fraction high, "ink" fraction in range (hair, shadows), no face found (Haar misses a laughing baby). Runs of such photos produce runs of "backs", and the front search then walks back 5 positions.

The features in use cannot separate "white-background B&W photo" from "handwriting on paper". Add the two that can, both as hard vetoes:

1. **Mid-tone fraction.** Convert to L. Fraction of pixels with 0.15 < L < 0.85. A back is bimodal (paper + ink): expect < 0.06. A photo, even a high-key B&W one, has continuous tone: expect > 0.15. Veto if > 0.10.
2. **Largest dark connected component.** Threshold L < 0.4, connected components. On a back the largest component is a stroke: area < 0.5 % of the image, and its bounding box is thin (min(w,h)/max(w,h) < 0.3 or area/bbox < 0.35). On a photo the largest dark component is hair/clothing/shadow: area > 1 %. Veto if largest component area > 0.8 % of image.

Keep the existing vetoes. Verify on: Batch 00001 #19 (must stay >= 0.8), Batch 00001 #20-#22 (0), Batch 00005 #236-#240 (all 0), and print the component table for each of those in the report.

3. **Front is the immediate predecessor only.** Never walk back past a probable back. If the preceding file is itself a probable back, propose the file as an orphan-back candidate (front = none, George decides with N or F). Remove the walk-back logic.

4. **Contact sheet before pairs.** Add "Proposed backs contact sheet": an HTML page (written to `%LOCALAPPDATA%\PhotoArchive\reports\backs-<timestamp>.html`) with every pending proposed back as a 200 px thumbnail, batch and sequence under each, sorted by batch/sequence, clicking a thumbnail toggles a "not a back" mark, and a button that writes the marked ids to a JSON file the app can import ("Reject marked from contact sheet"). George can clear false positives in minutes this way instead of one pair at a time.

5. Rebuild pending proposals with the new scorer, produce the contact sheet, and report: new pending count, histogram, and the component tables from step 2.

Commit: `Phase 2 fix-up 4: mid-tone and component vetoes, no walk-back, contact sheet`.

---

## Phase 2 fix-up 5 — review grid display bugs (photo-as-back proposals)

Screenshot from George, `Batch 00006 #2 (front) <- #3 (back)`, a correct proposal:

1. Right pane shows "(no thumb)" for the back, yet the same photo's thumbnail renders in the filmstrip. For `photo-as-back` proposals (`back_photo_id` set, `back_master_path` null) the main pane is still looking in `THUMBS_DIR/_staging/{sha}.jpg`. Resolve the thumb the same way the filmstrip does: `back_photo_id` -> `THUMBS_DIR/{photo_id:08d}.jpg`; fall back to staging only for held files. Same for the front. Add an offscreen UI test that loads a photo-as-back proposal and asserts both panes have a non-placeholder pixmap.
2. Filmstrip labels: most tiles show only `#` — the sequence number is clipped. Give the label its own line under the thumbnail (not overlaid on the right edge), full width, `#N` plus `FRONT` / `BACK` / `PENDING` tags where relevant.
3. The red focus border sits on tile #1 while the proposal is #2/#3. On load, the filmstrip's current item must be the proposed front; the back tile gets a distinct outline.
4. The back in this example is scanned mirrored (text reads reversed). Record `details.mirrored = true` when the filmstrip/back detector sees mostly reversed strokes is out of scope; instead add a note to `PROJECT-PLAN.md` Phase 6: transcribe-back should try the horizontally flipped and 180-degree rotated variants when the first pass returns low confidence, and record which orientation was used.

Commit: `Phase 2 fix-up 5: review grid thumbs and filmstrip labels`.

---

## Phase 2 fix-up 6 — Accept on a photo-as-back proposal fails with WinError 2

George pressed Accept on a photo-as-back proposal and got "Decision failed: [WinError 2] The system cannot find the file specified". Do this with fix-up 5.

1. **Integrity check first.** Find every `ingest_pairings` row touched in the last 24 h (any status change or `decided_at`), and for each: does the `photos` row / `photo_backs` row / working file / thumb agree with the status? Report any half-applied state and repair it (restore the photo row and file to their pre-accept state if the DB moved but the file did not, or vice versa). Also list any `photo_backs` rows whose `working_path` does not exist on disk.
2. **Root cause.** For `back_photo_id` proposals the source file is `photos.working_path` (or `quarantine_path` if the photo was junked meanwhile) and the thumb is `THUMBS_DIR/{id:08d}.jpg` — not `_staging/{sha}`. Use one resolver function for "where is this proposal's back file / thumb right now" shared by the grid display, accept, and reject.
3. **Order of operations on accept** (all inside one try): verify source files exist -> DB transaction (insert `photo_backs`, soft-delete the photo row with `physical_ref_note`, mark proposal accepted, audit) -> commit -> move file and thumb -> if a move fails, log it, keep the DB as decided, and put the proposal id in a "needs file repair" list shown in the status bar (DB is the source of truth; files catch up). Never show a raw WinError to George: the banner says what file was expected and where.
4. Tests: accept a photo-as-back proposal end to end on the test DB with real temp files; accept when the photo is in quarantine; accept when the file is missing (DB decided, repair list populated).

Commit: `Phase 2 fix-up 6: accept photo-as-back proposals`.
