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
