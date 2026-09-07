# inference — the Mac mini's vision service

FastAPI service that takes images in and returns JSON. It runs on the Mac mini and
is called over the LAN by the Windows desktop app (Phase 6).

**It never touches the database.** Nothing it returns is a fact: every answer is a
suggestion, and the desktop writes them all to `suggestions` with `status='pending'`
for George to promote. See `../CLAUDE.md` (Facts vs. suggestions).

- Vision: Qwen3-VL 8B (4-bit) through MLX.
- Faces: InsightFace / ArcFace (`buffalo_l`) through ONNX Runtime. The VLM is
  never used for identity.
- Auth: one bearer token, LAN-only, no TLS.

## Setup

Needs Python 3.12 and the Xcode command line tools.

```bash
cd inference
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env          # then set INFERENCE_TOKEN
```

The first request downloads the models (~5.5 GB for the VLM, ~300 MB for
`buffalo_l`) into `~/.cache/huggingface` and `~/.insightface`. To pull the VLM
ahead of time:

```bash
.venv/bin/python -c "from huggingface_hub import snapshot_download as d; d('mlx-community/Qwen3-VL-8B-Instruct-4bit')"
```

## Run

```bash
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8500
curl -s localhost:8500/health | python3 -m json.tool
```

### As a LaunchAgent (runs at login, restarts on crash)

```bash
ops/install-launchagent.sh              # install + start
ops/install-launchagent.sh uninstall    # stop + remove
launchctl print gui/$UID/com.photoorg.inference | head -20
```

The agent starts at **login**, not at boot, because MLX needs a Metal-capable user
session. Turn on automatic login (System Settings → Users & Groups → Automatic
login) or the service will not come back after a power cut.

`logs/launchd.out.log` and `logs/launchd.err.log` are uvicorn's own output; the
service's structured log is `logs/inference.log`.

## Configuration

Everything lives in `.env` — see `.env.example` for the full list.

| Variable | Default | Notes |
|---|---|---|
| `INFERENCE_TOKEN` | `CHANGEME` | Bearer token. Anything else gets a 401. |
| `VLM_MODEL` | `mlx-community/Qwen3-VL-8B-Instruct-4bit` | The M6 swap is this line. |
| `FACE_MODEL` | `buffalo_l` | InsightFace model pack. |
| `MAX_IMAGE_EDGE` | `1536` | Longest edge fed to either model. |
| `SHARED_ROOT` | *(empty)* | Enables the `path` variant and the inbox. Empty = 400 on both. |
| `MAX_IMAGE_EDGE_OVERRIDES` | `classify=1024;describe=1024;estimate-date=1024` | Per-endpoint longest edge. |
| `BATCH_BLACKOUT` | *(empty)* | When unattended batches stand down. |
| `BATCH_MIN_FREE_GB` | `1.0` | Batches also pause below this much free RAM. |
| `FACE_DET_SIZE` | `1024` | Bigger finds smaller faces in group scans, slower. |
| `REQUEST_TIMEOUT_S` | `120` | Per request; a batch applies it per item. |

**Moving to the M6 (2026-09-22)** is one line in `.env` plus a restart:

```
VLM_MODEL=mlx-community/Qwen3-VL-8B-Instruct-8bit
```

Then `launchctl kickstart -k gui/$UID/com.photoorg.inference` and check `/health`
reports the new model. Nothing else changes; no code is model-specific.

## Sending an image

Two ways, and every endpoint takes both:

```bash
# multipart upload — the primary path, works from anywhere on the LAN
curl -s -X POST localhost:8500/classify \
  -H "Authorization: Bearer $TOKEN" \
  -F ref=photo-4711 -F file=@samples/colour.jpg

# a path under SHARED_ROOT — for a mounted share, avoids copying bytes
curl -s -X POST localhost:8500/classify \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"ref":"photo-4711","path":"Batch 00012/IMG004.JPG"}'
```

`ref` is yours; it is echoed back untouched so results can be matched to photo ids.
Paths are resolved (symlinks included) and refused with 400 if they land outside
`SHARED_ROOT`.

Every response has the same envelope:

```json
{"ref": "...", "model": "...", "elapsed_ms": 1234,
 "prompt_version": "classify.v1", "result": { }}
```

## Endpoints

| Endpoint | Result |
|---|---|
| `POST /classify` | `label` ∈ photo, document, screenshot, receipt, blank, back_of_print, other; `confidence`; `reason`. |
| `POST /transcribe-back` | `text` (verbatim, line breaks kept, `[illegible]` where unreadable), `parsed_dates`, `names`, `confidence`. |
| `POST /describe` | `text` (one factual sentence, ≤ 30 words, no identities), `tags` (5–10 nouns). |
| `POST /estimate-date` | `year_min`, `year_max`, `confidence`, `reasoning`, `is_scan_of_print`. |
| `POST /detect-faces` | `image_w`, `image_h`, `faces[]` with `bbox`, `det_score`, 512-float `embedding`, `landmarks`. |
| `POST /match-faces` | ranked `matches[]` with cosine `distance`. |
| `POST /batch/{endpoint}` | NDJSON stream, one line per item. |
| `POST /batch/upload/{job_name}` | Hand the mini images to hold and work through. |
| `GET /batch/inbox/{job_name}` | What is held, and how much is still pending. |
| `GET /batch/results/{job_name}` | Collect NDJSON from a cursor. |
| `GET /health` | Models, memory, uptime, queue depth, inbox, blackout. No token needed. |

`parsed_dates` entries are `{text, iso, precision}` where `precision` is one of
`exact | month | year | decade | unknown` — the same vocabulary as the database's
`date_precision` type, so Phase 6 can copy one straight into a `date` suggestion
payload. `iso` is padded to the first day of the period (`Mar 62` → `1962-03-01`)
or `null` when it cannot be resolved. Seasons and holidays ("Easter 1962") come
back as precision `year` with the text kept verbatim.

Face boxes and landmarks are in pixels of the **original** image. Detection runs on
a copy downscaled to `MAX_IMAGE_EDGE` and the coordinates are scaled back.

`/match-faces` returns distances and nothing else. The caller has already excluded
disputed references, and the service does not assume the set is trustworthy — it
never decides who someone is:

```bash
curl -s -X POST localhost:8500/match-faces -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{
    "ref": "face-9", "embedding": [ ], "top_k": 5,
    "references": [{"person_id": 12, "embedding": [ ]}]}'
```

### When the model cannot be trusted

If the VLM returns something that is not JSON, the parser retries once with a
repair instruction. If that also fails the result is
`{"error": "unparseable", "raw": "..."}` with HTTP 200, so a batch keeps going and
the caller can record the failure against that photo.

A model crash or OOM is a **503**; the model is unloaded and lazily reloaded on the
next request. A request over `REQUEST_TIMEOUT_S` is a **504**. That clock covers
generation only, not the wait for the model — a batch item queued behind a run of
interactive requests is queued, not late.

## Batches

Batches take shared-folder paths only, so `SHARED_ROOT` must be set.

```bash
curl -N -X POST localhost:8500/batch/describe \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"items":[{"ref":"1","path":"a.jpg"},{"ref":"2","path":"b.jpg"}],
       "skip_refs":[]}'
```

The response is NDJSON:

1. a header line — `{"job_id": ..., "endpoint": ..., "total": ..., "skipped": ...}`
2. one line per item as it finishes — `{"ref", "path", "ok", "elapsed_ms", "result", ...}`
3. a final summary line — `{"summary": true, "status": "completed", "done", "failed", ...}`

An item that fails gets `"ok": false` and an `error`; the batch carries on.

The job runs as a background task, so losing the connection does not kill it. Every
line is also appended to `logs/batches/{job_id}.ndjson`.

- `GET /batch/stream/{job_id}` — re-attach: replays the file, then follows live.
- `GET /batch/status/{job_id}?include_refs=true` — counts, and the refs that finished.
- `POST /batch/cancel/{job_id}` — stops after the item in flight.

**Resuming.** Job state is in memory, so a service restart loses it; the NDJSON
file does not. After a crash, ask for the completed refs and hand them straight
back:

```bash
REFS=$(curl -s "localhost:8500/batch/status/$JOB?include_refs=true" \
       -H "Authorization: Bearer $TOKEN" | python3 -c \
       'import json,sys; print(json.dumps(json.load(sys.stdin)["completed_refs"]))')
curl -N -X POST localhost:8500/batch/describe -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"items\": $ITEMS, \"skip_refs\": $REFS}"
```

Only one VLM job runs at a time. A single-image request **cuts ahead** of queued
batch items, so the Triage UI stays responsive during an overnight run. Face
detection runs independently of the VLM.

## Throughput on the M4 (16 GB)

Measured 2026-09-07 on `Qwen3-VL-8B-Instruct-4bit`, `FACE_DET_SIZE=1024`, images
from `D:\Photos` (~3500 px scans), at each endpoint's configured edge.

| Endpoint | edge | s/image | n | Whole keep set (12,821) |
|---|---:|---:|---:|---|
| `/classify` | 1024 | 8.5 | 15 | ~30 h |
| `/describe` | 1024 | 9.1 | 30 | ~32 h |
| `/estimate-date` | 1024 | 11.9 | 15 | ~42 h |
| `/transcribe-back` | 1536 | 18.7 | 3 | ~4 h over 826 backs |
| `/detect-faces` | 1536 | 0.19 | 30 | ~40 min |

Measured at 1536 first, then again after the per-endpoint edges came in:

| `/describe` | mean | median | min | max |
|---|---:|---:|---:|---:|
| at 1536 | 17.4 s | 17.2 s | 15.9 s | 22.1 s |
| at 1024 | 9.1 s | 9.0 s | 7.7 s | 14.6 s |

**1.92× for free.** Prompt processing dominates — ~1,800 image tokens per request
against 40–90 generated — so the image edge is the whole ballgame and prompt length
is noise. Descriptions at 1024 are still specific and factual; handwriting and small
faces keep 1536, where the detail actually pays. Every figure above is measured at
the edge it ships with.

Model load is 2.9 s (VLM, already downloaded) and about 1 s for `buffalo_l`.

For Phase 6 that means the VLM jobs are still **days, not a night**, and the
priority order (backs first, then faces, then describe, then date) is the right one.
Face detection over the whole archive is a coffee break.

Memory with both models resident: 5.4 GB held by MLX, peaks around 7.2 GB during
generation, leaving ~3.5 GB of the 16 GB free. It works, but there is no room for a
larger model until the M6.

## Unattended runs

George's laptop will not stay on for a multi-day job, so the mini holds the work
*and* the results. Upload once, walk away, collect whenever.

```bash
# 1. hand over the images. The filename's stem is the ref (the photo id).
curl -s -X POST localhost:8500/batch/upload/describe \
  -H "Authorization: Bearer $TOKEN" \
  -F files=@4711.jpg -F files=@4712.jpg

# ...or keep your own filenames and send the refs alongside, in the same order:
curl -s -X POST localhost:8500/batch/upload/describe \
  -H "Authorization: Bearer $TOKEN" \
  -F files=@00004711_a1b2c3d4.jpg -F refs=4711

# 2. start it. Re-running this after a crash picks up where it left off.
curl -sN -X POST localhost:8500/batch/describe \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"job_name":"describe","from_inbox":true}'

# 3. collect, from wherever your cursor got to.
curl -s "localhost:8500/batch/results/describe?after=120" -H "Authorization: Bearer $TOKEN"
curl -s localhost:8500/batch/results/describe/summary -H "Authorization: Bearer $TOKEN"

# 4. free the disk once the results are safely in the database.
curl -s -X DELETE "localhost:8500/batch/inbox/describe?done=true" -H "Authorization: Bearer $TOKEN"
```

Uploads land in `SHARED_ROOT/inbox/{job_name}/{ref}.jpg` and re-uploading a ref
overwrites it, so a retried upload is harmless. Results append to
`LOG_DIR/batches/{job_name}.ndjson` — **keyed by job_name, not a random id** — so a
resumed run adds to the same file instead of starting a new one. `?done=true` only
removes inputs that already have a result; a pending input is never touched.

### It restarts itself

Job state is recorded in `LOG_DIR/batches/queue.json`. On start-up the service looks
for any job whose inbox still holds refs without results and picks it up with no
client attached, in this order:

`transcribe_backs` → `detect_faces` → `classify` → `describe` → `estimate_date`

Backs are the highest-value evidence and faces are minutes; the descriptive work is
days, so it goes last. Those five names map to endpoints automatically. Any other
`job_name` works too — it just has to be started once with an explicit endpoint so
the queue knows what to run.

A power cut, a crash, `kill -9`, a reboot: the LaunchAgent brings the service back
and the service brings the job back.

### Standing down

The mini is George's machine before it is a batch runner.

```
BATCH_BLACKOUT=Tue 04:30-07:30;Fri 04:30-07:30
BATCH_MIN_FREE_GB=1.0
```

Inside a window, or when free RAM drops below `BATCH_MIN_FREE_GB`, the batch
finishes the item in flight and then sleeps, re-checking every
`BATCH_PAUSE_POLL_S`. **Interactive requests are never affected** — a blackout is
about not competing for the machine overnight, not about refusing work. `/health`
reports the windows, whether one is active, and when it lifts.

## Prompts

One file per endpoint in `prompts/`, versioned by filename
(`transcribe-back.v1.txt`). The highest version wins and its name is echoed as
`prompt_version`, so a re-run after a prompt change is identifiable.

To change a prompt, add `name.v2.txt` — do not edit v1. Phase 6 re-runs a job by
setting `photo_job_status.status='pending'` for that job name.

## Tests

```bash
.venv/bin/python -m pytest -m "not slow"   # contract tests, model mocked, ~1 s
.venv/bin/python -m pytest -m slow -s      # real models on inference/samples/
```

The slow tests need `samples/colour.jpg`, `samples/bw.jpg` and `samples/back.jpg`.
They are family photos: `inference/samples/` is gitignored and nothing in it is
ever committed. The tests skip if the files are absent.
