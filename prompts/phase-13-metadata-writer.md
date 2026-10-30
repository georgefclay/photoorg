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

## What Phase 15 changed underneath this prompt (PM, 2026-10-29)
This prompt was written on 2026-10-02, before Phase 15 (corrections) and its fix-up 1 landed and were deployed. Four consequences, all small:
1. **Soft-delete flags are everywhere now.** `album_photos`, `photo_places`, `person_name_variants` and `place_aliases` all carry `is_deleted`. Every selector the writer uses filters `is_deleted = false` — a removed album must not appear in the caption, a removed place must not become `LocationShown`, a removed nickname must not be written as one of a person's names. Add a test per table: remove, re-run, the tag is gone from the payload and the hash changes for exactly that photo.
2. **The writer is a job, not a human.** It never stamps `edited_on_desktop_at` / `edited_on_web_at` (CLAUDE.md: those are set only by a human edit in a UI). It does bump `file_version` exactly as Cleanup does — the file's bytes changed — and `metadata_written_at` / `metadata_hash` are its own columns.
3. **Web wording comes down on every Pull** (`pull_web_edits`: people names, place names, album names edited on the site). The hash-based selector already makes that correct — a pulled rename changes the payload hash for the photos it touches and nothing else — so "Write metadata for N changed photos" after a Pull needs no special case; just make sure the test for it exists (rename a person on the laptop as the pull would, N = that person's photos).
4. **Names in the description come from live rows only:** `people` columns (incl. `nickname`, `suffix`) plus live `person_name_variants`; the format stays "Margaret 'Peggy' Clay (née Shaddock)". Pending suggestions are still never written.

Also from the Phase 15 close-out, for your own setup: `desktop` pytest runs under `./.venv/Scripts/python.exe` only, and `desktop/tools/` are plain scripts (`python desktop/tools/run_push.py`), not package modules — both now in CLAUDE.md.

---

## Answers to Claude Code's questions
(added as they come)

### Claude Code's questions (2026-10-29)

Measured on `photoorg` first, because several answers depend on it. In scope
(keep/private, live): **12,686** — 12,538 JPEG, 148 TIFF, 0 private.
Confirmed dates: **8,594, every one `exact`** (8,573 digital, 21 scans);
4,092 scans are `unknown`. Photos with an assigned person: **5,353**.
`photos.description_ai` set: **0** (12,933 `description` suggestions, all
pending). Live places / `photo_places`: **0**. `photo_backs`: 850, 824
transcribed, **1 confirmed**. Live album memberships: 253 photos.
`physical_ref_note`: 49. `D:` has 1.7 TB free; `working/` is 29.3 GB.
`exiftool` is **not installed** on this laptop.

**Q1. Date precision vocabulary.** The enum is
`exact|month|year|decade|unknown`; the prompt says `day`. I'll read `day` as
`exact`. For `decade` (no valid partial XMP date exists), my proposal: no date
tag at all, description line only — "Date: 1960s". Today this is moot (every
confirmed date is `exact`), but the code and tests will cover it. OK?

**Q2. What is the "common name" in keywords?** `people.display_name` is
`Margaret "Peggy" Clay (née Schmidt)`, which is the long form, not the common
one. Proposal:
- Keywords (`dc:subject`, IPTC Keywords): nickname-or-given + surname + suffix
  → `Peggy Clay`, `George Clay Jr.`.
- `Iptc4xmpExt:PersonInImage`: the formal full name → `Margaret Jane Clay (née Schmidt)`.
- Description "People:" line: `Margaret 'Peggy' Clay (née Schmidt)` and live
  variants in brackets after it → `… (née Schmidt; also Maggie, Meg)`.
Is that the shape you want, and should the variants be there at all? They
include misspellings (`kind = misspelling`). I'd leave those out and keep
nicknames and alternate spellings.

**Q3. Face regions.** We have a named bbox for every assigned face. Writing
them as XMP regions (MWG-RS, plus Microsoft's `MP:RegionInfo`, which is what
Windows Photo Gallery and Windows "People" tagging read) means Lightroom,
digiKam, Picasa successors and Windows show *who is where*. A list of names
can't do that. Verification step 3 ("people show in Windows Photos") probably
depends on it. Add regions (named faces only, never unknown/ignored)?
**Recommended: yes.**

**Q4. Metadata already in digital files.** Many camera files carry
`ImageDescription = "OLYMPUS DIGITAL CAMERA"`, and some carry older XMP
keywords/descriptions from Picasa or Lightroom. Proposal: the writer **owns**
the tags it writes (description/caption/ImageDescription, keywords/subject,
PersonInImage, regions, location, `photoarchive:*`) and replaces them
wholesale every write. Merging would mean a removed person or album could
never leave the file. The camera's original values stay in the master.
Untouched: everything else (camera, exposure, GPS from the camera,
`DateTimeOriginal` when present). If the file's own `DateTimeOriginal` differs
from a confirmed DB date, I leave EXIF alone, as the prompt says, and write the
DB date to XMP `photoshop:DateCreated`. Agree?

**Q5. Nothing to write for AI descriptions or places, as things stand.** With
0 accepted descriptions and 0 places, those two sections will be built and
tested but write nothing on the first run. That fits "facts only". Just
confirming you don't want a bulk-accept of descriptions done *before* the first
full write. Each later accept would re-write (and re-push) the file.

**Q6. "Human caption (albums/notes)".** There's no caption column. Proposal for
the first description line: `Albums: <live album names>`. That's the corrected
names, so "Canada", never the master's "Canaca". I'd **not** write
`source_folder` as a caption, even though PROJECT-PLAN §2's folder-hints row
says the folder name goes to XMP, because it preserves master typos. Instead
the provenance goes in separate fields, labelled as records, not description:
`photoarchive:SourceFolder` / `photoarchive:SourceFilename` (spec Phase 13
lists "original filename" with the physical reference). For a split child,
`ScanLocator` gets `Batch 00012 #017, print 2 of 3`. OK?

**Q7. A replaced working file loses its metadata, and the hash wouldn't
notice.** If Cleanup accepts, a rescan is accepted, or `check_working_files`
rebuilds from `_versions/` or the master, the payload hash is unchanged, so the
selector would never rewrite. Proposal: a third column
`photos.metadata_file_version` (the `file_version` right after our write), and
the selector writes when the hash differs **or** `file_version ≠
metadata_file_version`. No other writer needs to know about Phase 13. OK?

**Q8. Installing exiftool.** I'd use exiftool, in one `-stay_open` process per
chunk. It rewrites metadata segments without touching image data, including
TIFF strips. It needs a system install: `winget install OliverBetz.ExifTool`,
then `EXIFTOOL_PATH` in `desktop/.env` with a startup check that refuses to run
if it's missing. May I install it, or would you rather do it yourself?

**Assumptions I'll proceed on unless you say otherwise:**
- Every metadata write keeps the previous file in `_versions/` forever, like
  Cleanup. That's ~29 GB per full pass against 1.7 TB free.
- An unconfirmed back is written as "Back of print (unverified transcription): …".
  Only 1 of 824 is confirmed.
- The identity fields (`PhotoId`, `MasterSha256`, `ScanLocator`) put every
  in-scope photo in the payload, so the first full write touches all
  **12,686** files. That's in line with "expect most of them".
- The new `photos` columns go in a shared migration (same schema on the VM).
  They are not pushed.

### Answers (PM, 2026-10-29)

Good measurements; three of them change what the first run does, and I've said where. Yes to all eight, with these specifics.

1. **Yes.** `exact` = day. `month` / `year` → partial XMP dates only (`photoshop:DateCreated` `YYYY-MM` / `YYYY`, `xmp:CreateDate` the same) plus the description line; `decade` → description line only ("Date: 1960s"); `unknown` → nothing at all — no "Date: unknown" line, absence is the statement. One more rule under non-negotiable 3: **never fabricate a time of day.** EXIF `DateTimeOriginal` needs `HH:MM:SS`; write it only when the DB holds a time (it does not for the 21 day-precise scans). For those, XMP date-only is the whole answer. Today that means the EXIF date path never fires — the 8,573 digital files already carry theirs — but the code and tests cover it.

2. **Yes, that shape.** Keywords = nickname-or-given + surname + suffix (`Peggy Clay`, `George Clay Jr.`), one keyword per person; if the formula yields nothing, fall back to `display_name`. `PersonInImage` = the formal full name with née. The "People:" line = `Margaret 'Peggy' Clay (née Schmidt; also Maggie, Meg)` with live variants of kind nickname / alternate spelling **only — `misspelling` stays out of the file.** Variants are `is_deleted = false` rows (Phase 15 section).

3. **Yes — regions, named faces only, both MWG-RS and `MP:RegionInfo`.** And the thing that will put boxes off the faces if it is skipped: **`faces.bbox` is in the display frame (EXIF-transposed, Phase 6 fix-up 6); region coordinates are relative to the *stored* pixels.** Map every box through the inverse of `photos.orientation` before normalising, and take `AppliedToDimensions` from the file being written, never from the DB (a cleaned scan's working file is not the master's size). Tests: fixtures with orientation 1, 6 and 8, each read back with exiftool and the region re-cut from the stored pixels compared against the face crop — the same check fix-up 7 used. Unknown / ignored faces never go in the file.

4. **Yes — own and replace, wholesale, every write.** Camera and exposure data, camera GPS, and an existing `DateTimeOriginal` are untouched; a confirmed DB date that disagrees with the file's EXIF goes to XMP only, as you say. Two additions to the dry-run report, both counts plus a CSV under `WORKING_DIR/_report/`: (a) **pre-existing values in tags we own that are not ours** — Picasa / Lightroom keywords, descriptions and above all *named face regions* are family labelling that belongs in `suggestions`, not under a bulldozer; the masters still hold them, so nothing is lost, but I want the numbers before deciding whether an import job is worth writing; (b) **confirmed DB date ≠ file `DateTimeOriginal`**, so George can see how often the viewers will show the camera's date instead of his.

5. **No bulk-accept before the first write.** The first write is the settled truth, and 12,933 unreviewed descriptions are not it. Build the path, test it, write nothing. Whether to bulk-accept later is George's decision, and the cheapest moment for it is *after* Phase 13 closes and *before* the group bulk-assignment, since every accept re-writes and re-pushes the file — recorded as PROJECT-PLAN §5 item 19.

6. **Yes.** First description line `Albums: <live album names>` (corrected names, never the master's); `photoarchive:SourceFolder` / `SourceFilename` as provenance records, not caption — that satisfies PROJECT-PLAN §2's folder-hints row; `ScanLocator` gets `Batch 00012 #017, print 2 of 3` for a split child, and `physical_ref_note` rides with it. Albums are `album_photos.is_deleted = false` rows.

7. **Yes** — `photos.metadata_file_version`, selector writes when the hash differs **or** `file_version ≠ metadata_file_version`. Add one check: every path that replaces a working file's bytes must bump `file_version` — Cleanup and the rescan accept do; confirm `check_working_files`' rebuild-from-`_versions/` and copy-from-master paths do too, and add a test that a rebuilt file is re-selected. If either path does not bump, fix it there (it is the sync contract as well, not just ours).

8. **Yes, install it** (`winget install OliverBetz.ExifTool`); `EXIFTOOL_PATH` in `desktop/.env` with the startup refusal, the version pinned in a comment in `.env.example`, and the install noted in `GC.md`. Two rules for how it is driven: **never `-overwrite_original` on the working file** — write to `_tmp/{id}.{ext}` with `-o`, verify, then the Cleanup swap, exactly as non-negotiable 4 says; and a photo whose working file is missing or undecodable is **skipped and listed, never aborts the chunk** (the hand-over rule from Phase 6 fix-up 11).

Assumptions: all four accepted. On `_versions/` forever at ~29 GB per full pass — fine against 1.7 TB, and later passes only touch what changed; if a bulk-accept ever triggers a second full pass we will look at whether byte-identical-pixel versions deserve a dedupe, not before. The first full write of all 12,686 stays **before** the group bulk-assignment, as the plan says.

GO.

### Claude Code — verification 2 result and one question (2026-10-29)

Whole-scope dry run, 51 chunks, 516 s: **12,686 would be written, 0 skipped,
0 failed.** Report: `D:\PhotoArchive\working\_report\metadata\20261029-090920-dry\`.
Files by tag: identity 12,686; date 8,594; description 5,808; people
keywords / PersonInImage / regions 5,353; ScanLocator 4,094; PhysicalRef 49.
Pre-existing values in owned tags: 2,431 files, almost all camera captions in
`IFD0:ImageDescription` ("My beautiful picture" ×90, "OLYMPUS DIGITAL CAMERA",
"IM000038.JPG"…), 24 with XMP keywords ("Fall Area Camporee", "Oshkosh 2009"),
36 with face regions — **none of them named**. Confirmed date ≠ camera date: 0.

**Q9. Scan dates that are scanner clocks.** 21 scans carry
`capture_date_confirmed = true` with `exact` precision, and no audit row
ever set them: ingest took them from the *scan file's* EXIF. They are the
scanner/phone clock — `2025-11-07`, `2025-11-21`, `2025-11-23` — or a device
clock set to a guessed January 1st (`1947-01-01`, `1949-01-01`, `1963-01-01`).
Written as-is, 21 prints would say they were taken when they were scanned,
and 18 (cleaned files that lost their EXIF) would get it back as
`DateTimeOriginal`. Batches 00009, 00024, **00033 (photo #2693)**, 00045,
00047, Photo Book. Proposal: **on a scan, a date is written only when a human
set it** (a `photo.capture_date.set` audit row — the accept path writes one),
so the 21 write no date until someone confirms one, and the writer never puts
a scan date into EXIF. Separately, they should probably be un-confirmed in the
DB, but that is a fact change, not mine to make in this phase.

Also seen, data not code: person #? on #1053 has nickname `Pete, JR` (the
suffix typed into the nickname), so the keyword reads `Pete, JR Clay` — a
People-editor fix. Digital photos whose camera clock was never set (#4096:
`2004-01-01 00:00:11`) carry that as a confirmed date; the writer only echoes
the camera's own value into XMP, so I've left it.

Waiting on Q9 before the Batch 00033 real run.

### Verification log (Claude Code, 2026-10-29)

Q9 answered yes: a scan's date is written only when a person set it, never
into a scan's EXIF. Selection after the change: 8,573 dated payloads (the 21
scanner-clock scans dropped out).

**Batch 00033, real run** (97 photos, 2 TIFF): 97 written, 0 skipped, 0
failed, 15 s; masters guard read-only in both processes. Second run: 0 due.
`check_working_files --dry-run`: 12,686 already_ok, every post-condition 0.
#2693 (the batch's scanner-clock scan): no date tags written, its own camera
`DateTimeOriginal` untouched.

Found on the real files and fixed before closing:
1. **Two people with the same formal name** (#2608: George Frederick Clay,
   father and son) were collapsed into one `PersonInImage` entry by a text
   de-dup. Now one entry per person. Re-run: exactly 7 photos due ("facts
   changed"), 2 tags each.
2. **Windows invents January 1st.** It reads `xmp:CreateDate` as "Date
   taken" and shows a year-only value as 1/1/1962 12:00 AM. Partial dates
   now go only to `photoshop:DateCreated` (which Windows ignores) and the
   description; `xmp:CreateDate` / `IPTC:DateCreated` are day-only.

`exiftool -a -G1` on two of them (camera/format rows trimmed):

```
=== 00002608_54ce1de3.jpg
[IFD0]   Image Description  : People: Lola Joan Jones (née Clay), Samuel Kenneth Clay, George Frederick Clay, Dorothy Jean Clay (née Boots), George Frederick 'Pete, JR' Clay, Carol Ann Clay.Back of print (unverified transcription): 1983
[IPTC]   Keywords           : Lola Jones, Samuel Clay, George Clay, Dorothy Clay, Pete, JR Clay, Carol Clay
[XMP-iptcExt] Person In Image : Lola Joan Jones (née Clay), Samuel Kenneth Clay, George Frederick Clay, Dorothy Jean Clay (née Boots), George Frederick Clay, Carol Ann Clay
[XMP-MP] Region Person Display Name : Pete, JR Clay, Carol Clay, Dorothy Clay, Samuel Clay, Lola Jones, George Clay
[XMP-mwg-rs] Region Applied To Dimensions : 2636 × 2016 pixel; 6 Face regions, normalized
[XMP-mwg-rs] Region Name    : Pete, JR Clay, Carol Clay, Dorothy Clay, Samuel Clay, Lola Jones, George Clay
[XMP-photoarchive] Master Sha 256 : 54ce1de32f8f06f1a07ac5a603e92fc158ad55a46103240978e847b6ade07b6f
[XMP-photoarchive] Photo Id       : 2608
[XMP-photoarchive] Scan Locator   : Batch 00033 #023
[XMP-photoarchive] Source Filename: 2025-11-06-10-41-0006.jpg
[XMP-photoarchive] Source Folder  : Batch 00033
=== 00002604_6fcf49e3.tif  (TIFF)
[IFD0]   Image Description  : Back of print (unverified transcription): Dorothy & George Clay
[XMP-dc] Description        : Back of print (unverified transcription): Dorothy & George Clay
[IPTC]   Caption-Abstract   : Back of print (unverified transcription): Dorothy & George Clay
[XMP-photoarchive] Photo Id       : 2604
[XMP-photoarchive] Scan Locator   : Batch 00033 #017
```
(The "." before "Back of print" is exiftool's display of the newline.)

Windows property system on #2608: **People** = Pete, JR Clay; Carol Clay;
Dorothy Clay; Samuel Clay; Lola Jones; George Clay — **Tags** = the six
keywords — **Title/Subject** = the description. Date taken is blank, which
is right (no confirmed date). On a scratch file a day-precise
`xmp:CreateDate` shows as Date taken 2/1/1968.

**For George:** open `D:\PhotoArchive\working\00002608_54ce1de3.jpg` in
Windows Photos and confirm the people show. For a dated photo, the Batch
00033 run had none; any digital photo after the full write will do.

---

## Phase 13 fix-up 1 — date hygiene (PM, 2026-10-29)

PM review of 878f89f: the build is accepted. The three catches on real files (scanner-clock dates, Windows' January 1st, father-and-son de-dup) were all the right calls, and the Q9 answer stands. But Q9 is a workaround for a data fault the writer did not cause: **ingest confirmed dates it had no business confirming.** 21 scans carry `capture_date_confirmed = true` from the scan file's EXIF with no `photo.capture_date.set` audit row — scanner or phone clocks, or a device set to a guessed January 1st — and an unknown number of digital photos carry an unset camera clock the same way (#4096: `2004-01-01 00:00:11`). A wrong confirmed date is a wrong fact: it feeds the web photo page, completeness, decade search, and Phase 12's year/month zip tree, where a 1960s print files under 2025. **Run this after the full write and before Phase 12.**

### 1. Report first — no writes
`python -m photoarchive.tools.date_hygiene --report`: every confirmed date with no `photo.capture_date.set` audit row (i.e. ingest-sourced), flagged by rule: **(a)** the photo is `is_scan` or on a `|scan` root; **(b)** January 1st with a time of day under 00:01:00; **(c)** disagrees with the `_YYYY-MM` folder hint by more than 3 months; **(d)** later than the file's own ingest date, or before 1826. Counts per rule and per batch, a CSV under `WORKING_DIR/_report/date-hygiene/`, and 10 sample rows pasted here.

### 2. Un-confirm, audited — never SQL
`--apply --rules a,b[,c,d]` sets `capture_date_confirmed = false` on the flagged rows and keeps the old value as a `date` suggestion (`source='import'`, `details.reason=<rule>`, low confidence) so nothing is lost and the Suggestions block still shows where it came from; one `photo.capture_date.unconfirm` audit row per photo with previous/new; `refresh_completeness` on each; the search index follows by trigger. George picks the rules from the report — default **a + b**; c and d only if their numbers look right to him.

### 3. The edit path that was missing
Today a confirmed date can be overridden by force-accepting another suggestion but never cleared. Add **Clear confirmed date** to the desktop Suggestions block and to the web admin photo page (`POST /api/admin/photos/:id/capture_date/clear`), both writing `photo.capture_date.unconfirm` with previous/new, both syncing the way a date accept does — the push carries the cleared fact, the web's clear comes down through `pull_confirmed`'s fact-set audit rows. Phase 15's rule applies to facts as much as to text: fixing a mistake must never need SQL.

### 4. Ingest stops creating these
On a `|scan` root or `is_scan`, an EXIF date becomes a `date` suggestion (`source='import'`, `details.reason='scanner_clock'`), never a confirmed fact. On a digital root, an EXIF `DateTimeOriginal` of January 1st with a time under 00:01:00 becomes a suggestion with `details.reason='unset_camera_clock'`. Tests for both; every other digital EXIF date stays confirmed exactly as before.

### 5. Then the writer
Re-run `write_metadata`: the un-confirmed photos drop their date tags and only they are due ("facts changed"). Paste the count — it must equal the rows un-confirmed — and confirm no scan gained a date.

### 6. The GUI push must never die silently (added 2026-10-29, evening)
George's first Sync-tab push today was killed hard — no exception, no line in `photoarchive.log` between app start and the restart four hours later; the worker only logs on a Python failure and progress goes to the pane alone. Mistakes are fixable only if they leave a trace: the sync worker logs every stage start and end, the per-stage counts and the final summary to the log file as well as the pane; the app installs a `sys.excepthook` and a Qt message handler so an uncaught error or a Qt abort writes its last words before the process goes; and the Sync tab shows the resident memory next to the progress line, since low memory is the usual killer on this laptop (CLAUDE.md, Cleanup). `run_push.py` already does the terminal equivalent; the GUI path must match it.
Evidence so far: the app window vanished after the push had already delivered its 139 files (the next terminal push was asked for 0), with no traceback and no log line — the Phase 7 pattern, a finished `BackgroundJob` dropped while Qt still owns the thread (CLAUDE.md, Cleanup, "two UI rules"). Check the Sync worker against that rule first. And while on the VM read-only: confirm `synced_file_version = file_version` for all 139 grouped photos and paste the count — that is Phase 13's closing number.

### Tests, verification, docs
pytest green; the report pasted; apply on George's rules with before/after counts; the writer re-run count; `CLAUDE.md` Metadata section gains "a scan's EXIF date is never a fact; an ingest-sourced date can be cleared from the UI"; `PROJECT-PLAN.md` Phase 13; commit and push `Phase 13 fix-up 1: date hygiene`.

## Answers to Claude Code's fix-up 1 questions
(added as they come)

### Claude Code's questions (2026-10-30)

**F1. Un-confirm only flips the flag, or does it clear the value too?** As
written, `--apply` sets `capture_date_confirmed = false` and leaves
`capture_date` in place. Most web readers never look at the flag: Browse's
oldest/newest sort (`services/photos.js:15`), the year/decade filters
(`photos.js:64`, `admin.js:515-578`), a person's year span (`people.js:53`),
and the search index's `photo_date_range(capture_date, …)` gist. A 1960s
print would still sort and search as 2025. Proposal: **un-confirm clears
it** (`capture_date = null`, `precision = 'unknown'`, `confirmed = false`).
The old value survives in the `import` suggestion and in the audit row's
`previous`. The same applies to **Clear confirmed date** (item 3).

**F2. Which dates can Clear confirmed date clear?** Item 3 reads as *any*
confirmed date. The CLAUDE.md line in "Tests, verification, docs" reads as
*ingest-sourced* only. Proposal: **any**, because a person's wrong accept is
also a mistake that must not need SQL. The old value goes back as a `date`
suggestion with `source='human'` when a person set it and `source='import'`
otherwise, `details.reason='cleared'`, so it can be re-accepted with one click.

**F3. The writer re-run count will not equal the rows un-confirmed.** Q9's
gate already keeps scans without a human-set date from writing a date. So
the rule-(a) rows have no date tags to drop, their payload hash doesn't
change, and they won't come up as due. What I'll actually check: **due =
un-confirmed non-scan rows (rules b/c/d)**, rule-(a) rows due = 0, and no
scan carries a date tag. Q9's gate stays as a second line of defence. OK?

**F4. Rule (c) has no stored hint to compare against.** Ingest writes the
`_YYYY-MM` folder suggestion only when the photo has *no* EXIF date
(`_record_folder_hints`, `has_exif`). So every confirmed-from-EXIF photo has
no folder suggestion. The report will parse `source_folder` with the same
`parse_date_folder` instead. Noting this, not asking.

**F5. Should the ingest guard also cover rule (d)?** Item 4 names scans and
the January 1st unset clock. Proposal: an EXIF date **after the master
file's mtime, or before 1826**, also arrives as a suggestion
(`details.reason='implausible_date'`), because the same wrong fact can
otherwise come back on the next contrib pull. Yes / no?

**F6. The Sync tab's worker lifecycle (item 6), what I see before testing
anything.** `_run_worker` stores the worker in `self._worker` (no Qt
parent) and the thread in `self._thread`. Nothing stops a second Pull /
Push / Write-metadata click while one is running. A second click overwrites
both, so the running worker's last Python reference goes away mid-run. That
is the Phase 7 rule (a) shape, and there's no `closeEvent` join either. The
fix: one-job-at-a-time buttons, a `_live_jobs` park until `finished`, and
join on close, plus the logging, excepthook, Qt handler and RSS display the
prompt asks for. **For George:** during that first push, did you click
anything else on the Sync tab (Pull, or "Write metadata for N changed
photos" when it appeared)? Or did you switch modes or close a dialog? Either
answer is useful. If you did nothing, memory is the suspect, and the RSS
display and the log lines are what will show it next time.

### Answers (PM, 2026-10-30)

Yes to all six, with these specifics.

**F1. Yes — clear the value, not just the flag.** `capture_date = null`, `capture_date_precision = 'unknown'`, `capture_date_confirmed = false`; the old value survives in the suggestion and in the audit row's `previous`; `refresh_completeness` runs. One addition for the digital rows this clears (rules b/c/d): if the photo's `source_folder` parses as `_YYYY-MM`, also insert the folder-month suggestion (`source='import'`, precision `month`) when none exists — that is exactly what ingest would have recorded had it not trusted the camera, so the Suggestions block offers the next-best date instead of nothing.

**F2. Any confirmed date.** A person's wrong accept is a mistake like any other. `source='human'` when a person set it, `source='import'` otherwise, `details.reason='cleared'`, re-acceptable in one click. Fix the CLAUDE.md wording accordingly: "any confirmed date can be cleared from the desktop or the web admin page; the cleared value returns as a suggestion". Admin-only on the web.

**F3. Yes, your check is the right one.** Due = cleared non-scan rows; rule-(a) rows due = 0; no scan carries a date tag; Q9's gate stays. Paste the three numbers.

**F4. Noted, agreed** — parse `source_folder` with `parse_date_folder`.

**F5. Yes.** An EXIF date after the master file's mtime (allow one day of slack for time zones) or before 1826 arrives as a suggestion with `details.reason='implausible_date'`. Test with a contrib pull fixture, since that is the path it would come back through.

**F6. Build all of it regardless of what George did** — one job at a time on the Sync tab (every button that starts work shares the one slot and says which job is running), the `_live_jobs` park until `finished`, cancel-and-join in `closeEvent` with a confirm dialog when a job is running, plus the logging, `sys.excepthook`, Qt message handler and RSS display from item 6. The second-click path is a real bug whether or not it fired on 2026-10-29. George will tell you in the session what he clicked; if the answer is "nothing", memory is the suspect and the new log lines and RSS figure are what will show it next time. While on the VM read-only, confirm `synced_file_version = file_version` for the 139 grouped photos and paste the count.

GO.

### Fix-up 1 verification log (Claude Code, 2026-10-30)

**Report** (`date_hygiene --report`, read-only; `D:\PhotoArchive\working\_report\date-hygiene\20261030-063951\`):
9,646 ingest-sourced confirmed dates; 24 flagged. Rule a (scan) 21, b (unset
clock) 3, c (folder > 3 months) 0, d (after ingest / before 1826) 0 — so a+b
and a+b+c+d are the same set. Per batch: 00009 1, 00024 1, 00033 1, 00045 8,
00047 2, Photo Book 8 (all a); `photos:_2004-01` 3 (b). Samples:

```
photo_id | capture_date | exif_taken_at       | kind    | batch           | rules
676      | 1968-02-01   | 1968-02-01 10:05:21 | scan    | Batch 00009     | a
1907     | 1963-01-01   | 1963-01-01 08:52:59 | scan    | Batch 00024     | a
2693     | 2025-11-07   | 2025-11-07 12:07:57 | scan    | Batch 00033     | a
3739     | 2025-11-11   | 2025-11-11 17:34:16 | scan    | Batch 00045     | a
3741     | 2025-11-23   | 2025-11-23 10:03:14 | scan    | Batch 00045     | a
3742     | 2025-11-23   | 2025-11-23 10:03:29 | scan    | Batch 00045     | a
3745     | 1949-01-01   | 1949-01-01 10:00:32 | scan    | Batch 00045     | a
3746     | 1947-01-01   | 1947-01-01 09:58:11 | scan    | Batch 00045     | a
4096     | 2004-01-01   | 2004-01-01 00:00:11 | digital | photos:_2004-01 | b
4097     | 2004-01-01   | 2004-01-01 00:00:19 | digital | photos:_2004-01 | b
```

**Apply** (`--apply --rules a,b`, default; one transaction): un-confirmed
a=21, b=3 (**24**); 24 `photo.capture_date.unconfirm` rows + one
`date_hygiene.apply`; each old value is a pending `date` suggestion
(`source='import'`, `payload.reason`/`rule`); 3 folder-month suggestions
(`_2004-01`) added for the digital three. Ingest-sourced confirmed dates
9,646 → 9,622.

**Writer re-run** (F3's three numbers): due = **3** (#4096–4098, "facts
changed", all digital = the cleared non-scan rows); cleared scans due = **0**;
scans carrying a date tag = **0**. Real run: wrote 3, skipped 0, failed 0;
#4096 keeps only its camera's own `DateTimeOriginal`; second run 0 due;
`check_working_files --dry-run` post-conditions 0.

**Item 6 — the GUI push that vanished.** George pressed Push and nothing
else; the app closed as the push finished. Cause found and reproduced: the
Sync tab's `finished` handler was a **lambda**, and PySide6 6.11 runs a
receiver-less lambda on the *emitting* thread — so `setVisible` on the
metadata button, `setText` and `append` ran on the worker thread, which kills
the process with no Python traceback. A test that observes the real slots
fails with the old lambda (`('halfway', <worker thread>)`) and passes now.
Also found: a plain function patched over a `@Slot` name runs on the emitting
thread too — the test is written around that. Built: one job slot (every
work button disabled while one runs, the pair held until `finished`),
`shutdown()` stopping at the next progress callback and joining, a confirm in
`MainWindow.closeEvent`, stage start/end + counts + memory in the log,
`sys.excepthook` / `threading.excepthook` / Qt message handler,
`faulthandler` → `%LOCALAPPDATA%\PhotoArchive\logs\crash.log`, an
"exiting rc=" last line, and the memory figure beside the progress line.

**VM (read-only):** grouped live photos with `synced_file_version =
file_version`: **139 / 139**, 0 behind. (A second read cross-checking each id's
version against the laptop was refused by the auto-mode classifier.)

**Deviation:** `suggestions` has no `details` column; reasons ride in the
payload (`payload.reason`, `payload.rule`). A web clear's returned suggestion
stays on the web (web-born, like every web suggestion); the desktop applies
the clear and adds only the folder-month hint. The web cannot see a desktop
accept, so a desktop-set date cleared on the web returns as `source='import'`.

Tests: desktop 727 passed / 1 skipped in the full run, plus the 2 tests in
`test_date_clear_ui.py` written while it ran (green on their own); web 211 passed. New: `test_date_hygiene.py` (14),
`test_ingest_date_guard.py` (1, real `run_ingest` over digital / scan /
contrib roots; confirmed failing with the guard off), `test_sync_ui_jobs.py`
(5), `test_date_clear_ui.py` (2), `web/test/date-clear.test.js` (3).
Needs a VM deploy (web code only, no migration) for the web Clear link and
`/sync/pull/confirmed` carrying `photo.capture_date.unconfirm`.

**George's push after the deploy (2026-10-30 07:39, the fixed Sync tab):**
done in 269.9 s — 13,290 photos, 13,533 masters, 23,464 faces, 34,849
suggestions, 845 backs, 0 files, 0 failed stages; the window stayed open and
every stage start/end is in `photoarchive.log` with memory (peak 799 MB at
the album_photos → faces boundary, back to ~360 MB after); `crash.log` empty.
That push carried the 24 un-confirms to the VM (a read-back of them on the VM
was refused by the auto-mode classifier — check #2693 on the site: "No date
yet"). The new Qt handler also caught one thing on its first day: the 07:20
`--smoke` exit logged `QThread: Destroyed while thread '' is still running` —
another mode's startup job, not Sync; PROJECT-PLAN §5 item 22.
