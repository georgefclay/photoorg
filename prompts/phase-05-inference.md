# Phase 5 — Inference service (Mac mini)

Run in Claude Code **on the Mac mini**, in a clone of the repo, working directory `inference/`. Read `CLAUDE.md`, `PROJECT-PLAN.md` (§2 decisions, Phase 5, Risks), `shared/SCHEMA.md` (suggestions payload shapes), and `photo-archive-build-prompts.md` §2 first.

CONTEXT: The service runs on an M4 Mac mini (16 GB) today and moves to an M6 (32 GB) on 2026-09-22. It is called over the LAN by the Windows desktop app (Phase 6). It never touches the database; it takes images in and returns JSON. Nothing it returns is a fact — the desktop writes everything to `suggestions`.

Archive scale: ~13,700 photos after triage (fewer), ~4,700 scans, several hundred scanned backs with handwriting. At 5–15 s/image, a full pass is days, so batches must be interruptible and resumable, and each job must be re-runnable on its own.

## Stack
- Python 3.12, FastAPI + uvicorn, MLX (`mlx-vlm`) for the vision model, InsightFace (`insightface` + `onnxruntime`) for faces, Pillow, numpy.
- `pyproject.toml`, `.venv`, `README.md` with setup + run steps, `.env.example`:
  ```
  INFERENCE_HOST=0.0.0.0
  INFERENCE_PORT=8500
  INFERENCE_TOKEN=CHANGEME
  VLM_MODEL=mlx-community/Qwen3-VL-8B-Instruct-4bit
  FACE_MODEL=buffalo_l
  MAX_IMAGE_EDGE=1536
  LOG_DIR=./logs
  ```
- Model choice is config. On the M4 use the 4-bit 8B model; document the M6 swap (larger or 8-bit variant) as a one-line `.env` change. Verify the chosen model actually loads on this machine before writing endpoints; if it does not fit, drop to the smallest Qwen3-VL/Qwen2.5-VL that does and say so in the report.
- Images arrive as multipart uploads or as a `path` under a configurable shared folder (SMB from the laptop) — support both; multipart first.
- Bearer token in `Authorization`; reject anything else with 401. LAN-only; no TLS.
- Structured JSON logging per request: endpoint, image id (caller-supplied `ref`), bytes, model, wall time, tokens if available. Log file rotates daily.

## Endpoints
All accept a caller-supplied `ref` (string, echoed back) so the desktop can correlate. All return `{"ref":..., "model":..., "elapsed_ms":..., "result": {...}}`.

`POST /classify` — label ∈ `photo | document | screenshot | receipt | blank | back_of_print | other`, confidence 0–1, one-line reason. Used by triage and to catch backs the heuristics missed.

`POST /transcribe-back` — for a scanned back. Returns `text` (verbatim transcription, line breaks preserved, `[illegible]` for unreadable parts), `parsed_dates` (list of `{text, iso, precision}` — `1962`, `Mar 62`, `3/15/62`, `Easter 1962`), `names` (list of strings as written), `confidence`. Prompt must forbid guessing: transcribe what is there.

`POST /describe` — one factual sentence, no identities, no speculation about relationships or emotions; max 30 words. Plus `tags` (5–10 nouns). Used for full-text search.

`POST /estimate-date` — `year_min`, `year_max`, `confidence`, `reasoning` (clothing, hairstyles, cars, film grain, print border, paper finish, colour cast). Always a range of ≥ 3 years; ≥ 10 years when the model is unsure. Flag `is_scan_of_print` if it sees a print border.

`POST /detect-faces` — InsightFace: list of `{bbox:{x,y,w,h}, det_score, embedding:[512 floats], landmarks}` in pixel coordinates of the **original** image (undo any resize). Also `image_w`, `image_h`.

`POST /match-faces` — body: `{embedding:[...], references:[{person_id, embedding}], top_k}`. Cosine distance, ranked. The caller has already excluded disputed references; do not assume the set is trustworthy — return distances, never a decision.

`POST /batch/{endpoint}` — body: list of `{ref, path}` (shared-folder path variant only). Streams NDJSON, one result line per item as it finishes, plus a final summary line. Accepts `Range`-like `skip_refs` list so the caller can resume. Serialises model use (one VLM job at a time; faces can run concurrently).

`GET /health` — loaded models, VLM model name, free unified memory (via `mlx.core.metal.get_active_memory` / `psutil`), uptime, queue depth.

## Prompts
Keep every VLM prompt in `prompts/*.txt`, one per endpoint, versioned by filename (`transcribe-back.v1.txt`). The response includes `prompt_version`. Enforce JSON output (schema in the prompt + a parser that retries once on invalid JSON, then returns `{"error":"unparseable","raw":...}`).

## Resilience
- A model crash or OOM returns 503 with a reason; the process restarts the model on the next request (lazy load).
- Image over `MAX_IMAGE_EDGE` is downscaled for the VLM; face detection also runs at ≤ 1536 but scales boxes back.
- Request timeout 120 s; batches run in a background task; `/batch/status/{job_id}` and `/batch/cancel/{job_id}`.
- `launchd` plist in `ops/` to run at login, restart on crash, logs to `LOG_DIR`. README documents install/uninstall.

## Tests
- `pytest` with the model mocked for endpoint contract tests (auth, schema, echo of `ref`, error shapes).
- One marked-slow integration test that runs each endpoint on 3 real sample images placed in `inference/samples/` (a colour photo, a B&W print scan, a handwritten back — ask George to drop them in; do not commit anything with faces if he'd rather not).

## Verification, then stop
1. `/health` reports the loaded model and memory.
2. Each endpoint returns valid JSON on the sample images; paste the outputs.
3. `/batch/describe` on 30 images: kill the process at ~15, restart, resume with `skip_refs`; completes.
4. Throughput numbers per endpoint on the M4 (s/image), so Phase 6 can plan.
5. README done; `GC.md` on the laptop gets the Mac's hostname/IP, port, token location, and the launchd commands (George updates it).
6. Commit: `Phase 5: inference service`.

---

## Answers to Claude Code's questions

1. **Images.** George will `scp` a folder from the laptop: `inference/samples/` (3 files) and `inference/samples/batch30/` (30 files). Gitignore `inference/samples/` entirely — family photos never go in the repo. Wait for George to say they are there before steps 2–4.
2. **Shared folder.** Add `SHARED_ROOT=` (empty = path variant disabled, 400 on any `path` request). Reject anything that does not resolve inside it after symlink resolution. Phase 6 will use multipart uploads first; the share is a later optimisation, so no mount exists yet. For verification 3, put the 30 images in a local folder on the mini and point `SHARED_ROOT` at it.
3. **Token.** Default: generate, write to `inference/.env`, print once.
4. **Downloads.** Yes.
5–9. **Defaults**, all of them. On 8: keep `year` for season/holiday references, `text` verbatim; no new precision.
10. **launchd.** LaunchAgent. George: turn on automatic login for your user on the mini (System Settings → Users & Groups → Automatic login) so the service survives a power cut. Note it in GC.md.
11. **Commit and push** `Phase 5: inference service` to `origin/main` from the mini. Rule from now on: every phase pushes when it finishes, and every session starts with `git pull`. Two clones are fine under that rule.

GO once the images are in place.

---

## Phase 5 follow-up — unattended batches (run on the mini, after `git pull`)

George's laptop will not stay on for multi-day jobs. The mini must be able to hold the work and the results on its own.

1. **Upload endpoint.** `POST /batch/upload/{job_name}` multipart, many files per request, each with a `ref` (photo id). Stored under `SHARED_ROOT/inbox/{job_name}/{ref}.jpg`. Returns the count and total bytes held. Idempotent (re-upload overwrites). `GET /batch/inbox/{job_name}` lists refs present. `DELETE /batch/inbox/{job_name}/{ref}` and a `?done=true` sweep that removes inputs whose results exist.
2. **Start from inbox.** `POST /batch/{endpoint}` accepts `{"job_name": "...", "from_inbox": true}` and builds the item list from the inbox folder minus any refs already present in that job_name's results file. Results append to `LOG_DIR/batches/{job_name}.ndjson` — **keyed by job_name, not a random job id**, so a resumed run appends to the same file.
3. **Collect.** `GET /batch/results/{job_name}?after=<line_no>` streams NDJSON from that line; the caller tracks its own cursor. `GET /batch/results/{job_name}/summary` gives done/failed/pending counts.
4. **Persistent queue.** Jobs restart after a service restart: on boot, any job_name with inbox items lacking results is resumed automatically (order: transcribe_backs, detect_faces, classify, describe, estimate_date). Store queue state in `LOG_DIR/batches/queue.json`.
5. **Blackout on the mini.** Look up what runs on this machine on Tuesday and Friday early mornings (`crontab -l`, `launchctl list`, `~/Library/LaunchAgents`, `/Library/LaunchDaemons`, and `log show --predicate 'eventMessage contains "cron"' --last 7d` if needed). Report what you find. Set `BATCH_BLACKOUT` in `.env` to cover it with 30 minutes of margin on each side, format `Tue 04:30-07:30;Fri 04:30-07:30` (local time). The batch loop finishes the current item and sleeps through the window; interactive requests are unaffected. Also pause when `system_available_gb < 1.0`.
6. `MAX_IMAGE_EDGE` per endpoint: 1024 for classify/describe/estimate-date, 1536 for transcribe-back and detect-faces. Re-measure describe s/image at 1024 and report.
7. Tests for upload/inbox/results/resume-after-restart. Update README. Commit and push: `Phase 5 follow-up: unattended batches`.
