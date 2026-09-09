# Phase 6 — Inference client, batch runner, Faces mode

Read `CLAUDE.md`, `PROJECT-PLAN.md` (§2, Phase 6, the 2026-09-07 progress note with throughput numbers), `shared/SCHEMA.md` (faces, suggestions payloads, photo_job_status, job_runs/job_items), `inference/README.md` (endpoint contracts — especially upload → inbox → run → results → sweep, restart behaviour, blackout), and `photo-archive-build-prompts.md` §6. Work in `desktop/`. Start with `git pull` — Phase 5 and its follow-up were pushed from the Mac.

CONTEXT: The service is at `INFERENCE_URL` with `INFERENCE_TOKEN` (both in `desktop/.env`). Measured on the M4: classify 8.5 s, describe 9.1 s, estimate-date 11.9 s, transcribe-back 18.7 s, detect-faces 0.19 s per image. The mini runs batches unattended and keeps results on disk; the laptop hands work over and collects results later. Keep set: 12,821 photos, 826 backs. **Everything the models produce is a suggestion**, never a fact.

## Inference client (`inference_client/`)
- Abstract `InferenceClient` with a `LanInferenceClient` implementation covering: the single-image endpoints, `health()`, and the batch API (`upload`, `inbox`, `start from inbox`, `results after cursor`, `summary`, `sweep`, `cancel`).
- Images are sent as multipart from the working copy, downscaled client-side to the endpoint's edge (1024 for classify/describe/estimate-date, 1536 for transcribe-back/detect-faces), JPEG q85. Upload convention: the multipart filename stem is the `ref` (= photo id, or `b<id>` for backs).
- Retries with backoff on connection errors and 503; 401 is fatal (bad token — stop and tell the user); timeouts per endpoint (VLM 180 s, faces 30 s, uploads 60 s per chunk).
- `health()` drives a status-bar indicator (model name, green/red dot), polled every 30 s. Never block the GUI thread.

## Batch runner (`jobs/`) — hand over, then collect
One framework, five jobs: `transcribe_backs`, `detect_faces`, `classify`, `describe`, `estimate_date`. Each job has a selector (which photos still need it), an uploader, and a writer (response → DB rows).

**Hand over.** For each job: select eligible items (excluding those whose `photo_job_status` is `done` for the current model + prompt_version), upload them to `/batch/upload/{job_name}` in chunks of 50 with progress (12.8k images at ~150 KB ≈ 2 GB over the LAN — minutes), then `POST /batch/{endpoint}` with `from_inbox`. Record the hand-over in `job_runs`. Once handed over, the laptop can be shut down.

**Collect.** On app start and every 5 minutes while open: for each job with outstanding work, `GET /batch/results/{job_name}?after=<cursor>`, apply the writer per line, advance the cursor (new table `job_cursors(job_name PK, line_no, updated_at)` — migration), update `photo_job_status`, then sweep the inbox with `?done=true`. Collection is idempotent (re-reading a line must not duplicate rows).

**Jobs panel** (new sidebar mode): per job — eligible / uploaded / processed on mini / collected into DB, mini's ETA from `/summary`, blackout state, Run / Pause / Cancel (which call the service). Queue order: transcribe_backs → detect_faces → classify → describe → estimate_date. Blackout is enforced on the mini; no laptop-side blackout logic.

**Selectors and writers** (all restricted to `triage_status in ('keep','private')` and `is_deleted = false`):
- `transcribe_backs`: every `photo_backs` row without `transcribed_text`. Send the image as-is; if confidence < 0.5, re-queue the horizontally flipped and 180° variants (as separate refs `b<id>_f`, `b<id>_r`) and keep the best; record `orientation_used`. Writes `photo_backs.transcribed_text` + `transcription_confidence` (observational text may be stored directly, `transcription_confirmed=false`) **and** a `suggestions` row kind `transcription`, source `ai`, with `parsed_dates` and `names`. For each parsed date also a kind `date` suggestion on the **front** photo, confidence 0.8, payload per SCHEMA.md, `evidence: "handwritten on back: <text>"`. Orphan backs (`photo_id` null) get the transcription only.
- `detect_faces`: every keep photo with no `faces` rows from the current model. Insert `faces` rows (`source='ai'`, `person_id` null, `embedding`, `embedding_model`, `confidence`, bbox scaled back to working-copy pixels using returned `image_w/h`). Zero faces → `suggestions` row kind `classification`, payload `{"label":"no_people"}` (never set `has_no_people` directly).
- `classify`: every keep photo. `suggestions` row kind `classification`. Labels `document`/`screenshot`/`receipt`/`blank` with confidence ≥ 0.9 also set a Triage hint `ai_junk` (add to the hint CHECK) so George can bulk-junk them; `back_of_print` on a scan creates a pending `ingest_pairings` row exactly like the B key.
- `describe`: `suggestions` row kind `description` with `text` and `tags`.
- `estimate_date`: only photos with no confirmed date. `suggestions` row kind `date`, `payload.range = {year_min, year_max}`, `precision` = `decade` if the range spans ≥ 10 years else `year`, confidence = service confidence × 0.5, `evidence` = reasoning.

Every writer stores `model` and `prompt_version` on the suggestion.

## Faces mode (UI)
Goal: label thousands of faces by handling clusters, not faces.
- **Clustering**: agglomerative on cosine distance over all unlabelled `faces` embeddings (`FACE_CLUSTER_DIST=0.45` in `.env`; tune on real data). Recompute on demand (button); in-memory with numpy, not in the DB.
- **Cluster view**: one cluster at a time, largest first: grid of face crops (from the working copy via bbox, cached at `THUMBS_DIR/faces/{face_id}.jpg`), count, and the suggested match (nearest labelled person by mean embedding, **excluding faces with `is_disputed=true`** from the reference set, with distance). Actions: assign to existing person (autocomplete over `people.display_name` + `person_name_variants`), create new person (given/surname minimum), **split** (select faces that don't belong → new cluster), skip, "not a face" (soft-delete the face row, audit). Keys: `Enter` accept suggestion, `N` new person, `X` toggle selection, `S` split selected, `K` skip, `Delete` not-a-face on selected.
- **Person view**: all faces for a person; "this isn't them" → `is_disputed=true`, `disputed_by` null (desktop), note; disputed faces leave reference sets immediately.
- **Per-photo view** (from any thumbnail): boxes on the image; click a box to assign/dispute; draw a box for a missed face (`source='human'`; embedding via `/detect-faces` on the crop, or null).
- **Facts rule**: an assignment George makes here is an admin decision — write `faces.person_id`, `source='human'`, audit row. Accepting the AI's suggestion is the same. Contributor suggestions from the web stay in `suggestions` (Phase 9).

## People
Minimal management inside Faces mode: create, edit names (given, middle, surname, maiden, nickname), birth/death year, notes; merge two people (moves faces and variants, soft-deletes the loser, audit row). Full CRUD on the web later.

## Tests
- Client: retries, 401 fatal, timeouts, multipart shape and ref convention (mock HTTP server).
- Runner: selector excludes done items for current model/prompt; collect is idempotent (same line twice → one row); cursor advances; sweep called.
- Writers: each job's response → correct rows/payloads (fixtures from real responses in `inference/README.md`), bbox scaling, orientation retry, orphan backs.
- Clustering on synthetic embeddings; disputed exclusion.
- Merge people round-trip.

## Verification, then stop
1. `pytest` green.
2. Hand over `transcribe_backs` (826) and `detect_faces` (12,821); confirm both appear in the mini's queue and the laptop can be closed.
3. When collected: transcriptions — confidence histogram, how many yielded a parsed date, 10 samples; George spot-checks 5 against the images. Faces — total found, photos with none, faces-per-photo distribution.
4. Hand over `classify`, `describe`, `estimate_date`; `tools/job_report.py` prints per-job handed-over / processed / collected / ETA. Report as they complete over the following days.
5. Cluster; report cluster count and size distribution at the default threshold and ±0.05. George labels clusters for 20 minutes; report clusters and faces labelled, and how often the suggestion was right once a few people exist.
6. Update `CLAUDE.md` and `shared/SCHEMA.md`. `PROJECT-PLAN.md` is left to the PM.
7. Commit and push: `Phase 6: inference client, jobs, faces`.

---

## Answers to Claude Code's questions

1. **orientation_used**: payload only (transcription suggestion). No column.
2. **faces.is_deleted exists** (migration 4, `boolean not null default false`). Add `deleted_at` and `delete_reason` via migration; all selectors exclude deleted rows.
3. **ai_junk precedence**: (b) presort wins. Also record the AI label in `details.also` on the existing hint row so nothing is lost.
4. **prompt_version**: add `prompt_version text` to `photo_job_status` via migration; selector excludes done at current model **and** prompt_version.
5. **detect_faces selector**: confirmed — gate on `photo_job_status`, not on faces row count.
6. **Private on the mini**: yes. LAN-only, no egress. The rule is about the VM. Sweep removes inputs once results exist.
7. **classify → back_of_print row**: correct. Set `front_photo_id` to the immediate predecessor by scan_sequence if it is not itself a back/pending back (same rule as the B key), else NULL.
8. **Split**: (a), in-memory.
9. **Face crops**: precompute at collect time.
10. **Drawn box, service down**: insert with `embedding = null`, mark `photo_job_status` for `detect_faces` on that photo as `pending` so the next run fills it. Show a banner saying so.
11. **Hand-over**: sequential, one uploader, queue order.

GO.

---

## Phase 6 fix-up 1 — two small things seen on first run

1. The health dot was green while every call returned 401 (health is unauthenticated). Make `health()` also do one authenticated no-op (e.g. `GET /batch/inbox/_probe`) and show **amber with "bad token"** when that fails. Also validate at startup: if `INFERENCE_TOKEN` is `CHANGEME` or empty, banner in the Jobs panel.
2. `auto_collect` warns "HTTP 404: No results for job" for every job that has never been handed over. Treat 404 on results as "nothing yet" — debug log, not a warning — and skip jobs with no `job_runs` hand-over row.

Commit: `Phase 6 fix-up 1: token health, quiet 404s`.

3. **Hand-over read timeout.** `POST /batch/{endpoint}` with `from_inbox` streams NDJSON for the life of the batch; the client waited for the full body and hit `Read timed out` after 180 s even though the mini had started the job. Fix: open the request with `stream=True`, read the first line (job id / accepted count), record the hand-over, close the connection. Confirm via `/batch/results/{job}/summary` immediately after. Add a test with a mock server that never finishes streaming. Also: check `job_runs` for a hand-over row that was *not* written because of this timeout, and reconcile (the mini's summary is the source of truth for "handed over").

---

## Phase 6 fix-up 2 — clustering produced a 3,566-face cluster

The largest cluster after the first Recompute is 3,566 faces. That is chaining, not a person. Do this, in order, and report the size distribution after each step:

1. **Diagnose.** Print: total faces, det_score histogram, face box size histogram (short edge in px), and for the giant cluster its mean pairwise cosine distance and det_score/size distribution vs. the rest.
2. **Quality gate.** Exclude from clustering (and from reference sets) faces with `det_score < 0.7` or short edge < 40 px. Keep the rows; show them later under a "low quality" bucket that George can ignore or review. Store the gate values in `.env` (`FACE_MIN_SCORE`, `FACE_MIN_PX`).
3. **Linkage.** Use **average** (or complete) linkage, not single. With scipy available use `scipy.cluster.hierarchy.linkage(..., method='average', metric='cosine')` + `fcluster` at `FACE_CLUSTER_DIST`; otherwise implement average-linkage agglomerative directly. Single linkage is what chains.
4. **Recursive split.** Any cluster larger than `FACE_MAX_CLUSTER` (default 300) is re-clustered on its own members at threshold × 0.8, repeated until nothing exceeds the cap. Record in the cluster header "split from a larger cluster".
5. **Order** the queue by size but skip clusters with < 3 faces until the big ones are done (singletons are the long tail; George labels them from the per-photo view later).
6. Tests: a synthetic set with two tight groups joined by a chain of intermediates must come out as two clusters under average linkage and one under single.

Target after the fix: the largest cluster is a plausible single person (tens to a few hundred faces), and the top 20 clusters look clean to George.

Commit: `Phase 6 fix-up 2: face clustering quality gate, average linkage, recursive split`.

7. **Mixed-sibling clusters.** George's two sons as children land in the same clusters. In the cluster grid, order faces by distance from the cluster centroid (closest first) so the "other" person collects at the end and is easy to select with Shift-click / Shift-arrow ranges. Add a **"Split by nearest person"** action: once both people exist with a few labelled faces, one key (`B`) assigns every face in the cluster to whichever of the two nearest labelled people it is closer to, shows the proposed split as two groups, and George confirms or fixes before it commits. Also show each face's age-ish context: the photo's year (capture date or import folder) under the crop — brothers are easy to tell apart when you know the year.

---

## Phase 6 fix-up 3 — suggestions across ages

Average linkage (fix-up 2) correctly stops chaining but splits one person's lifetime into age bands (George's mother, ages 5–85, was one cluster under single linkage; now several). Keep average linkage. Make the suggestion step bridge the ages instead:

1. **Multi-prototype references.** For each labelled person, instead of one mean embedding, keep up to K prototypes (K = 5): k-means over that person's non-disputed, quality-gated faces (fewer if they have < 10 faces). The suggested match for a cluster is the person whose *nearest prototype* is closest to the cluster centroid. Once Mom has adult faces labelled and one childhood face assigned, her child prototype pulls in the rest.
2. **"Also probably this person" list.** Under the main suggestion, show the next 2 candidates with distances, so a near-miss is one click away.
3. **Same-person merge hints.** After Recompute, for every unlabelled cluster whose centroid is within `FACE_CLUSTER_DIST` of a labelled person's nearest prototype, badge it "likely <name>" in the queue header so George can Enter through them quickly.
4. Tests: synthetic person with two age-band blobs; single-mean suggestion misses the second blob, multi-prototype finds it.

Commit: `Phase 6 fix-up 3: multi-prototype references`.

---

## Phase 6 fix-up 4 — name suffix

George's family has Jr./II/III. Add `people.suffix text` (migration; nullable). Include it in the trigger-maintained `display_name`: `given "nickname" surname suffix (née maiden)` → e.g. `George Clay Jr.`, `John "Jack" Smith III (née …)` only when set. Add the field to the person editor in Faces mode (after surname), to the autocomplete display, and to `shared/SCHEMA.md`. Search (Phase 11) and the metadata writer (Phase 13) must include it; note that in `PROJECT-PLAN.md` Phase 11/13 lines is the PM's job — just make the column and display work here. Update the existing `display_name` test.

Commit: `Phase 6 fix-up 4: people.suffix`.

---

## Phase 6 fix-up 5 — see the whole photo from a face

In the cluster grid and the person view, a face crop is often not enough to decide who it is. Add:

1. **Space** (or double-click) on a face tile opens a preview pane (right-hand dock, or a lightbox if the pane is narrow) with the **full photo** fitted to the pane, the current face outlined, every other detected face on that photo outlined too — labelled with their person name where known — plus the caption strip: year, batch/sequence or folder, and back transcription text if the photo has one. Arrow keys move to the next/previous face in the cluster and the preview follows. Esc closes.
2. **Hold Space** (peek) shows it only while held, for quick checks without leaving the grid flow.
3. Clicking another outlined face in the preview jumps the grid cursor to that face's cluster (if unlabelled) or opens that person (if labelled) — siblings and spouses in the same photo are the fastest way to identify someone.
4. The same preview is reachable from the per-photo view and the Person view.

Commit: `Phase 6 fix-up 5: full-photo preview from faces`.

---

## Phase 6 fix-up 6 — face boxes land in the wrong place on some photos

George's example: a portrait phone photo (girl beside a statue). The face crop and the preview outline are on the statue's collar; the face is lower-left. Clustering was still correct, so the mini saw the right pixels — only the box mapping back to working-copy coordinates is wrong. Several photos show this; all are likely phone photos with EXIF orientation 6/8 (stored landscape, displayed portrait).

1. **Diagnose on this photo.** Print: EXIF orientation tag of the working copy; raw pixel dims; dims after `ImageOps.exif_transpose`; what the client sent (transposed or raw, and at what edge); `image_w/image_h` the service returned; the stored bbox; the bbox the preview draws; how the thumbnail/preview load the image (transposed or not). One of these disagrees.
2. **One rule everywhere:** all face coordinates are in the **EXIF-transposed (display) orientation** of the working copy at full resolution. Client: `exif_transpose` before downscale and record the transposed dims; scale boxes back using those. Crops, preview, and per-photo view: `exif_transpose` before drawing. Store `photos.orientation` (the EXIF value, migration) so nothing has to re-read EXIF later.
3. **Repair existing rows.** For every photo with orientation ≠ 1 that has faces, recompute the bbox from the raw box under the known transform (do not re-run detection) and regenerate the face crops. Report how many photos/faces were fixed. If the raw box cannot be recovered from what was stored, re-queue those photos for `detect_faces` (they are cheap).
4. Test: synthetic image with orientation 6 → detection on transposed image → box drawn on the transposed image lands on the synthetic face.

Commit: `Phase 6 fix-up 6: EXIF orientation for face boxes`.
