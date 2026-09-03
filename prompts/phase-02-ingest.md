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

