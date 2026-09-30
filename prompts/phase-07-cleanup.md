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

**PM on the full-scope report (2026-09-30):** 1. Run the suite and commit the `draft()` loader change now, with a test that the draft path and the full-decode path give identical rects/ops on a fixture JPEG (and that TIFFs, which `draft()` doesn't touch, still go the old way). 2. Turn the "verification, not a guarantee" into a number: re-analyse a random 60 of the ~3,279 pre-change photos in a dry mode that writes nothing and compare `operations`/rect against the stored proposal; report mismatches (expected 0; any mismatch → re-analyse the whole pre-change set, `--reanalyse`, overnight). 3. Nothing else before George's review. Commit: `Phase 7: low-memory JPEG loader`.

**PM on the #15 diagnosis:** both deviations approved — outside-is-not-bed alone drives the push, and the threshold comes from the 455-scan sweep, not from #15. One more thing to try in the same sweep, because it addresses the root rather than the symptom: the print mask itself keys on brightness ("darker than bed − 25"), which is why white cardigans became bed. Scanner bed is *calm* (std ≈ 1.5 on #15) in a way no photograph is, even a white curtain (std 5–20). Pilot a mask that treats a pixel as bed only when it is near the bed colour **and** its local std (small window, ~7 px at analysis scale) is under the same tight threshold you settle on; everything else is print. If that mask finds #15's edge at ≈95 on its own and doesn't regress the other 454, use it and keep the guard as the second line of defence. If it's not clearly better, keep the guard-only version. Either way, `needs_manual: print_edge_unclear` for the unresolvable ones is correct.

## Progress

Newest first. Commits are on `main` and pushed unless marked otherwise.

### Fix-up 5 — split diagnosis, manual region editing, split undo (2026-09-30)

**Done.**

**Diagnosed all four scans with an overlay** — mask, components, and every
gate's value. They failed for three different reasons, and only one of them is
a gate:

| scan | wanted | cause | now |
|---|---|---|---|
| #708 | 3, got 2 | the lower two prints **touch** — one component covered both | **3** |
| #1398 | 8, got 2 | pale studio backgrounds read as bed; the mask is holes | 2 |
| #3817 | 10, got 2 | prints touch with **no bed at all** between them | components 2→6, 2 regions |
| #3839 | 0, got 2 | two newspaper cuttings — real objects, not prints | **0** (vetoed) |

The per-region-minimum hypothesis was only part of it, and not the part that
mattered for #708 or #3817: those prints were never separated, so no gate could
have saved them. Measured directly — #708 has a 62 px band of bed through the
merged component, #3817 has none at any threshold.

- **`split_merged_components`** cuts a component along bands of bed running
  across it. Evidence is bed tone + flatness at a small window + reachable
  from the scan border, not absence from the print mask (the mask has already
  bridged the gutter, which is why they merged). `CLEANUP_GUTTER_STD_WINDOW_FRAC`
  is 0.003 — a fifth of the mask's — because the window has to fit *inside*
  a gutter.
- **Relative size gate** `CLEANUP_SPLIT_REL_MIN=0.55`: a region counts if it is
  large absolutely **or** comparable to the largest region. Eight equal prints
  at 8.8 % of the scan now split eight ways; the absolute gate admitted none.
- **Document veto** `CLEANUP_SPLIT_SKIP_LABELS=document,screenshot,back_of_print`,
  read from the newest non-rejected `classification` suggestion via
  `ScopeRow.ai_label`. #3839 is already labelled `document` at 0.98, so I took
  the PM's option (b) and **skipped the text-density heuristic (a)** — it
  would risk regressions across 3,400 scans to solve a case the classifier
  already answers. Flagging that as a deliberate omission.
- **W — keep whole.** `accept.keep_whole` marks the proposal `rejected` with
  `reason='not_a_split'`, writes the audit row, and stops: no file written
  anywhere, `photos` untouched. Works on any proposal, not just a split.
- **G — manual region editing.** `regions.py` holds every rule and is free of
  Qt and the database (no overlap beyond a 2 % touching tolerance, inside the
  scan, above 0.5 % of it); `region_editor.py` is the dialog — drag to move,
  corners to resize, drag on empty bed to add, `Del` to remove, plus the
  rows × columns **grid helper** for #3817. Saving rewrites `split_regions`
  with `edited_by='human'` and writes a `cleanup.split` audit row carrying
  **both** the measured and the drawn regions. Transforms are built by the
  analyser's own `transform_for`, so `accept_split` cannot tell an edited
  region from a measured one — faces still map by containment, children still
  get their own `region_key`.
- **Split undo** already existed and was tested at module level; added the path
  through the **Z** key, and extended the cross-restart recipe in `GC.md`
  (gitignored, not committed) for edited regions and for W.
- **Tests:** `test_cleanup_regions.py` (16), `test_cleanup_split_detect.py`
  (18), `test_cleanup_split_edit.py` (11), plus 6 in `test_cleanup_ui.py`.
  Two real fixtures cut from the archive — `scan_708_three_prints.jpg` and
  `scan_3839_newspaper.jpg` — each with a test asserting it still reproduces
  the original wrong answer, so the fix cannot be proved by a fixture that
  stopped exhibiting the bug.

**Outstanding.**

- **#1398 and #3817 are not fixed by detection**, and I do not think they can
  be. #1398's prints have pale calm backgrounds that any bed test reads as
  bed; #3817's prints touch with no bed between them. The region editor and
  its grid helper are the answer for both — as the PM anticipated for #3817.
- **The archive-wide sweep is still running** (3,446 live proposals
  re-measured against the new detector, writing nothing). At 800 it stood at
  **2 region-count changes and 14 print-rect moves > 8 px**. This is the
  number that decides whether `CLEANUP_SPLIT_GUTTERS` should stay on by
  default, and it also answers "how many clean/crop-only scans have ≥ 2 real
  regions". **Not yet reported.**
- `test_cleanup_ui.py` re-run pending (the earlier failures were two pytest
  processes against the shared `photoorg_test` at once, not a code fault —
  the same collision noted during the full-scope run).

### Fix-up 4 — queue filter, deskew never grows the canvas (2026-09-30)

Commit `5c78bfb`. **Done.**

- `Show:` filter (All pending / Splits / Needs manual / Geometric-only), each
  labelled with its size, persisted in QSettings under `cleanup/queue_filter`.
  *Geometric-only* is `Proposal.is_geometric_only` written in SQL with a test
  asserting the two agree.
- `transform_for`'s deskew-only branch kept the whole rotated canvas, so
  accepted files came out **larger** than the scan. Now clipped to a
  source-sized window; the rule `out_w <= src_w and out_h <= src_h` holds for
  everything that is not a split region.
- **109 of 3,434** proposals were affected (median +2.1 % area, worst +47.4 %).
  108 were pending and are fixed. **Photo #50 was already accepted** under the
  old behaviour — its working copy is 3527×2382 from a 3510×2357 scan. Left
  alone (it is internally consistent, boxes included); Undo + re-accept would
  re-cut it. **Your call.**

### Low-memory JPEG loader (2026-09-30)

Commit `505cf3d`. **Done.**

- `draft()` was never engaging: Pillow picks the scale as
  `min(w // box_w, h // box_h)`, so a square box is governed by the **short**
  edge and every 4:3 scan decoded in full. The box now matches the aspect.
- My earlier claim that `draft()` fixed the memory kills was **wrong** — 18 of
  the 27 largest scans are TIFF, where `draft` does nothing. What cost the
  memory was two needless full-size copies (`exif_transpose` returning
  `image.copy()`, and `convert("RGB")` on a 93.7 MP greyscale TIFF).
- Measured peak working set: 93.7 MP greyscale TIFF **661 MB → 254 MB**;
  33 MP greyscale TIFFs 321 → 163 MB; load time over the 27 largest scans
  66 s → 8 s.
- Verification, writing nothing: all 83 scans the change can touch plus a
  random 60 of the remaining 3,375. **0 mismatches in the 60**; across the 83,
  median change 0.000, max 0.260° and 2 px. **One** photo changes its op set
  (#1696, a 0.44° deskew falls under the 0.3° threshold). I did **not** run
  the whole-set `--reanalyse` the contingency called for — it would rewrite
  3,458 rows to change one, right before your review. One command away.

### Earlier

- Fix-up 3 — crop never removes picture content: commit `399e11f`.
- Fix-up 2 — colour off by default, deskew from the print's edges: `9ab9fb1`.
- Fix-up 1 — colour cast must show on the paper: `3feb13a`.
- Phase 7 — scan cleanup: `62282d8`.
