# Phase 13 — Metadata writer (desktop)

Read `CLAUDE.md` (Inviolable rules; Cleanup invariants 1 and 4 — the version-and-swap pattern and the masters guard are reused here; the one working-file resolver; the sync `file_version` rule), `PROJECT-PLAN.md` (§2 decisions; Phase 13), `shared/SCHEMA.md` (photos, people, person_name_variants, places, photo_places, photo_backs, faces, audit_log), and `photo-archive-build-prompts.md` §12. Work in `desktop/`. Start with `git pull`.

GOAL: every working copy carries the **settled truth** about its photo inside the file — confirmed date, who is in it, where, what the back says — so the archive survives this software. Incremental, idempotent, resumable; runs on demand and after every Pull.

## Non-negotiables
1. **Writes only to working copies.** Assert in code: the target path must resolve through `resolve_working_path` to a file under `WORKING_DIR`; any path under a master root raises before a byte is written. The masters-guard probe runs at the start of every batch, as in ingest and cleanup.
2. **Facts only.** `capture_date` only when `capture_date_confirmed`; `faces.person_id` assignments (facts); `photo_places` facts; `photo_backs.transcribed_text` (confirmed or not — it is observational, but mark unconfirmed as "unverified transcription"); `photos.description_ai` only when it is the accepted fact, and always prefixed "AI-generated description: ". Pending suggestions are never written. Audit history, contributor identities, comments, likes, suggestion status: never.
3. **Never write a guess into a date field.** Decade/year precision: write `DateTimeOriginal` only for `precision in ('day')`; for `month`/`year` write XMP `dc:date`-style partial dates (`YYYY-MM`, `YYYY`) in XMP only, and a human-readable note in the description ("Date: about 1962"). Never fabricate a January 1st.
4. **Atomic**: write to `WORKING_DIR/_tmp/{id}.{ext}`, verify it opens and decodes to the same pixel dimensions, then replace. The previous file goes to `_versions/{id:08d}_v{file_version}.{ext}` exactly like cleanup; `file_version` bumps (sync re-pushes). Pixels are never re-encoded — metadata only (exiftool or an XMP/EXIF library that rewrites segments, not the image).
5. **Scope:** `triage_status in ('keep','private')`, `is_deleted=false`; backs excluded (`photo_backs` images are not photos). Digital photos included — their own EXIF stays; we add XMP/IPTC alongside and only set `DateTimeOriginal` when the file has none and the DB date is confirmed to the day.

## What is written (document the exact tags in `shared/SCHEMA.md` → "Embedded metadata")
- Date: EXIF `DateTimeOriginal` (day precision only), XMP `xmp:CreateDate` / `photoshop:DateCreated` partial where allowed.
- People: XMP `dc:subject` + IPTC Keywords = each person's `display_name` (common name); XMP `Iptc4xmpExt:PersonInImage` = full names; the description carries "People: Margaret 'Peggy' Clay (née Shaddock), …" with the full set of known names.
- Place: IPTC City/State/Country where parseable, `Iptc4xmpExt:LocationShown` name, GPS lat/lon when known.
- Description (`dc:description` / IPTC Caption): composed in a fixed order — human caption if any (albums/notes), "People: …", "Date: …" when not day-precise, "AI-generated description: …", "Back of print: …". Keep it under 2,000 characters; truncate the AI text first.
- Physical reference for scans: `Iptc4xmpExt`/XMP custom field `photoarchive:ScanLocator` = "Batch 00012 #017" and the `physical_ref_note`. Family archives outlive software; someone must be able to find the print.
- Archive identity: `photoarchive:PhotoId` and `photoarchive:MasterSha256` so a file found loose on a disk can be matched back.

## Incremental selection
New columns: `photos.metadata_written_at`, `photos.metadata_hash` (sha256 of the canonical payload). The selector composes the payload for every in-scope photo, hashes it, and writes only when the hash differs from `metadata_hash` — so a re-run after no fact changes touches zero files, and a single new face assignment touches one. Run as resumable chunks of ~250 in fresh processes (CLAUDE.md memory rule). Audit row `metadata.write` per file with the payload summary.

## Where it runs
- `python -m photoarchive.tools.write_metadata [--dry-run] [--photo ID] [--batch …] [--limit N]` — dry run prints, per file, every tag that would change.
- Sidebar: a **Metadata** entry with Run / Dry run, progress, last-run summary, and a per-photo "What's embedded" viewer (reads the file, shows the tags). 
- After every Pull, the Sync tab offers "Write metadata for N changed photos" (never automatic — George presses it).

## Interactions to get right
- A write bumps `file_version`, so the next push re-sends the file. Say so in the Sync tab ("N files will re-push"). The first full write touches every photo with any fact — expect most of them — so do it **before** the big group assignment/push, not after.
- Cleanup's `_versions/` and `check_working_files` already know the version pattern; reuse, don't duplicate.
- Photos with web comments: not relevant here (comments are never written) — but keep the pull's "has comments" summary untouched.
- TIFF: metadata-only rewrite must preserve 16-bit data and compression untouched; verify with a byte comparison of the image strips.

## Tests
Masters assert (a path under a master root raises; probe refuses when writable); day-precision date written, year-precision not written to EXIF but present in XMP/description; people common names in keywords and full names in description; AI description prefixed and only when fact; back transcription with unverified marker; atomic replace survives a simulated crash (temp file left, original intact); idempotent (second run writes nothing); hash changes on one new fact → one file; TIFF pixels byte-identical before/after; JPEG pixels identical (decode compare); dry run writes nothing.

## Verification, then stop
1. `pytest` green.
2. Dry run over the whole scope: counts by tag, 10 sample payloads. George checks 3 against the photos in the app.
3. Real run on **one batch** (chunked), then `exiftool -a -G1 <file>` on two of them pasted in the report; `check_working_files --dry-run` clean; open one in Windows Photos and confirm the people and date show.
4. Update `CLAUDE.md` (Metadata section with the five non-negotiables), `shared/SCHEMA.md`, `PROJECT-PLAN.md`; commit and push: `Phase 13: metadata writer`.

---

## Answers to Claude Code's questions
(added as they come)
