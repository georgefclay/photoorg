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
- **Desktop and web never share a database on the same machine.** The
  laptop web server uses `photoorg_web`; the desktop uses `photoorg`.
  The web's sync routes store `working_path` as a bare basename (correct
  on the VM), so a push into a shared DB rewrites every desktop
  `working_path` (fix-up 11, 2026-09-14). `push()` pre-flights
  `GET /sync/status` and refuses when the web reports our own
  `(current_database, system_identifier)`; `allow_shared_db=True` exists
  only for the one test fixture that deliberately shares `photoorg_test`.
- **Id ranges: desktop below, web at or above `WEB_ID_FLOOR`** (Phase 9
  fix-up 1). One constant, `shared/id-ranges.json` (`1 000 000 000 000`;
  every listed table is bigint), read by the shared migration, the web
  (`services/id-floor.js`) and the desktop (`photoarchive.id_ranges`).
  Tables: `faces`, `people`, `suggestions`, `albums`, `places`,
  `relationships`, `person_name_variants`. On a **web** DB the sequences
  start at the floor — migration `phase-9-fixup-1-web-origin-ids` moves
  them only when `PHOTOORG_DB_ROLE=web` is in the migrate environment
  (VM `shared/.env`; inline for the laptop's `photoorg_web`; the web
  `test:setup`). Never set it for `photoorg`. The web server checks the
  sequences on start and refuses to run in production below the floor
  (`node tools/id-floor.js [--apply]`). Sync never crosses the ranges:
  every `/sync/<table>` push with an id ≥ floor is a 400 and upserts
  never update a row ≥ floor; the desktop push selects `id < floor`;
  `/sync/pull/web_origin` copies web-born people / places /
  relationships / faces down with their web ids (faces: `source='human'`,
  `embedding=null`, `embedding_stale=true`, crop cut at pull). Web-born
  rows are web-authoritative — a desktop edit to one is overwritten by
  the next pull and never pushed. After any VM restore, check
  `/sync/status` → `id_floor.ok`.
- **Push pulls first, always.** `push()` runs `pull_groups` +
  `pull_confirmed` (which runs `pull_web_origin` first) before sending
  anything, so a web change (rescan flag, accepted suggestion, group
  removal, web-drawn face) can't be clobbered by a stale push. A web
  accept/reject of a desktop-pushed suggestion is mirrored onto the
  laptop's copy, and the web's suggestion upsert never re-opens a
  resolved suggestion.

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

## Cleanup (Phase 7 onwards)

### The four invariants
1. **Cleanup writes a new derived file; it never overwrites.** On Accept the
   current working file moves to `WORKING_DIR/_versions/{photo_id:08d}_v{file_version}.{ext}`
   (**kept forever**), the render takes the standard working name,
   `file_version` bumps (so sync re-pushes), `photos.width/height/file_size`
   update, the thumbnail regenerates, and `photos.phash/dhash` are
   **recomputed from the new pixels** in the same statement (there is no
   staleness flag — fix-up-free by construction). `photos.sha256` stays the
   master's: it is the photo's identity, not the working copy's.
2. **Face boxes travel with the pixels.** Every geometric op composes into one
   affine (`modes/cleanup/geometry.Transform`) and Accept applies that same
   transform to every `faces.bbox`, regenerates the face crops, and keeps the
   embeddings. `apply_bbox` maps the box **centre** exactly and preserves w/h —
   exactly invertible, and more honest than growing the box to the
   axis-aligned bounds of the rotated corners, because the face rotates with
   the image. That only holds for small angles, which is why anything over
   `CLEANUP_MAX_DESKEW_DEG` (15°) is `needs_manual`. Tonal ops leave boxes
   alone. A box outside the new frame (less than 50 % of its area surviving),
   or straddling two split regions, is soft-deleted with
   `delete_reason='cleanup_out_of_frame'` — never silently dropped.
3. **Format follows the source.** JPEG in → JPEG q95 out; TIFF in → TIFF (LZW)
   out. `ops.py` is dtype-agnostic, so a 16-bit **greyscale** TIFF stays
   16-bit (`I;16`) end to end. Pillow has no 16-bit RGB mode — `fromarray` on
   a 3-channel uint16 array raises — so multi-channel high-bit-depth input is
   reduced to 8 bits once, in `load_display_array`, rather than blowing up at
   save time. Pillow's TIFF reader already hands 16-bit RGB over as 8-bit.
4. **Masters untouched.** The ingest masters-guard probe runs at the start of
   every `cleanup_analyse`; any writable root raises `MastersWritable` and the
   run refuses, printing the `icacls` deny.

### Scope and proposals
- **Scope:** `is_scan` (not the root kind — that also catches the 19 scans
  filed under the digital root), `triage_status in ('keep','private')`,
  `is_deleted=false`, **minus back-shaped photos** (a `possible_back` triage
  hint or any `ingest_pairings.back_photo_id` row), exactly as Dedupe
  excludes them. Backs get their own pass later. Fronts *with* a
  `photo_backs` row are in scope.
- **Everything is a proposal.** `cleanup_proposals` holds
  `pending|accepted|rejected|manual|clean|superseded`, one live `pending` per
  photo (partial unique index). A photo where nothing would change is
  `clean` and never reaches the queue. Re-analysis marks the old row
  `superseded` rather than deleting it — `pending` **and `clean`**, since a
  clean photo has no decision to preserve and leaving its old row behind
  double-counted it in the report. A decision (`accepted`, `rejected`,
  `manual`) is never superseded.
- **Analysis stores a plan, not a file.** `operations` (jsonb) holds what was
  measured; the full-resolution derivative is cut **on demand** — when the
  review pane zooms to 1:1, and again at Accept with exactly the ops still
  ticked. Only a ~2000 px preview is written at analysis time. Pre-rendering
  4 000 derivatives would cost 8–10 GB most of which gets re-rendered.
- **Tonal ops are opt-in** (fix-up 2). `CLEANUP_COLOUR_ENABLED` and
  `CLEANUP_LEVELS_ENABLED` are **false** by default, so the review queue is
  geometry only: deskew, crop, split. The analyser still measures colour cast
  and levels on every scan — the numbers are cheap and useful — and records
  them under `operations.ops_disabled`, where nothing will apply them. The
  colour op's wins did not pay for its losses on the batch 1–5 review even
  after fix-up 1; levels followed it off rather than being the one unreviewed
  tonal change. All of fix-up 1's safeguards stay in the code, so switching
  either back on is a setting, not a revert. `render.default_ticked(ops,
  settings)` applies the switches, which is how a proposal analysed before the
  change opens with its tonal ops **unticked** instead of needing its row
  rewritten; `plan_from` refuses a disabled op even if a caller ticks it.
- **The deskew angle comes from the print's edges, never from its enclosing
  box** (fix-up 2). `cv2.minAreaRect` returns the minimum *enclosing*
  rectangle, whose orientation is pinned by whatever sticks out furthest — a
  torn corner, a spur of bed, the print running off the edge of the scan. On
  photo #306 it read +2.27° on a print that was straight, and deskewing by it
  tilted the photograph. `analyse.edge_orientation` instead simplifies the
  outline to straight runs, folds each run's direction into [−45, 45) and
  takes the **length-weighted consensus**: four long edges outvote a torn
  corner. Below `EDGE_MIN_CONFIDENCE` (0.55) no deskew is proposed and the
  caption says "deskew skipped (print edges disagree)"; the crop extent is
  measured in that same straightened frame (`_extent_at_angle`). Across
  batches 1–5 this took wrong-direction deskews from **10 of 41 to 0 of 45**.
  A deskew test must check **direction and residual tilt** — the original
  assertion took `abs()` twice and could not tell +3° from −3°, which is how
  #306 shipped.
- **Analyse at ≤ `CLEANUP_ANALYSE_EDGE` (2000 px), apply at full resolution**,
  one image in memory at a time. *Memory is a real constraint, not a slogan*:
  the biggest scan is 93.7 MP = 281 MB as uint8 RGB, and one float32 copy of
  that is 1.1 GB. So (a) both tonal ops compose into **one lookup table per
  channel** (`ops.tone_luts`, 256 entries for uint8 / 65536 for uint16), mapped
  in a single pass — and in place when the array is a warp result nobody else
  holds; and (b) `render_preview` reduces the source *before* the warp and
  scales the transform with `Transform.scaled_by`, so a thumbnail never puts a
  93 MP array through a full-resolution rotation. Measured on that scan:
  preview 7 MB peak, accept-time full render 188 MB, analysis 75 MB. The LUT
  is within one level of the direct arithmetic and rounds once instead of
  twice, so it is marginally *more* accurate. Every measurement is recorded in the
  **full-resolution display frame**, so nothing needs re-analysing to apply.
- **All thresholds live in `desktop/.env`** (`CLEANUP_*`, documented in
  `.env.example`) so they retune without a code change. The crop inset
  **scales with DPI**: `CLEANUP_CROP_INSET_PX_AT_300` px at 300 DPI, so 16 px
  at 1200; unknown DPI uses the base value.
- **Bed detection tries white and black.** A saturated pixel only counts as
  print above a brightness floor (`BED_SAT_MIN_VALUE`): HSV saturation is
  `(max-min)/max`, so a near-black pixel with a few levels of scanner noise
  reads as *highly* saturated and a black bed would otherwise mask in as one
  print covering the whole scan.
- **A crop removes scanner bed, never pixels of the photograph** (fix-up 3).
  Two lines of defence, because the second one holds even when the first is
  wrong:
  1. **The print mask keys on calmness, not brightness** (`CLEANUP_MASK_MODE`,
     default `calm`). A pixel is bed only when it is near the bed tone, *locally
     flat*, **and** reachable from the scan's border — an overexposed sky
     inside a print is pale and calm but enclosed by photograph. The old
     brightness mask ("darker than bed − 25") called photo #15's white curtain
     and a grandmother's white cardigan bed, so the rectangle started 290 px
     inside the picture and the crop took her arm. **The flatness window must
     scale with the image** (`CLEANUP_MASK_STD_WINDOW_FRAC`, 0.013 of the long
     edge): a fixed 7 px sits inside one smooth fold of that curtain at 2000 px
     and reads as calm as bed, while the same 7 px on a 520 px thumbnail spans
     several folds — which is why a fixture can pass while the real frame fails.
  2. **The content guard** (`guard_crop_edges`) then pushes every edge outward
     until what lies *beyond* it is genuinely bed, or the scan boundary is
     reached (a print scanned to its edge simply has nothing to crop there —
     that is `runs_off_scan`, not a failure). The strip being judged starts one
     depth beyond the edge: flush against it, a few antialiased print pixels
     raise the variance and every edge creeps outward on a perfectly good scan.
     An edge dragged further than `CLEANUP_EDGE_MAX_MOVE_FRAC` (5 % of the
     short side) is not trusted at all and that side is left uncropped —
     "crop skipped on left: print edge unclear"; two or more such sides means
     `needs_manual: print_edge_unclear`. A safety margin
     (`CLEANUP_CROP_SAFETY_PX_AT_300`, 6 px at 300 DPI) is given back outward:
     bed slivers are harmless and obvious, missing pixels are neither.
  **Calmness is judged against *this scan's own* bed**, measured from its
  corners (`bed_noise`), never a fixed number: genuine bed runs from std 1.2 on
  a clean scan to 10 on a noisy one, and #15's curtain sits at 10.5 in between.
  Within one scan the gap is decisive. Measured over batches 1–5, crop edges
  placed inside the picture went **30 of 455 → 12**, and edges misplaced at all
  **106 → 31**.
- **A multi-print scan is not a small print.** The split gates (per region
  ≥ `CLEANUP_SPLIT_MIN_FRAC` and rectangular) run *before* the whole-scan size
  gate and exempt it: on a scan of three prints the largest covers a third of
  the bed, which is the analyser understanding the scan, not failing to find a
  print. `print_frac_all` records the combined figure. The aspect and skew
  checks still apply, per region.
- **`needs_manual` stays in the queue** (`print_too_small`,
  `implausible_aspect`, `skew_too_large`, `has_back`, `no_print_found`): the
  geometric checkboxes are disabled with the reason in the tooltip, the tonal
  ones are still offered. `MANUAL_FIX_DIR` is written only on an explicit **R**.
- **Colour cast is measured on the print's near-neutral mid-tones, not all of
  them.** `ops.neutral_midtones` narrows the mid-tones (inside the rect inset
  by 8 %, since the edge carries paper border and bed bleed) to the
  least-colourful `CLEANUP_CAST_NEUTRAL_PCT` per cent — 40 by default; 100
  restores plain grey-world. **Grey-world over every mid-tone assumes the
  average scene is grey, which is exactly where it fails**: a lawn, a winter
  field or a warm indoor shot reads as a cast and gets pushed into the
  opposite one. An age cast shifts the paper itself, greys included, so it
  survives the narrowing while the scene does not. Measured on batches 1–5:
  grey-world fired on 270 of 444 scans (median magnitude 14.2) against 168
  (6.4) for this estimator, and 152 of those 270 were overstated more than
  twofold. `cast_gains` uses **the same pixel selection** as the measurement —
  gains from a wider set than the measurement would not be the correction the
  caption promised. Every proposal also records `grey_world_magnitude` so the
  gap stays visible. p95 chroma under `CLEANUP_MONO_CHROMA_MAX` → mono; high
  chroma with hue circular variance under `CLEANUP_SEPIA_HUE_VAR_MAX` →
  sepia. Both skip the op and say so in the caption. Gains are clamped to
  [0.74, 1.35] so a heavy cast cannot blow a channel — a strong cast is
  improved, not erased.
- **A cast must show on the paper, not just in the scene** (fix-up 1). The
  neutral-mid-tone estimator above was still fooled by prints whose *subject*
  is one colour — a church interior of warm wood, a lawn. So the cast is
  measured twice and three rules gate the correction:
  1. **Highlight agreement.** `ops.measure_highlight_cast` reads the print's
     near-white pixels (top `100 - CLEANUP_CAST_HIGHLIGHT_PCT` per cent of
     luminance inside the inset rect, **blown pixels excluded** — a clipped
     channel has lost its colour and would drag the reading toward neutral).
     An age cast stains the paper; a scene colour leaves it alone. The two
     readings must point the same way (Lab a/b vectors less than 90° apart)
     and the highlight magnitude must be at least
     `CLEANUP_CAST_HIGHLIGHT_AGREE` (0.5) of the mid-tone one, or the op is
     skipped as `scene_colour` with the reason in the caption. When they do
     agree, the correction uses the **smaller** of the two readings.
  2. **Partial strength.** Gains are blended toward 1.0 by
     `CLEANUP_CAST_STRENGTH` (0.7). Restorers under-correct on purpose: a
     print that keeps 30 % of its warmth still looks like an old photo, one
     pushed past neutral looks wrong instantly. 1.0 restores full correction.
  3. **White-point guard.** `ops.guard_white_point` applies the candidate
     gains to the highlight sample and, if the corrected paper white lands
     further from neutral than it started, scales them back until it does
     not. This is what stops a white shirt under a warm lamp coming out blue.
  Every proposal records both vectors, the ratio, `gains_raw`,
  `gains_blended`, the guard's verdict and `grey_world_magnitude`, so a
  decision can always be re-read rather than re-derived. Measured on batches
  1–5: the colour op went 270 (grey-world) → 168 (neutral mid-tones) → **50**,
  with 49 rejections for "highlights cast the other way" and 25 for "paper
  white is clean".
- **Per-op checkboxes really change the geometry.** `render.plan_from` is the
  single place ticks become pixels: unticking *deskew* crops to the print's
  axis-aligned bounds instead; unticking *crop* keeps the whole frame,
  straightened, bed-filled where the rotation pulled bed in.
- **A deskew never grows the picture** (fix-up 4). Rotating needs a canvas big
  enough to hold the corners, and keeping that expanded canvas made accepted
  files *larger* than the scans they came from — up to +47 % in area on a
  13.5° print; 109 live proposals would have rendered that way, and photo #50
  was accepted before the fix (3510×2357 → 3527×2382 on disk). A skipped crop
  now clips back to a source-sized window centred on the rotated canvas. The
  rule: **for anything that is not a split region, `out_w <= src_w` and
  `out_h <= src_h`**, enforced in `geometry.transform_for` so every caller
  gets it, and pinned over the whole angle/size/tick space by
  `test_cleanup_canvas.py`. Split children are the one exemption — they are
  cut from a region, so they are smaller by construction.
- **The review queue has a `Show:` filter** (fix-up 4): *All pending* /
  *Splits* / *Needs manual* / *Geometric-only*, each labelled with its size,
  persisted in QSettings under `cleanup/queue_filter`. George's verdict on the
  full queue was that the crops are minimal by design and reviewing 981 of
  them is not worth it, so the queue has to be able to show just the 17
  splits. `repo.QUEUE_FILTERS` is the list and `_QUEUE_FILTER_SQL` the
  clauses; *Geometric-only* is `Proposal.is_geometric_only` written in SQL and
  a test asserts the two agree, because two definitions of one rule is how the
  bulk-accept button and the filter drift apart.
- **Bulk accept is geometric-only**: deskew and/or crop, no tonal op, no
  split, nothing `needs_manual`. Tonal ops and splits always go through the
  eye.

### Split (multi-print scans)
- **A split child's `photos.sha256` is an identity key, not a file hash:**
  `sha256(master_sha256 + ':' + region_key)`. Its `photo_masters` row points
  at the **same master file** as its siblings with `region` (jsonb, frame
  `display`) and `region_key` (`'x,y,w,h'`); the old global uniques on
  `master_path` and `sha256` are now per `(…, region_key)`. The parent keeps
  the real `source_filename` so re-ingesting the master stays a no-op;
  children take `<parent filename>#pN`.
- **Faces are assigned by containment**: the region holding ≥ 50 % of the box
  wins, unless a second region also holds ≥ 20 % — then it straddles the cut
  and is soft-deleted (invariant 2). Album memberships **and live
  `photo_groups` rows** are copied to every child: group membership is what
  makes a photo visible on the web, so dropping it would quietly hide the
  print from the family the parent was shared with.
- **`detect_faces` is marked done** on a child that inherited faces, so the
  next jobs run doesn't detect them a second time. `classify` / `describe` /
  `estimate_date` run on children normally. Existing suggestions and
  `photo_job_status` on an ordinary (non-split) accept are left alone.
- **The parent leaves through the front door**:
  `triage.apply_decision('junk', hint='split_parent')`, so it is quarantined,
  audited and restorable.
- **Prints that touch arrive as one component, and are cut apart** (fix-up 5).
  Photo #708 is three prints stacked on one bed; the lower two touch, so one
  connected component covered both and the analyser proposed a two-way split.
  No gate was wrong — the prints were never separated. `split_merged_components`
  looks for bands of scanner bed running right across a component and cuts on
  them. The evidence is `bed_by_tone` (bed tone + **reachable from the scan
  border**), not the absence of the print mask: on a scan whose prints sit
  close together the mask has already bridged the gutter, which is why they
  merged, and on one whose prints have pale backgrounds (#1398) the mask is
  full of holes that are not gutters. Its flatness window
  (`CLEANUP_GUTTER_STD_WINDOW_FRAC`, 0.003) is a fifth of the mask's, because
  it has to fit *inside* a gutter — at 1.3 % the window is wider than the gap
  and every pixel in it reads as busy. A cut is kept only if it yields two or
  more pieces that each look like a print.
- **A region counts if it is big in absolute terms OR relative to the largest**
  (`CLEANUP_SPLIT_REL_MIN`, 0.55). Eight prints on one bed are ~10 % of the
  scan each and every one failed the 12 % gate (#1398); what makes them prints
  is that they are the same size as each other.
- **A `document` is never split.** Photo #3839 is a newspaper cutting whose
  columns of text, separated by white gutters, are exactly what a multi-print
  scan looks like. No image heuristic is needed — the classify job already
  labelled it `document` at 0.98 — so `CLEANUP_SPLIT_SKIP_LABELS`
  (`document,screenshot,back_of_print`) vetoes the split and the crop is
  offered instead. The label comes down the scope query as `ScopeRow.ai_label`
  (newest non-rejected `classification` suggestion).
- **What the detector still cannot do, the region editor can.** #3817 is a
  ten-picture proof sheet whose prints touch with *no* bed between them: there
  is no gutter to find, and measuring says so. **G** opens
  `region_editor.RegionEditorDialog` — drag to move, corners to resize, drag
  on empty bed to add, `Del` to remove, plus a rows × columns **grid** helper
  laid over the area the regions already cover. Every rule lives in
  `regions.py`, which is free of Qt and of the database: regions may not
  overlap (beyond a 2 % touching tolerance — adjacent prints share an edge),
  may not leave the scan, and must be more than 0.5 % of it. Saving rewrites
  `cleanup_proposals.split_regions` with `edited_by='human'` per region and
  writes a `cleanup.split` audit row carrying **both** the measured and the
  drawn regions. The transform is built by `transform_for` with the same
  arguments the analyser uses, so `accept_split` cannot tell an edited region
  from a measured one — faces still map by containment, children still get
  their own `region_key`.
- **W — keep whole.** R was the only way to refuse a proposal, and R copies
  the file to `MANUAL_FIX_DIR` and parks the photo in a queue, which is the
  wrong answer when nothing is wrong with the photo. **W** marks the proposal
  `rejected` with `reason='not_a_split'`, writes the audit row, and stops: no
  file is written, no pixels change, `photos` is untouched. It works on any
  proposal, not just a split.
- **A scan with a `photo_backs` row is never auto-split** — which child owns
  the back is not the analyser's guess to make; it becomes `needs_manual`
  with "has a back — split by hand".
- **Splits are never bulk-accepted**, and `accept_proposal` refuses one
  (`split.accept_split` is the only path).

### Ordering, undo, remote
- **File moves happen inside the transaction, just before commit** — the
  opposite of Triage's rule, deliberately. A failed triage move leaves a
  recoverable mismatch; a committed `file_version` bump whose bytes never
  arrived is unrecoverable. `_swap_in_new_version` moves the old copy aside,
  then the render into place, and puts the old one back if the second move
  fails.
- **`check_working_files` is cleanup-aware.** A missing working file is now
  rebuilt from the newest `_versions/` copy before the master is considered —
  the master is the raw scan and would undo every cleanup the photo has had.
  When only the master is left on a photo with `file_version > 1` the copy
  still happens (a photo beats no photo) but it is audited as
  `…recopied_from_master_cleanup_lost` and counted in `cleanup_version_lost`,
  whose ids the summary tells George to re-clean and re-check the boxes on.
  Never a silent revert.
- **Undo (Z) is session-scoped.** It keeps the cleaned file (moved into
  `_versions/` under its own version), restores the previous one to the
  working name and bumps `file_version` **again** — history is never
  rewritten — and restores each box **verbatim from the `cleanup.accept`
  audit row** (exact; `Transform.invert()` exists and is tested, but the
  recorded box is better). Cross-restart undo is a manual job from
  `_versions/` plus the audit row: the recipe is in `GC.md`.
- **Remote enhance is pluggable and off by default.**
  `CLEANUP_REMOTE_PROVIDER=null|claid`; with no `CLAID_API_KEY` the **E** key
  is disabled with a tooltip. The returned image becomes a **new pending
  proposal** (`operations.ops.remote_enhance`) through the same review, never
  auto-accepted; if the provider resized, the boxes go through a scale
  transform. Spend is recorded in `cleanup_spend` before the job completes;
  the status bar shows the session total, the header the cumulative one. The
  Claid client is only ever exercised against a mock HTTP server.
- **Two UI rules the GUI tests enforce.** (a) *Never drop the last Python
  reference to a running `BackgroundJob`* — Qt aborts with "QThread: Destroyed
  while thread is still running". Cancelling a preview render and starting the
  next one parks the old job in `self._live_jobs` until its own `finished`
  signal fires, and `closeEvent` cancels and joins every live job. (b) *The
  status bar has two halves*: the left is the last **decision** and persists
  until the next one; the right is context (which photo, what was measured,
  what a job is doing). A preview landing must never wipe out what George just
  did — the Phase 3 "no flash messages" lesson, restated.
- **Audit namespace:** `cleanup.propose`, `cleanup.accept`, `cleanup.reject`,
  `cleanup.split`, `cleanup.remote`, `cleanup.undo`, plus
  `cleanup_analyse.start` on the `job_run`.
- **Tombstones (Phase 7 answer 3).** Junk, private and soft-deleted photos
  are excluded from the push selector, so a photo the web already holds that
  later goes away — a dedupe loser, a triage-to-junk, a split parent — would
  stay visible on the VM forever. `push()` now sends each such photo **once**
  with `is_deleted=true` and stamps `photos.tombstoned_at`. The marker has to
  be its own column: `set_updated_at` fires on the `synced_at` write, so
  `synced_at > updated_at` can never hold. The web never asks for a
  tombstone's file bytes.

## Inference client / jobs (Phase 6 onwards)
- **`INFERENCE_URL` / `INFERENCE_TOKEN`** in `desktop/.env`. The single-image
  endpoints, `/health`, and the unattended-batch surface
  (`/batch/upload/{job_name}`, `POST /batch/{endpoint}` with `from_inbox`,
  `GET /batch/results/{job_name}?after=<cursor>`, `/summary`, sweep,
  cancel) all live behind `inference_client.LanInferenceClient`.
- **Client-side downscale + JPEG Q85** before every upload. Per-endpoint
  edge: `classify`/`describe`/`estimate-date` at 1024, `transcribe-back` and
  `detect-faces` at 1536. The multipart filename stem is the `ref` (photo id,
  or `b<photo_backs.id>` for backs). 401 is fatal; connection errors and 503
  retry with exponential backoff.
- **Hand over, then collect.** The batch runner (`jobs/`) selects eligible
  items, uploads in chunks of 50 with progress, then POSTs
  `/batch/{endpoint}` with `from_inbox` — laptop can be closed after that.
  Collect polls every 5 minutes (and on app start): reads NDJSON from the
  mini's per-job results file after the stored cursor, applies the writer
  once per line (idempotent), advances `job_cursors.line_no`, updates
  `photo_job_status`, then sweeps `?done=true`.
- **Selectors gate on `photo_job_status`, not on presence of downstream
  rows.** A legitimately zero-face photo has no `faces` rows — but its
  detect_faces status is 'done', so it isn't re-processed.
- **Blackout lives on the mini** (Phase 5 follow-up). Laptop has no
  blackout logic; the Jobs panel just reads `/health.blackout`.
- **Queue order:** `transcribe_backs → detect_faces → classify → describe →
  estimate_date`. Hand-overs run sequentially in that order.
- **Everything the models produce is a suggestion.** Writers insert
  `suggestions` rows with `source='ai'`, `model`, and `prompt_version`.
  Fact columns (`photos.capture_date`, `photos.description_ai`,
  `faces.person_id`, `photo_places`) are only written by an admin
  promoting a suggestion — or by George's own decisions in the Faces mode
  (which count as admin actions and get audit rows tagged `source='human'`).
  The two facts the writers set directly are observational and low-risk:
  `photo_backs.transcribed_text/transcription_confidence` (with
  `transcription_confirmed=false`) and `faces` rows themselves.
- **transcribe_backs low-confidence retry (< 0.5)** re-runs the flipped
  and rot180 variants **inline via single-image calls**, not through the
  batch queue. Keeps the writer's state machine simple; the retry rate is
  small.
- **classify → `back_of_print` on a scan** inserts a pending
  `ingest_pairings` row exactly like the B key: `back_photo_id` = this
  photo, `back_score` = confidence, `front_photo_id` = immediate
  predecessor by `scan_sequence` (unless the predecessor is itself a
  back / pending back → orphan), `details.source='ai_classify'`.
- **AI-junk hint precedence: presort wins.** If a photo already has a
  `triage_hints` row from the presort job, the AI's label goes into
  `details.also.ai_classify`; the hint stays as-is. Only photos with no
  presort hint get `hint='ai_junk'`.
- **Private photos are sent to the mini.** LAN-only, no external egress;
  the "private is private" rule is about the web VM. Sweep removes the
  inbox copy once the result is in the DB.
- **One working-file resolver** (fix-up 11).
  `modes.ingest.paths.resolve_working_path(WORKING_DIR, stored)` is the
  only place a stored `photos.working_path` / `photo_backs.working_path`
  becomes a filesystem path: absolute stays, bare joins `WORKING_DIR` by
  basename. Every uploader is `jobs.base.WorkingFileUploader`; the faces
  preview context, bbox edit, face-crop writer, transcribe retry, and
  push all go through it. No code builds a working path from the naming
  scheme on its own. The bare-name tolerance is a safety net: the DB
  holds absolute paths, and `check_working_files` (which now also covers
  `photo_backs` and prints a "still bare" post-condition that must be 0)
  rewrites anything bare with an audit row per repair.
- **A hand-over never aborts on one bad file.** `hand_over` reads and
  downscales each item itself; `FileNotFoundError` / undecodable image
  → `job_items` row `status='failed'` with the error, log, continue.
  `HandoverSummary.report()` is "N uploaded, M skipped (missing file)";
  `job_runs.params.stats.skipped_refs` keeps the ids and the Jobs panel's
  **Skipped files…** button lists them. Skipped items never reach
  `photo_job_status`, so the next Run re-selects them. If every item was
  skipped the mini is not started.

## Faces mode (Phase 6)
- **Clustering is in-memory**, scipy **average-linkage** cosine distance
  (fix-up 2 — single linkage chained ~3,500 faces into one blob),
  threshold `FACE_CLUSTER_DIST` (default 0.45). Recomputed on demand from
  the button. Never stored in the DB.
- **Quality gate before clustering AND before reference-set means.** Faces
  with `det_score < FACE_MIN_SCORE` (0.7) or bbox short-edge <
  `FACE_MIN_PX` (40) are excluded; rows kept, surfaced via the
  "Include low-quality" toggle. Low-quality faces poison reference means
  too, so the same gate applies there.
- **Recursive split** for any cluster larger than `FACE_MAX_CLUSTER`
  (300): re-cluster the members at threshold × 0.8, iteratively (cap 5
  levels). Sub-clusters are tagged "split from a larger cluster" in the
  header.
- **Queue order**: big first, but clusters with < 3 faces push to the
  back — the meaty ones get handled first; singletons are the long tail.
- **Cluster grid ordering**: within each cluster, faces sort by cosine
  distance from the cluster centroid (closest first). Outliers land at
  the tail so a Shift-range-select picks off the "other person" in a
  mixed-sibling cluster.
- **B key — Split by nearest person.** For a cluster that mixes two
  siblings, once both are labelled, `B` assigns every face to whichever
  of the two nearest labelled people (measured against the cluster
  centroid) it is closer to; preview + confirm before it commits.
- **Reference embeddings exclude `is_disputed=true` AND low-quality
  faces.** One wrong tag on a blurry crop must not quietly poison every
  future match.
- **Multi-prototype references** (fix-up 3). Each labelled person's
  reference set is up to 5 prototypes produced by scipy k-means over
  their non-disputed, quality-gated faces (fewer for tiny samples;
  single-mean fallback for < 3 faces). Suggested match is nearest
  prototype, not the mean, so a lifetime doesn't split across age
  bands. Under the primary suggestion the side pane also shows the
  next 2 candidates ("Also probably: …"). Unlabelled clusters within
  `FACE_CLUSTER_DIST` of any labelled person's nearest prototype are
  badged "likely <name>" in the cluster header.
- **Full-photo preview** (fix-up 5). Space or double-click on a face
  tile opens a right-hand pane with the whole photo, the current face
  outlined in yellow, every other detected face outlined and labelled
  with its person name where known, plus year / batch#sequence /
  folder / back transcription in the caption. Left/Right step through
  the cluster; Esc closes. Space-and-hold is a peek (release closes
  if held > 300 ms); Space-tap locks it open. Clicking another face in
  the preview jumps to that face's cluster (unlabelled) or opens the
  person editor (labelled).
- **Face coordinate frame** (fix-up 6). Every face bbox in `faces.bbox`
  is in the EXIF-transposed (display) orientation of the working copy
  at full resolution. `photos.width/height` are display dims;
  `photo_masters.width/height` are the raw file dims. Ingest reads
  EXIF via `image_io.probe_image()` and stores `photos.orientation`
  (1..8). Client-side downscale, face-crop generation, and the preview
  all `ImageOps.exif_transpose` before drawing so coordinates match.
  Pre-fix-up-6 rows are repaired by
  `python -m photoarchive.tools.repair_face_boxes`
  (backfills orientation, swaps dims for {5,6,7,8}, and rescales every
  stored bbox by (H_raw/W_raw, W_raw/H_raw)).
  `python -m photoarchive.tools.diagnose_face_box PHOTO_ID` prints
  everything relevant for one photo in one report.
- **`unknown` / `ignore`** (fix-up 8). `faces.review_status` (migration
  24) is one of `pending | unknown | ignore` and applies to unassigned
  faces (assigned faces stay `pending`; their identity comes from
  `person_id`). In the cluster view **U** marks the whole cluster (or
  the current selection) as `unknown` — a real person, not one George
  can name yet; those clusters drop to the "Unknown queue" at the back
  of the pass and stay in clustering so a later labelled person's
  prototype can match them. **I** marks as `ignore` — noise; excluded
  from clustering AND from reference sets entirely. **Z** undoes the
  last U/I (session-scoped stack). Every action writes a `face.review`
  audit row; undo writes `face.review.undo`. Sync (Phase 9) pushes
  `review_status`; the web will surface `unknown` faces as
  "Who is this?" prompts.
- **Manual bbox edit** (fix-up 9). In the preview: click a face box and
  drag its body to move, drag a corner to resize (`CORNER_HANDLE_PX=10`);
  drag on empty canvas to draw a new box; arrow keys nudge the current
  face by 2 px (Shift+arrows = 10 px); short click without dragging
  still routes to `face_clicked`. On release the writer updates
  `faces.bbox`, sets `source='human'`, writes `face.bbox_edit` audit,
  regenerates the face crop, and tries `/detect-faces` on the crop for
  a fresh embedding. If the service is down, `faces.embedding_stale`
  (migration 25) goes true; a future refresh path picks it up.
- **People sidebar** (fix-up 9). A dedicated sidebar mode with a
  searchable list (name, face count, birth/death years) and an editor
  covering every name field, birth/death year, notes, and add/remove
  name variants. Merge and "show all photos" buttons per person. The
  same editor is what the People… dialog in Faces uses.
- **Assign-existing-person dialog** (fix-up 9). Opens with an empty
  focused search field. Typing filters live — prefix on any name field
  first, then trigram on `display_name` and hand-curated variants. The
  AI-suggested person is pinned at the top; Enter accepts the highlighted
  row. Never pre-fill the field with the suggestion.
- **Full-photo preview back panel** (fix-up 10). When a photo has a
  `photo_backs` row, the preview grows a Back panel below the image:
  back thumbnail (click-to-enlarge via the `T` key which flips the main
  pane), verbatim transcription with `confidence`/`orientation_used`,
  chips for `parsed_dates` and `names` from the latest AI transcription
  suggestion, plus **Confirm transcription** and **Fix transcription…**
  buttons (audit `back.transcription_confirm` / `back.transcription_edit`).
- **Suggestions block** (fix-up 10). Read-only list of the photo's
  date / description / folder suggestions. Each date suggestion has an
  **Accept date** button that promotes it via
  `repo.promote_date_suggestion` (sets
  `photos.capture_date/precision/confirmed=true`, marks the suggestion
  accepted, writes `photo.capture_date.set`, refreshes completeness).
  When the photo already has a confirmed date, the promoter returns
  `conflict=True` so the UI can prompt (409-style) before overwriting.
- **✎ badge** (fix-up 10). Face tiles whose photo has any
  `photo_backs` row are prefixed with `✎` in the cluster grid so
  George knows there's writing on the back to read.
- **Working-file integrity** (fix-up 7). Some scans went through
  `_staging/` in Phase 2 and later got released via rebuilds /
  rejections, leaving `photos.working_path` stale. Every non-deleted
  photo's `working_path` should be `WORKING_DIR/{id:08d}_{sha[:8]}.{ext}`
  and the file must exist. `python -m photoarchive.tools.check_working_files`
  scans every photo; for anything missing it looks under the standard
  name (updates the pointer only), then under `_staging/{sha}.{ext}`
  (moves into place, bumps `file_version`), **then the newest
  `_versions/{id:08d}_v{n}.{ext}` (Phase 7)**, then copies from the
  preferred master as a last resort (masters are read-only — copy,
  never move). Audit row per repair; truly-missing photo ids are
  listed. `--dry-run` for a report. Run this in the Phase 9 push
  pre-flight and after any `_staging/` reshuffle. A cleaned photo
  (`file_version > 1`) rebuilt from the master has lost its cleanup and
  its face-box frame: that is counted as `cleanup_version_lost`, audited
  as `…recopied_from_master_cleanup_lost`, and the ids are printed —
  never a silent revert.
- **"Not a face"** soft-deletes the row (`is_deleted=true`, `deleted_at`,
  `delete_reason`) and writes an audit row. Every selector filters out
  deleted rows.
- **Face crops are precomputed** at collect time to
  `THUMBS_DIR/faces/{face_id}.jpg` (padded ~15% around the box, 256 px
  edge). The cluster grid renders straight from those files.
- **George's assignments in Faces mode are facts** — `faces.person_id`
  is set, `source='human'`, audit row `face.assign`. Accepting the AI's
  suggestion is the same. Contributor-side suggestions from the web
  arrive later in `suggestions` (Phase 9).
- **Merge two people:** faces and name variants move onto the winner
  (variants deduped by lower(variant)); loser is soft-deleted; audit rows
  written both directions (`person.merge`, `person.merged_into`).

## Web (CraftTags lessons — always apply)
- Token links land on a POST-confirm page. GET on the token changes nothing.
- `app.set('trust proxy', 1)` before any middleware that reads client IP.
- Register specific routes before wildcards.
- Watch fail2ban when smoke-testing from a new IP.

## Web auth (Phase 8 onwards)
- **No signup.** Strangers use `/request-access` → George approves via an
  emailed confirm-page link (or `/admin/access`) → the new user gets a
  first magic link → session cookie. Only `tools/create-admin.js` creates
  admins; there is no other path.
- **Expiries are computed in SQL.** Access-request tokens
  `token_expires_at = now() + interval '72 hours'`; magic-link
  `expires_at = now() + interval '15 minutes'`. Never a JS `Date`.
- **Magic-link tokens are stored hashed.** The raw 32-byte hex token
  goes only into the emailed URL; `magic_links.token_hash` stores
  `sha256(token)`. Lookups hash-then-compare.
- **loadUser gates suspension.** On every request, if the user's status
  is not `active`, `loadUser` destroys the session and treats them as
  anonymous. Suspending in `/admin/users` also deletes their `session`
  rows for tidiness.
- **CSRF policy:** per-session `_csrf` token on every authed POST form
  (`req.user` set). Pre-auth POSTs (`/request-access`, `/login`) rely on
  the honeypot + rate limiter. Token-URL POSTs (`/a/:token`,
  `/admin/access/:token/{approve,deny}`) rely on the unguessable token.
- **Rate limiter:** 5 / 15 min per IP on both `/request-access` and
  `/login`, with independent counters.
- **Never reveal whether an email exists.** `POST /login` for a
  suspended/unknown email returns the same "check your inbox" page and
  sends no email; only the internal log records the miss.
- **Audit namespace `auth.*`:** `auth.request_access`,
  `auth.request_access.duplicate`, `auth.approve`, `auth.deny`,
  `auth.login`, `auth.suspend`, `auth.reactivate`, `auth.role_change`,
  `auth.magic_link.sent`, `auth.magic_link.expired_attempt`. `actor` is
  the acting user's email (or `system` for automatic steps, `bootstrap`
  for `create-admin.js`).
- **Service token:** desktop → web sync uses
  `Authorization: Bearer <SERVICE_TOKEN>` with constant-time compare and
  no `users` row. `is_service` on `users` is unused for now.
- **Dev mail** goes to `web/tmp/mail/` when `POSTMARK_API_KEY` is unset —
  the sink is gitignored; sign-in links there are clickable.

## Web core, groups, sync, contributions (Phase 9 onwards)

- **Visibility is the load-bearing rule.** A photo is visible to a user
  when the user is an admin (sees every non-private, non-deleted photo
  including unfiled) OR they share at least one live group with it
  (`photo_groups + group_members`, both `is_deleted = false`).
  `is_private` and `is_deleted` always exclude. Photos with no live
  group are **admin-only** — that's how the 12 800 back-catalogue
  photos start until George bulk-assigns them. Every list, count,
  detail, image, face, back, comment, like, suggestion, and search
  goes through `middleware/visibility.js`. **A non-member gets 404,
  never 403** — don't confirm the photo exists.
- **Groups have soft-delete on the join tables.** `group_members` and
  `photo_groups` both carry `is_deleted / deleted_at / deleted_by /
  updated_at` and a `set_updated_at` trigger. A "remove from group" is
  `update ... set is_deleted = true` — never a hard delete — so the
  desktop↔web sync can do last-writer-wins by `updated_at`. Ties
  fall through and re-write; strictly-older incoming rows are
  rejected. **Group name uniqueness is per live row** (`create unique
  index groups_name_unique_live on groups (lower(name)) where not is_deleted`).
- **Moderator scope is per group.** `group_members.role = 'moderator'`
  (per group). `requireModerator({ groupIdParam })` gates the routes.
  A moderator may hide/unhide comments on photos visible in their
  group, remove a photo from their group (soft-delete the
  `photo_groups` row; photo unfiled iff no groups remain), add/remove
  **members** of their group (never role changes), and
  approve/reject contribution files that target their group.
  **Moderator approve assigns only their group.** Moderator reject
  removes only their group from the contribution's targets — the file
  becomes `rejected` only when no target group remains.
- **Facts vs. suggestions still holds.** Every contributor write
  produces a `suggestions` row with `source='human'`. Admin
  `POST /api/admin/suggestions/:id/accept` promotes it into a fact
  column and writes an audit row; **the accept path returns 409 with
  both current + proposed values when the target already has a
  confirmed fact**, and only `force: true` overrides (still audited).
  Accept also calls `refresh_completeness` on the touched photo.
- **CSRF has two carriers.** The per-session token comes back as
  `res.locals.csrfToken` (form `_csrf`) or `X-CSRF-Token` header
  (JSON clients). `GET /api/csrf` returns the current token for JS.
  Token-URL POSTs (`/a/:token`, `/admin/access/:token/*`) are exempt
  and rely on the unguessable token as their CSRF defence.
- **Contributor rate limit is per-user, in-process.** 300/hour
  combined across suggestions/comments/likes/tags/disputes;
  admins exempt. Uploads: 600/hour per user. Both are courtesy caps,
  not a security tool; replace with Redis if we ever run more than
  one Node process.
- **Contributions never make anything public.** Files land under
  `PHOTO_DIR/uploads/<contribution_id>/<file_id>.<ext>`, visible only
  to the uploader (`/api/contributions/mine`) and to admins/moderators
  of a target group. Admin approve-all assigns all target groups;
  moderator approve assigns only their own. **Rejected files stay on
  disk forever** with `status='rejected'` — never a delete.
- **`HEAD /api/contributions/:id/files?sha256=`** returns 204 if the
  server already holds that sha (photos, photo_masters, or another
  contribution_files row). The `/upload` page pre-hashes each file in
  the browser and skips whatever exists — that's how a partial upload
  resumes.
- **Duplicate policy on upload:** the file is uploaded either way, and
  the server records `duplicate_of_photo_id` + `duplicate_distance`
  when sha256 is exact OR pHash Hamming ≤ 10. Admin decides whether
  to approve (sha256-exact never creates a new photo; pHash-near goes
  through laptop-side dedupe / rescan). Never auto-reject.
- **Sync is service-token authed.** `Authorization: Bearer <SERVICE_TOKEN>`
  must match `desktop/.env` `WEB_API_TOKEN`. `/sync/photos` batch cap
  200, rejects with 400 on any `is_private=true` row, and returns
  `need_files` for photos whose `synced_file_version < file_version`
  or whose file is missing on disk. `PUT /sync/photos/:id/file`
  writes the working copy, regenerates a 320-px thumb via `sharp`,
  and records `synced_file_version` so the second push sends zero
  files. Every metadata batch route caps at 500 items. **Face
  embeddings are optional** — the desktop only sends them when
  `SYNC_FACE_EMBEDDINGS=true` in `desktop/.env`; the web accepts
  either shape.
- **`photo_groups` LWW.** Both directions push updates through
  `POST /sync/photo_groups`. The write compares `updated_at`: strictly
  older is rejected, equal or newer wins. Soft-delete rows sync with
  the rest.
- **`GET /sync/pull/confirmed` is minimal by design.** It returns
  accepted-suggestion payloads, every fact-set audit row
  (`photo.capture_date.set`, `face.assign`, `relationship.confirm`,
  …), and comment/like *summaries* — not comment bodies. Bodies
  stay web-authoritative; the desktop's metadata writer only needs
  to know a photo has comments to exclude it from XMP writes.
- **Contrib master root is append-only.** `MASTER_ROOTS` accepts a
  `|contrib` kind. Creation is permitted ONLY inside
  `<root>/_incoming/<contribution_id>/`. The masters guard skips
  `_incoming/` when probing subfolders. The pull writer snapshots
  the committed area, streams files into `_incoming/<cid>/`,
  verifies the snapshot didn't change, then renames to
  `<uploader>/<cid>/` and calls
  `POST /sync/pull/contributions/:id/pulled`.
- **Web `PHOTO_DIR` on the laptop must differ from desktop `WORKING_DIR`**
  — if the same path, a push copies files onto themselves.
- **Two facts writers set directly, no promotion needed** (repeated
  from Phase 6): `photo_backs.transcribed_text/…` and the `faces`
  rows themselves. Everything else on the web flows through the
  suggestion queue.
- **Audit namespaces added in Phase 9:** `contribution.*`,
  `contribution.file.*`, `photo_group.*`, `group.*`,
  `group_member.*`, `photo.rescan_wanted`, `sync.pull.*`.
- **The `photo-back-orphan` migration's `down` refuses to run when
  orphan `photo_backs` rows exist** — those are legitimate scanned
  backs of unidentified fronts and must never be silently deleted.
- **Push has a `files_only_for_grouped` flag (default True)**. Metadata
  for every non-private/non-junk photo pushes; file bytes go only for
  photos in at least one live `photo_groups` row. This is on while
  Phase 7 cleanup and inference jobs are still bumping `file_version`
  — pushing 28 GB of bytes that are about to change wastes hours.
  Pass `--all-files` to `run_push` (or uncheck the Sync-tab checkbox)
  when the archive is quiescent and we're doing the one-time full
  file push.

## Web pages (Phase 10 onwards)
- **Queries live in `web/services/`, never in routes or views.** Photos:
  `services/photos.js` (`listPhotos`, `getPhotoDetail`, `photoNeighbours`,
  `attentionCounts`); people, albums, faces ("Who is this?"), places,
  search, admin, contributions each have their own file. Visibility
  (`middleware/visibility.js`) and the group scope are applied there, so
  the JSON API and the server-rendered pages share one code path.
- **Group scope** (`services/scope.js`): `req.session.scope` = `all` |
  `unfiled` (admins) | `<group id>` (contributors: only their own groups),
  set by the header switcher (`POST /scope`, return_to must be a local
  path). Every photo grid and count applies it on top of visibility;
  people and album *lists* stay site-wide. API clients may pass
  `?scope=`. The view local is `groupScope` — never `scope`, which EJS
  treats as a reserved render option.
- **Keyset cursors are composite** `(sort_value, id)` for every sort
  (`services/cursor.js`, encoded `value~id`). Sorts: recent, liked,
  incomplete, oldest, newest (dates NULLS LAST), position (album order).
- **List keys** carry prev/next context on the photo page:
  `?from=b.<sort>|nd.<sort>|ut.<sort>|wi.<sort>|p.<personId>|a.<albumId>`;
  anything else falls back to Browse order. Never dump filters into URLs.
- **Layout**: every `res.render` is wrapped in `views/layout.ejs`
  (`middleware/layout.js`). Views describe themselves by mutating `page`
  (`page.title`, `page.nav`, `page.wide`, `page.css.push`, `page.js.push`)
  and pass `layout: false` to opt out. Shared partials: `photo-grid`,
  `tile`, `empty`, `icon`. `site.css` holds tokens + components (light
  and dark); page extras live in `photo.css`, `upload.css`, `admin.css`,
  `people.css`.
- **JS enhances, never required for reading.** Plain `<script defer>`
  modules in `public/js/`; `site.js` provides `CD.api` (JSON + CSRF
  header), `CD.csrf`, `CD.toast`; `autocomplete.js` is the one combobox
  (people, places). Writes go through the JSON API. Tap targets ≥ 44 px,
  inputs 16 px, no hover-only affordances.
- **Contributors never create people directly.** `POST /api/people` is
  admin-only; a new name travels inside a `person` suggestion
  (`new_person`) and the person is created at accept (web id ≥ floor).
  A person suggestion's `face_id` must belong to that photo.
- **Albums are read-only on the web** (no create / rename / add) until
  album changes can be pulled back to the desktop.
- **Normal browsing and uploading must never produce 4xx responses.** The
  VM's fail2ban `caddy-4xx-rate` jail (shared with George's other sites)
  bans an IP after 20 4xx in 10 minutes for an hour — it banned George on
  2026-09-17 when an admin Browse page asked for thumbnails of
  metadata-only photos. So: a visible photo whose file isn't on the server
  gets a 200 "No image yet" SVG from every `/media` route (header
  `X-Media-Placeholder: 1`; not visible stays 404); lists carry `has_file`
  and views don't request missing images; the upload sha pre-check answers
  204 (held) / 200 (not held), never 404. Check the caddy access log for
  4xx whenever a new page or script ships.
- **Media derivatives are cut on demand and cached under `PHOTO_DIR`**,
  behind the same visibility gate as every `/media` route:
  `/media/display/:id` (≤ 1600 px, `display/<id>_v<synced_file_version>.jpg`),
  `/media/faces/:id` (the pushed crop if present, else cut from the working
  copy after `sharp().rotate()` so the bbox's display frame matches —
  `faces/gen_<id>_v<version>_<bbox hash>.jpg`), `/media/contrib/:file_id`
  (uploader, admin, moderator of a target group). Back images are pushed
  as JPEG to `backs/back_<id:08d>_<sha8>.jpg`; `/sync/photo_backs` answers
  `need_files` (file missing on disk), so a second push sends zero backs.
- **Moderators**: `/admin`, `/admin/contributions`, `/admin/groups[/:id]`
  for their own groups only (404 for other groups); every other admin page
  is admin-only (403). Moderators never accept suggestions. The groups
  strip on the photo page shows "Remove from my group" for groups they
  moderate. `/api/admin/contributions` is reachable by moderators (the
  `/api/admin` router passes it through).
- **Tests run in parallel-safe schemas**: `TEST_DB_SCHEMA=<name> npm run
  test:setup` migrates that schema in `photoorg_test` and the tests' pool
  uses `search_path=<name>,public`. `test/page-helpers.js` `seedWorld()`
  is the shared fixture. Tests always use a temp `PHOTO_DIR`.

## Search (Phase 11 onwards)
- **The index is pure SQL.** `photo_search` and `person_search` are
  maintained by statement-level triggers with transition tables
  (`shared/migrations/*phase-11-search*`), so the desktop and the VM stay
  correct without any Python or Node knowing they exist. Never write to
  them from app code; call `refresh_photo_search(id)` /
  `refresh_person_search(id)`, or `rebuild_search()` for everything.
  Phase 1's `photos.search_tsv` / `people.search_key` are **gone**.
- **An update trigger may not carry a column list when it uses transition
  tables** (Postgres refuses). Each update function therefore joins `ot`
  and `nt` and refreshes only the rows whose searchable text actually
  changed — that is what keeps a sync push (file_version) or a jobs run
  (embeddings) from rewriting thousands of rows.
- **Bulk writers may defer**: `set local photoarchive.search_defer = on`
  queues ids in `photo_search_dirty` / `person_search_dirty`; `select
  sweep_search()` drains them. Off by default (measured: ~4.8 s of trigger
  work per 10 000 rows). If you turn it on, sweeping is not optional.
- **One query parser** (`web/services/search-parse.js`, pure) and one
  search service (`web/services/search.js`). Terms are AND-ed; the layers
  inside a term (person, place, date, free text) are OR-ed; the score is
  the sum of the best layer per term, and every hit carries `why` — the
  sentence the card shows. Dates reuse `services/date-parse.js`.
- **A phonetic hit must look alike as well as sound alike**: same
  `dmetaphone`, plus `similarity >= 0.3` **or** a shared first four letters
  on names of 5+ letters (Katherine/Kathryn score 0.29 on trigrams).
  Schmitt finds Schmidt; Smith never does. Phonetic hits rank below exact,
  variant and nickname hits, always.
- **A pending suggestion is searchable but never outranks a fact.** The
  newest pending `description` is in the vector at weight C (George will
  never hand-accept 12k descriptions) and a pending `date` suggestion is
  labelled "estimated"; a suggested person or place scores 30 below the
  tagged fact.
- **Search obeys the same visibility and scope SQL as every other list.**
  A non-member never sees a hit, a count, or an autocomplete suggestion
  for a photo they can't open — including the header box, which only
  offers people and places with at least one photo the user can open.
- **Empty search, no hits, unknown filter value: all 200.** Unparseable
  filter values are ignored, never a 4xx (the fail2ban rule above). Slow
  queries (> 300 ms) log `[search] slow …` with the parsed form.
- **`place_aliases` has no sync route yet** — like album edits, pulling it
  back to the desktop is an open item for Phase 12.

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

## Command-line entry points (desktop)
- `python -m photoarchive.tools.run_ingest` — ingest a master root.
- `python -m photoarchive.tools.run_presort` — triage hints.
- `python -m photoarchive.tools.run_cleanup [--batch "Batch 00001"] [--reanalyse]
  [--limit N] [--no-previews] [--report]` — Phase 7 `cleanup_analyse`.
  Masters-guard first; resumable; `--report` writes the run report.
- `python -m photoarchive.tools.cleanup_report [--batch …] [--samples N]` —
  the report on its own (counts, timings, op frequency, before/after sheet).
- `python -m photoarchive.tools.check_working_files [--dry-run]` — working-file
  integrity (now `_versions/`-aware; see Cleanup).
- `python -m photoarchive.tools.repair_face_boxes`,
  `python -m photoarchive.tools.diagnose_face_box PHOTO_ID` — face geometry.
