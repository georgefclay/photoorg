# Phase 7 — Scan cleanup (desktop)

Read `CLAUDE.md` (Desktop, Triage, Dedupe, Faces — especially the face coordinate frame, the one working-file resolver, and the "worker threads never touch widgets" lesson from Phase 3), `PROJECT-PLAN.md` (§2 decisions on masters / rescans / growing archive; Phase 7), `shared/SCHEMA.md` (photos, photo_masters, faces, photo_backs, audit_log, job_runs), and `photo-archive-build-prompts.md` §5. Work in `desktop/` and `shared/migrations/`. Start with `git pull`. The mini is still running the VLM jobs; this phase does not use the inference service except where noted.

GOAL: batch-clean the ~5,000 scanned prints (deskew, crop to the print, split multi-print scans, correct colour cast / levels / fade) with a review step, without ever touching a master, losing a previous working copy, or breaking the thousands of face boxes George has already labelled.

## Scope
- Photos with `source_root` of kind `scan`, `triage_status in ('keep','private')`, `is_deleted=false`. Backs (`photo_backs`) are out of scope this phase. Digital photos are never touched.
- Everything is a **proposal** until George accepts it. Nothing changes the working copy without an Accept.

## Invariants (add to CLAUDE.md)
1. **Cleanup writes a new derived file; it never overwrites.** Proposals render to `CLEANUP_DIR/{photo_id:08d}_v{file_version+1}.{ext}` (new `desktop/.env` var, default `D:\PhotoArchive\cleanup`). On Accept: the current working file moves to `WORKING_DIR/_versions/{photo_id:08d}_v{file_version}.{ext}` (kept forever), the derived file moves to the standard working name, `file_version` bumps (sync re-pushes), `photos.width/height` update, thumbnail regenerates, pHash/dHash rows are marked stale for the next dedupe scan. `photos.sha256` stays the master's — it is the photo's identity, not the working copy's.
2. **Face boxes travel with the pixels.** Every geometric op is expressed as one affine transform (rotation about the centre + crop offset + optional split-region offset), and Accept applies that same transform to every `faces.bbox` on the photo, regenerates the face crops, and keeps embeddings (same face, same person). Tonal ops leave boxes alone. A box that ends up outside the new frame (or straddles two split regions) is soft-deleted with `delete_reason='cleanup_out_of_frame'` and listed in the report — never silently dropped.
3. **Format follows the source.** JPEG in → JPEG q95 out; TIFF in → TIFF (LZW) out. No format changes.
4. **Masters untouched.** The masters guard probe runs at the start of every cleanup batch, same as ingest.

## Analysis (OpenCV + numpy; analyse at ≤ 2000 px, apply at full resolution; one image in memory at a time — the laptop is memory-tight)
- **Print detection:** find the print rectangle against the scanner bed (bed is white or black; try both). `minAreaRect` on the largest non-bed component. If the print covers < 40% of the scan, or the rectangle's aspect is implausible, mark `needs_manual` instead of guessing.
- **Deskew:** rotate by the rectangle angle. Skip if |angle| < 0.3°. Angle recorded.
- **Crop:** to the rectangle with a 4 px inset. Skip if it removes < 1% of the area.
- **Multi-print split:** ≥ 2 non-bed components each ≥ 12% of the scan and roughly rectangular → a split proposal with one region per print. Each region becomes its **own photos row** on Accept: new `photo_masters` row pointing at the same master file with a `region` (add the column), `parent_photo_id` on the child (add the column), same `scan_batch`/`scan_sequence`, `physical_ref_note` "print N of M on this scan", album memberships copied, faces mapped by containment (invariant 2). The parent goes through the normal triage transition to `junk` with hint `split_parent` (quarantined, restorable). The children are eligible for `detect_faces` / `classify` etc. on the next jobs run.
- **Colour cast:** estimate the cast from the print's mid-tones (not the bed); correct only when the cast exceeds a threshold. **Skip B&W and sepia prints** — low chroma is a deliberate look, not a cast. Record the estimated shift.
- **Levels / fade:** percentile stretch (0.5 / 99.5) with a mild S-curve only when the measured contrast is low. Record before/after contrast.
- Each op records what it measured and what it did in `operations` (jsonb). Ops that measure nothing are omitted, and a photo where nothing would change gets `status='clean'` with no proposal — it never reaches the review queue.

## Storage (migration)
`cleanup_proposals(id, photo_id, status pending|accepted|rejected|manual|clean|superseded, operations jsonb, transform jsonb, derived_path, needs_manual bool, split_regions jsonb null, analysed_at, decided_at, decided_by)`; one live pending proposal per photo. `photo_masters.region jsonb null`, `photos.parent_photo_id bigint null`. Audit rows: `cleanup.propose`, `cleanup.accept`, `cleanup.reject`, `cleanup.split`, `cleanup.remote`, `cleanup.undo`.

## Cleanup mode (sidebar)
- **Run analysis** button: batch job `cleanup_analyse` over the scope (resumable, progress, per-photo timing, skips photos with a decided proposal unless "Re-analyse"). Worker thread + mediator with `QueuedConnection` — no widget access from the worker (Phase 3 lesson).
- **Review queue:** proposals in `scan_batch`/`scan_sequence` order. Before and after **side by side at full resolution** with synced pan/zoom; `T` toggles A/B in one pane. Per-op checkboxes (uncheck "colour" and the preview re-renders without it; Accept applies only the ticked ops). Caption shows the measured numbers ("skew 2.1°, crop 6%, cast R+9 B−7").
- Keys: **A** accept, **R** reject → copies the current working file to `MANUAL_FIX_DIR/{photo_id:08d}_{sha[:8]}.{ext}` and sets `status='manual'` (a separate queue view lists them), **S** skip, **E** send to remote enhance, **Z** undo the last accept (restores from `_versions/`, reverts boxes via the inverse transform, bumps `file_version` again — never rewrites history). Filmstrip; counts; persistent status bar (no flash messages).
- **Bulk accept** for the boring majority: "Accept all *geometric-only* proposals in this batch" and "…in the whole queue", each with a confirm dialog showing the count. Tonal ops always go through the eye.

## Remote enhance (pluggable)
`cleanup/remote/` with a provider interface (`submit(image) → job`, `await(job) → image`, `cost_estimate`). `CLEANUP_REMOTE_PROVIDER=null|claid` in `desktop/.env`; `CLAID_API_KEY` from `GC.md` when George gets one — with no key the E key is disabled with a tooltip. The returned image becomes a **new proposal** (`operations=[remote_enhance]`) through the same review; never auto-accepted. Session spend counter in the status bar; cumulative spend persisted in a small `cleanup_spend` table. Test the Claid client against a mock HTTP server only.

## Tests
Synthetic scans: a rectangle rotated 3° on a white bed → angle within 0.3°, crop within 3 px; same on a black bed; two prints on one bed → two regions; orange-cast image → neutral within a delta; B&W image → no colour op; low-contrast image → levels op, normal image → none; face bbox transform round-trip (rotate+crop, then inverse = identity within 1 px); split maps a face into the right child; Accept keeps the previous version file and bumps `file_version`; Reject writes to `MANUAL_FIX_DIR`; undo restores file + boxes; masters probe refuses when a root is writable; "clean" photos never enter the queue.

## Verification, then stop
1. `pytest` green.
2. Analyse **Batch 00001–00005 only** (~250 photos). Report: per-photo analysis time, counts by status (clean / pending / needs_manual / split), op frequency, and 10 sample before/after pairs as a contact sheet under `D:\PhotoArchive\cleanup\_report\`. George reviews those in the app before the full run.
3. After George's OK: full scope run; same report; total elapsed.
4. `python -m photoarchive.tools.check_working_files --dry-run` must be clean afterwards; run `diagnose_face_box` on two accepted photos with labelled faces and confirm the boxes still sit on the faces.
5. Update `CLAUDE.md` (Cleanup section with the four invariants), `shared/SCHEMA.md`, `PROJECT-PLAN.md`; commit and push: `Phase 7: scan cleanup`.

---

## Answers to Claude Code's questions

1. **OK as proposed.** Child `sha256 = sha256(master_sha256 + ":" + region_key)`, `source_filename = "<parent>#pN"`, `photo_masters.region` + `region_key` default `'-'`, uniques become `(master_path, region_key)` and `(sha256, region_key)`. Document in SCHEMA.md that a child's `sha256` is an identity key, not a file hash, and that the parent keeps the real filename so re-ingest is a no-op.
2. **Agreed — force `needs_manual`**, never auto-split anything with a `photo_backs` row or pairing involvement. Say why in the caption ("has a back — split by hand").
3. **(b).** Push sends junk-but-previously-synced photos once as `is_deleted` tombstones; the web hides them everywhere (visibility already excludes `is_deleted`) and keeps the row. Cover dedupe losers and triage-junk too, and report how many tombstones the first push sends. Test both sides.
4. **Your way — plan + ~2000 px preview; full-res on demand and at Accept.** 8–10 GB of eager derivatives is not worth it.
5. **Viewport-sized levels + true 1:1 crop from disk at full zoom.** No separate loupe.
6. Include the 19 `is_scan` photos (scope by `is_scan`). **Exclude back-shaped photos** the way Dedupe does — this phase; backs get their own pass later (add to §5 open items). The 808 fronts with backs are in scope, confirmed.
7. **Former.** `needs_manual` enters the queue with geometric ops greyed out; tonal ops still offered; `MANUAL_FIX_DIR` only on an explicit **R**.
8. **Exclude splits** from bulk accept. Always through the eye.
9. **Session-only**, split undoable within the session. Cross-restart undo stays a manual job from `_versions/` + the audit row; document the recipe in `GC.md`.
10. **Agreed.** Leave existing suggestions and `photo_job_status` alone. Split children: `detect_faces` marked done when the parent's was done and faces were mapped; the VLM jobs run on them normally.
11. **Recompute pHash/dHash at Accept** from the regenerated thumbnail. Resolved groups and exclusions untouched.
12. **All tunables in `desktop/.env`** with the defaults you listed, documented in `desktop/.env.example`. **Inset scales with DPI**: 4 px at 300 DPI as the base (so 16 px at 1200), fallback 4 px when unknown.
13. **Fine.** Mid-tone Lab chroma inside the print; 95th-percentile chroma < `CLEANUP_MONO_CHROMA_MAX` → mono; high chroma + low hue variance → sepia; both skip the colour op and say so in the caption.
14. **Confirmed**, table shape as proposed. Nothing in this phase touches the network or spends money.

GO.

### PM answers to the batch 1–5 report

1. **Colour estimator: take your recommendation.** Switch to neutral-pixels-only with `CLEANUP_CAST_NEUTRAL_PCT=40` as the default, re-analyse batches 1–5 (`--reanalyse`, pending/clean rows superseded), and regenerate the contact sheet so George compares before/after on #27 and the rest. Answer 13 described a measurement, not a mandate — over-correction is exactly what the review was for.
2. **Commit now**, before the full run: `Phase 7: scan cleanup`. The full-scope run is data, not code; it doesn't need to gate the commit. Push.
3. **Full-scope run waits for George's OK** on the re-analysed contact sheet. When he gives it: close Firefox, run `run_cleanup --report` for the remaining scope, and report counts/timing as per step 3. Nothing is accepted by that run — it only proposes.
4. Leave photos 169 and 120 accepted; they're the step-4 evidence and reversible.

## Phase 7 fix-up 1 — colour cast still over-corrects on some prints

George's verdict on the re-analysed batch 1–5 sheet: #27 slightly better, #166 a big improvement, #1/#2 barely different, **#8 worse**, **#19 a white shirt turned blue** (the correction removed a warm cast that wasn't there, or removed more than there was). The estimator is better but still not safe enough to trust unseen. Two changes, both principled rather than threshold-fiddling:

1. **Highlights must agree with mid-tones.** An age cast shifts the whole print — paper white included. A scene colour (lawn, warm lamp, a beige wall) shifts mid-tones only. So measure the cast twice: on the neutral mid-tones (as now) and on the print's near-white highlights (top ~3% luminance inside the inset rect, excluding blown pixels). Correct only when both agree in direction and the highlight cast is at least `CLEANUP_CAST_HIGHLIGHT_AGREE` (default 0.5) of the mid-tone cast; the applied correction is the **smaller** of the two. If they disagree, no colour op, caption "scene colour, not a cast". Record both measurements on the proposal.
2. **Partial correction by default.** `CLEANUP_CAST_STRENGTH` (default 0.7): gains are blended toward 1.0 by that factor. Restorers under-correct on purpose; a print that keeps 30% of its warmth looks like an old photo, one that's pushed past neutral looks wrong instantly. 1.0 restores today's behaviour.
3. **White-point guard.** After computing gains, check the highlight sample: if the corrected highlights land further from neutral than they started, scale the gains back until they don't (this is what would have saved #19's shirt).

Diagnose **#8 and #19** first and paste: mid-tone cast vector, highlight cast vector, gains before/after each rule. Then re-analyse batches 1–5, regenerate the sheet, and report how many colour proposals survive (was ~168). Tests: warm-lamp scene on neutral paper → no op; uniformly yellowed print → op with highlights and mid-tones agreeing; #19-shaped case (warm mid-tones, neutral whites) → no op; strength 0.7 blends gains as specified.

Commit: `Phase 7 fix-up 1: colour cast needs highlight agreement, partial strength, white-point guard`.

## Phase 7 fix-up 2 — colour off by default; #306 deskewed the wrong way

George's verdict on the fix-up 1 sheet: #8 fine (no change), #25 added red to a print that already had an orange tint, #306's colour change is nice but **the deskew tilted the image to the left**. His conclusion, which I share: the colour op's wins don't pay for its losses. Two changes:

1. **Tonal ops are opt-in, off by default.** `CLEANUP_COLOUR_ENABLED=false` and `CLEANUP_LEVELS_ENABLED=false` in `.env` / `.env.example` / `config.py`. The analyser still measures and records both (the numbers are useful and cheap) but proposes neither unless enabled, so the queue becomes geometry only: deskew, crop, split. Keep all the fix-up 1 code and tests; this is a switch, not a revert. Mark existing pending proposals' tonal ops as unticked rather than re-analysing.
2. **#306 deskew direction.** Diagnose before touching code: the rectangle `minAreaRect` returned (centre, size, angle), the angle after your normalisation, the bed colour chosen, and the four print edges it found — overlay them on the preview and put the image in the report folder. Likely causes: OpenCV's `minAreaRect` angle convention (the [−90, 0) range flips meaning when width/height swap), a print with a white border where the detector locked onto the inner picture edge instead of the print edge, or the print's edge being genuinely tilted relative to its content. Fix the class. Then **sanity-check every deskew proposal in batches 1–5**: for each, measure the residual angle of the print edges *after* the proposed rotation; any with a residual larger than the original is a wrong-direction case — list them. Add a test with a real-scan fixture (a small crop of #306 is fine) rather than only synthetic rectangles.

Re-run batches 1–5, regenerate the sheet (geometry only now), report the wrong-direction count before and after. Commit: `Phase 7 fix-up 2: tonal ops opt-in, deskew direction`.

## Phase 7 fix-up 3 — #15 crop cut into the picture

On the fix-up 2 sheet, **#15's crop removed the left side of a person**; the result looks off-centre. A crop that removes picture content is worse than no crop — the whole point is to remove scanner bed, never pixels of the photograph.

1. Diagnose #15 first with an overlay in the report folder: bed colour chosen, the print mask, the rectangle, and the four crop edges drawn on the preview. Likely cause: a light-edged print (white border, pale sky, or a faded edge) on a white bed, so the mask found the *inner* darker content instead of the print edge, or a person standing at the edge of the frame whose light clothing merged with the bed.
2. **Content guard on every crop edge.** Before proposing a crop, examine the strip just *inside* each proposed edge (say 2% of the print's short side). A genuine print edge has bed or plain border on the outside and either border or picture on the inside — but the strip must not be *both* high-detail and continuous with what's beyond it. Concretely: if the strip's edge energy / local variance is comparable to the picture interior **and** the region just outside the edge is not clearly bed (colour within the bed tolerance, low variance), push that edge outward until the condition holds or the scan boundary is reached. If any edge cannot be resolved, skip the crop on that side entirely (crop the other three) and say "crop skipped on left: print edge unclear" in the caption; if two or more sides are unclear, `needs_manual` with `print_edge_unclear`.
3. **Prefer under-cropping.** When the print edge is found but the confidence is middling, add a margin (`CLEANUP_CROP_SAFETY_PX_AT_300`, default 6) outward. Bed slivers are harmless and easy to see; missing pixels are not.
4. **Batch check:** for every crop in batches 1–5, compute the content-guard score per edge and list the proposals with any edge failing it — those are the #15-shaped cases. Report the count before and after. Add a real-scan test from a crop of #15 alongside the synthetic ones.

Re-run batches 1–5, regenerate the sheet, report. Commit: `Phase 7 fix-up 3: crop never removes picture content`.

## Phase 7 fix-up 4 — queue filter; deskew must not grow the canvas

George's verdict on the full queue: the crops are minimal (the fix-up 3 safety rules leave visible bed by design) and reviewing 981 of them isn't worth it. He'll review **only the 17 splits**. Two small things:

1. **Queue filter.** Add a `Show:` combo next to the batch box: *All pending* / *Splits* / *Needs manual* / *Geometric-only*. It filters the review queue and the filmstrip; the header count reflects the filter. Default *All pending*. Persist the choice in QSettings.
2. **Bug: some proposals come out larger than the original.** A deskew is rotating the canvas and keeping the bed-filled corners instead of cropping to the largest axis-aligned rectangle inside the rotated print. Result dimensions must never exceed the source in either axis unless the op is a split region. Find the path (likely "crop unticked / crop skipped on a side → keep whole rotated canvas"), fix it so a skipped crop still clips to the source bounds, add a test asserting `out_w <= src_w and out_h <= src_h` for every non-split render, and report how many pending proposals were affected. No re-analysis needed — the render is computed at accept time.

Commit: `Phase 7 fix-up 4: queue filter, deskew never grows the canvas`. Small; do it in one go.

## Phase 7 fix-up 5 — split regions must be editable; #708 found 2 of 3 prints

George: **#708 was split into 2, it should have been 3.** A split the analyser gets wrong can't be accepted as-is, and there's no way to correct it by hand.

Second case: **#1398 split into 4, should be 8.** Eight prints on one bed means each is ~10% of the scan — under the 12% gate — so the per-region minimum relative to the whole scan is almost certainly the class bug; use both photos as fixtures.

Third case: **#3817 is a 12-picture panel (a contact/proof sheet) with 2 pictures cut out; the tool split it into 2.** Ten remaining prints in a grid, some adjacent with little or no bed between them — this one may be beyond the analyser, and that's fine: the manual region editor (item 2) is the answer for it, ideally with a "grid" helper (enter rows × columns, it lays out equal regions over the print area for George to nudge).

Fourth case, the opposite failure: **#3839 is a newspaper article and should not have been split at all** — columns of text with white gutters read as separate "prints". Two safeguards: (a) a region whose content is mostly text-like (high density of small dark strokes, low mid-tone variance) should count against splitting, and a scan whose regions share a single continuous background tone (newsprint, not bed) is one object; (b) more simply, when the `classify` job has labelled the photo `document`/`newspaper`/`text` (check what labels exist in `suggestions`), never propose a split — offer crop only. Use #3839 as a fixture.

So the review must also let George **reject a split and keep the photo whole** with one key — R currently sends it to manual-fix; add **W** ("keep whole"): marks the proposal rejected with reason `not_a_split`, the photo stays as it is, no file written.

1. **Diagnose #708, #1398, #3817 and #3839** with an overlay (mask, components, regions, the gates each component passed or failed). Likely: the third print is under `CLEANUP_SPLIT_MIN_FRAC` (12%) of the scan, or two prints touch/overlap and merged into one component, or one failed the rectangularity gate. Fix the class if it's a gate problem (e.g. per-region minimum relative to the *largest region* rather than the whole scan; watershed/erosion pass to separate touching prints) and re-check all 17 splits plus a scan of the "clean"/crop-only set for scans that have ≥ 2 large components but were never proposed as splits — report how many.
2. **Manual region editing in the split preview.** Regions are drawn as boxes on the before pane: drag to move, corners to resize, drag on empty bed to add a region, `Delete` to remove the selected one. Accept uses the edited regions (`operations.split_regions` updated, `details.edited_by='human'`, audit `cleanup.split` records original and edited). Faces map by containment against the edited regions exactly as before. Regions must not overlap and must lie within the scan.
3. **If a wrong split was already accepted**: undo (Z) in-session reverses it (children soft-deleted, parent restored from quarantine via the normal triage path, boxes restored from the audit row). Make sure `undo` covers splits — the prompt said "split undoable within the session" (answer 9); verify with a test, and document the cross-restart recipe (restore parent from quarantine browser, soft-delete children) in GC.md.

Commit: `Phase 7 fix-up 5: split diagnosis, manual region editing, split undo`.

**Fix-up 5 follow-up (diagnose only):** after re-analysis George reports **#1398 still not split properly, and #2651, #2716, #2717, #2718, #3817 split improperly.** Do not change the detector again on this round. Run the overlay diagnosis on all six and report, per photo: regions found vs what the scan actually contains (count), and the one-line reason (gutter not found / region merged / gate failed / proof sheet). If one clear class covers three or more of them, propose a fix and wait; otherwise the answer is the region editor, and say so. Also confirm the 9 re-analyses from the sweep were applied before George's review.

## Phase 7 fix-up 6 — grid helper lays boxes over the wrong extent

George on #3817 (3 × 4 grid): the boxes drift left as you go right — the leftmost column is on its print, the rightmost box sits noticeably left of its print. Cause: "Lay out grid" covers *the area the existing regions already cover*, and the two measured regions were narrower than the whole sheet, so the grid is squeezed and the error accumulates across columns.

1. **Grid extent = the sheet, not the regions.** Default the grid frame to the print rectangle measured by the analyser for the whole scan (the outer bounds of all print pixels — for a proof sheet that is the sheet itself). If the analyser has no rect, use the full image.
2. **Let George set the frame.** Draw the grid's outer frame as a draggable rectangle (corners resize, body moves) *before* the boxes are laid out: "Lay out grid" fills that frame with rows × columns. Re-pressing it after moving the frame re-lays the boxes. A "Gutter" spin box (percent, default 0) shrinks each cell evenly for sheets with white space between prints.
3. **Nudges after layout.** With a box selected, arrow keys move it 2 px (Shift = 10); Ctrl+arrows move the *whole column* that box belongs to; Alt+arrows the whole row. That's how a slightly uneven sheet gets fixed in a few keypresses rather than ten drags.
4. Test with a synthetic 3 × 4 sheet whose measured regions cover only the left two-thirds: after layout every box must be centred on its print within 2% of a cell.

5. **Go to photo.** The four Sears sheets (#2651, #2716–2718) came back with 0 regions after fix-up 5b, so they left the Splits filter and are now buried among 1,027 pending crops; George can't find them. Add a "Go to #" field next to the Show box: type a photo id, Enter jumps the queue to it regardless of filter (switching the filter to All pending if needed), with a clear message if the id has no live proposal. Also a **Hand-split** filter value listing photos whose proposal was edited with G but not yet accepted, so work in progress is findable.

Commit: `Phase 7 fix-up 6: grid helper frame, nudges, go-to`.

## Phase 7 fix-up 7 — 235 photos with raw dims and NULL orientation (do this now, before Phase 13)

Good catch; this is PROJECT-PLAN open item 13 finally explained. Approved, in this order:

1. `repair_face_boxes` learns to key on the **file's EXIF orientation** when `photos.orientation` is NULL (read the header; never trust a NULL as "1"). Dry run over all 235 first: print id, raw dims, EXIF tag, box count, labelled count. Then the real run: backfill `orientation`, swap width/height, rescale every live bbox by (H_raw/W_raw, W_raw/H_raw), regenerate the 280 face crops, audit row per photo. `diagnose_face_box` on three labelled ones (pick Samara Clay's if she has another) pasted in the report.
2. **Restore the two `cleanup_out_of_frame` boxes** (Samara's included): take the pre-accept bbox from the `cleanup.accept` audit row, map it raw→display with the same rescale, then through the accept's transform, un-soft-delete the row with the corrected bbox, regenerate the crop, audit `face.restore`. Confirm visually that both land on the faces.
3. Make ingest's `probe_image` path the only way `photos.orientation` gets set and add a post-condition to `check_working_files`: "photos with EXIF orientation ∉ {1, NULL} and `photos.orientation` NULL" must be 0. Say how the 235 slipped past the Phase 6 fix-up 6 backfill.
4. Push: the 235 photos' metadata and 280 face rows re-sync on the next push (bboxes changed → upsert; web crops are keyed by bbox hash so they regenerate). Verify one on the live site after George's next push.

Commit: `Phase 7 fix-up 7: orientation backfill from EXIF, restore out-of-frame boxes`.

**PM on the follow-up:** Part 1 rule accepted ("the cut subdivides found prints, never manufactures a split from one component"); the two-touching-prints cost is fine — G covers it. Part 2 accepted. The manual-fix copy deletion was correct and correctly reasoned (derived convenience copies, working file verified, re-creatable with R); add one line to CLAUDE.md making that explicit so nobody has to re-derive it. **Yes to the manual-queue filter change**: `manual_queue` skips photos with a live pending proposal; test; commit with the rest as `Phase 7 fix-up 5b: no invented splits, G on any scan, manual queue filter`.

**PM on the fix-up 5 sweep:** good catch on the fragmenting — reading the 29 individually instead of the headline is exactly the discipline that matters here. Apply the 9: George is not reviewing right now, so re-analyse those 9 photos by id (`--reanalyse` restricted to the list), confirm the result matches the sweep's prediction for each (incl. #3833 still a 4-way split, #3839 crop-only), and report. Then George reviews splits (Show: Splits) with G for #3817 and W for anything wrong. Note the chunked-sweep lesson in CLAUDE.md's memory paragraph: any whole-archive pass runs as resumable chunks in fresh processes from the first attempt.

**PM on the full-scope report (2026-09-30):** 1. Run the suite and commit the `draft()` loader change now, with a test that the draft path and the full-decode path give identical rects/ops on a fixture JPEG (and that TIFFs, which `draft()` doesn't touch, still go the old way). 2. Turn the "verification, not a guarantee" into a number: re-analyse a random 60 of the ~3,279 pre-change photos in a dry mode that writes nothing and compare `operations`/rect against the stored proposal; report mismatches (expected 0; any mismatch → re-analyse the whole pre-change set, `--reanalyse`, overnight). 3. Nothing else before George's review. Commit: `Phase 7: low-memory JPEG loader`.

**PM on the #15 diagnosis:** both deviations approved — outside-is-not-bed alone drives the push, and the threshold comes from the 455-scan sweep, not from #15. One more thing to try in the same sweep, because it addresses the root rather than the symptom: the print mask itself keys on brightness ("darker than bed − 25"), which is why white cardigans became bed. Scanner bed is *calm* (std ≈ 1.5 on #15) in a way no photograph is, even a white curtain (std 5–20). Pilot a mask that treats a pixel as bed only when it is near the bed colour **and** its local std (small window, ~7 px at analysis scale) is under the same tight threshold you settle on; everything else is print. If that mask finds #15's edge at ≈95 on its own and doesn't regress the other 454, use it and keep the guard as the second line of defence. If it's not clearly better, keep the guard-only version. Either way, `needs_manual: print_edge_unclear` for the unresolvable ones is correct.

## Progress

Newest first. Everything below is committed and pushed to `main`.

### Where it stands (2026-10-02)

| | |
|---|---|
| accepted | **991** — 964 of them one bulk accept of the geometric-only queue |
| clean (nothing to do) | 2,391 |
| pending | **62** — 61 needs-manual, 1 split, 0 hand-split |
| manual | 15 rows, 10 of them showing (5b hides those that came back) |
| rejected | 5 |
| split children created | 49 |

**George's next moves.** The four Sears proof sheets (#2651, #2716–#2718) want
`Go to #` → **G** → 2 × 3 → *Lay out grid*. #3839 (newspaper) wants **W**.
#1398 is already hand-split into its eight wallets and accepted.

**Waiting on a push:** the 235 photos and 280 face rows repaired in fix-up 7
re-sync on the next push, and verifying one face crop on the live site needs
that push to happen.

### The bulk accept (2026-10-02)

964 of 964, zero failures, ~4 minutes. `sum(file_version)` +964, accepted +964,
pending 1,026 → 62, disk +1 GB, every previous version kept in `_versions/`.
`check_working_files --dry-run` clean afterwards. Run as resumable chunks in
fresh processes, with a trial chunk of 25 verified before the rest.

Two face boxes went out of frame, which is what led to fix-up 7.

### Fix-up 7 — orientation backfill from EXIF (`650f9e9`)

**`photos.orientation` was NULL on all 12,686 photos because of a parse bug,
not because the archive has no rotated images.** Ingest read the orientation
from exifread's human-readable tag and matched it against a lookup written in
*exiftool's* vocabulary — exifread emits `Rotated 90 CW`, the table expected
`Rotate 90 CW`. The repair tool's dim-swap condition was therefore unreachable
and was never run. **This was PROJECT-PLAN open item 13.**

- 235 photos held raw dims where display dims belong; 280 face boxes (95
  labelled) sat in the wrong frame. Backfilled: 8,672 orientations set, 235
  photos repaired, 280 crops regenerated, re-scan shows 0 disagreements.
- The repair tool no longer decodes to read two numbers and a tag, and writes
  an audit row per photo carrying **every box as it was**, so it is reversible
  from the audit alone. The absence of such a row is how nobody could tell it
  had never run.
- Six photos had been accepted while their row held raw dims, so the accept
  mapped their boxes from the wrong frame: two fell outside (soft-deleted and
  flagged), four were misplaced by 85–343 px with nothing to notice. Which
  mapping each needed was decided by rendering the candidates and **looking** —
  #325 and #338 were already right; #330, #1462, #1910, #1920 needed the
  rescale. All six fixed, both deletions restored, live faces back to 23,425.
- Ingest now takes `orientation=dims.orientation` from `probe_image`, and
  `check_working_files` has a post-condition (reads 0) for a file that says it
  is rotated while the row says nothing. **A NULL orientation means "never
  established", not "upright".**

Worth keeping: the documented bbox rescale is a **scale**, not a rotation.
Verified by drawing both on real photos — the scale lands on the faces.

### Fix-up 6 — grid frame, nudges, go-to (`ef77d06`)

- `grid_boxes` divides the frame it is given. The frame defaults to the new
  `operations.print_bounds` (outer bounds of **every** print component = the
  sheet), is dragged by its border or corners, and has a percent **Gutter**
  that shrinks each cell about its centre. Framing on the found regions is
  what made #3817's cells drift a whole cell by the rightmost column.
  Measured after re-analysis: the frame is 79–89 % of the scan on the Sears
  sheets, 98 % on #3817.
- Arrows nudge 2 px (Shift 10); **Ctrl** moves the whole column, **Alt** the
  whole row, by where the boxes sit rather than by remembering the grid.
- **Go to #** jumps the queue to a photo whatever the filter says, and a
  **Hand-split** filter lists regions drawn with G but not yet accepted.

### Fix-up 5b — no invented splits, G anywhere, queue filter (`43aee61`, `19e67ee`)

- The gutter cut subdivides prints the mask already found and never
  manufactures a split from one component. Every split George accepted started
  from ≥ 2 native components; every wrong one from exactly 1.
- **G works on any proposal**, not just a split — which 5b made necessary, and
  which George used on #1398 to draw its 8 wallets by hand and accept them.
- `manual_queue` hides photos that have come back with a live pending
  proposal; `MANUAL_FIX_DIR` copies are derived and deletable (6 removed).

### Fix-up 5 — split diagnosis, region editing, undo (`a7f104b` … `c3090ca`)

Diagnosed #708/#1398/#3817/#3839, then the sweep caught three regressions the
four photos and the whole suite had missed (crops of 22–30 % on clean scans).
Shipped the region editor, **W** (keep whole), the `document` veto, the
relative size gate, and `--photo` for applying a sweep by id. Also fixed a
real bug: `select_scope` selected the classification label and dropped it, so
the veto could never fire in the job — only in the sweep, which passed it by
hand.

### Earlier

- Low-memory JPEG loader (`505cf3d`): 93.7 MP scan 661 MB → 254 MB.
- Fix-up 4 (`5c78bfb`): queue filter; deskew never grows the canvas (109
  proposals affected, photo #50 accepted before the fix and left alone).
- Fix-up 3 (`399e11f`): crop never removes picture content.
- Fix-up 2 (`9ab9fb1`): tonal ops opt-in; deskew from the print's edges.
- Fix-up 1 (`3feb13a`): colour cast needs highlight agreement.
- Phase 7 (`62282d8`): scan cleanup.

### Known and deliberately not done

- **#3839** (newspaper) is `needs_manual: print_too_small` rather than
  crop-only: the two clippings sit side by side, so a whole-scan crop would
  frame one and discard the other. **W** is the right key for it.
- **Photo #50** was accepted under fix-up 4's old behaviour and is 17 px larger
  than its scan. Internally consistent, boxes included; Undo + re-accept would
  re-cut it.
- **#1398 and #3817 are not fixable by detection** — pale studio backgrounds
  and touching prints respectively. The region editor is the answer, and
  #1398 is already done.
