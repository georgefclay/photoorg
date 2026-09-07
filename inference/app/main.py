"""FastAPI app. Takes images in, returns JSON. Never touches the database:
nothing here is a fact, and the desktop writes every answer to `suggestions`.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

from . import batch as batchmod
from . import blackout
from . import inbox
from . import jobqueue
from . import normalize
from .auth import require_token
from .config import get_settings
from .faces import FacesUnavailable, cosine_distance, face_engine
from .images import LoadedImage, load_from_bytes, load_from_path
from .logging_conf import log_event, setup_logging
from .schemas import BatchRequest, Envelope, MatchFacesRequest
from .vlm import VLMUnavailable, active_memory_gb, vlm_engine

STARTED_AT = time.time()

# Strong references to fire-and-forget tasks; see start_inbox_job.
_background_tasks: set[asyncio.Task] = set()

# endpoint name -> (prompt file stem, normaliser)
VLM_ENDPOINTS: dict[str, tuple[str, Callable[[dict[str, Any]], dict[str, Any] | None]]] = {
    "classify": ("classify", normalize.classify),
    "transcribe-back": ("transcribe-back", normalize.transcribe_back),
    "describe": ("describe", normalize.describe),
    "estimate-date": ("estimate-date", normalize.estimate_date),
}
BATCH_ENDPOINTS = set(VLM_ENDPOINTS) | {"detect-faces"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    settings = get_settings()
    log_event(
        "service.start",
        vlm_model=settings.vlm_model,
        face_model=settings.face_model,
        max_image_edge=settings.max_image_edge,
        edge_overrides=settings.edge_overrides,
        shared_root=str(settings.shared_root_path) if settings.shared_root_path else None,
        blackout=blackout.describe(settings.batch_blackout),
    )
    # Anything the mini was part-way through picks itself back up. No client
    # needs to be there, which is the whole point of the inbox.
    supervisor.start()
    try:
        yield
    finally:
        await supervisor.stop()
        log_event("service.stop", uptime_s=round(time.time() - STARTED_AT, 1))


supervisor = jobqueue.Supervisor(lambda name, endpoint: start_inbox_job(name, endpoint))

app = FastAPI(title="photoorg inference", version="0.5.0", lifespan=lifespan)


# --------------------------------------------------------------------------
# image intake — multipart upload or a path under SHARED_ROOT
# --------------------------------------------------------------------------


@dataclass
class ImageRequest:
    ref: str
    loaded: LoadedImage


async def _read_image_request(request: Request, max_edge: int) -> ImageRequest:
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip()

    if content_type in ("multipart/form-data", "application/x-www-form-urlencoded"):
        form = await request.form()
        ref = str(form.get("ref") or "").strip()
        upload = form.get("file")
        path = form.get("path")
        if not ref:
            raise HTTPException(status_code=400, detail="Missing ref")
        if upload is not None and hasattr(upload, "read"):
            return ImageRequest(
                ref=ref, loaded=load_from_bytes(await upload.read(), max_edge)
            )
        if path:
            return ImageRequest(ref=ref, loaded=load_from_path(str(path), max_edge))
        raise HTTPException(status_code=400, detail="Provide a file upload or a path")

    if content_type == "application/json":
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"Invalid JSON body: {exc}") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Body must be a JSON object")
        ref = str(body.get("ref") or "").strip()
        path = body.get("path")
        if not ref:
            raise HTTPException(status_code=400, detail="Missing ref")
        if not path:
            raise HTTPException(status_code=400, detail="Missing path")
        return ImageRequest(ref=ref, loaded=load_from_path(str(path), max_edge))

    raise HTTPException(
        status_code=415,
        detail=(
            "Send multipart/form-data (file + ref), or application/json "
            "(ref + path) for a file under SHARED_ROOT"
        ),
    )


def image_request_for(endpoint: str):
    """Each endpoint gets its own downscale: image tokens are the cost driver."""

    async def dependency(request: Request) -> ImageRequest:
        return await _read_image_request(request, get_settings().edge_for(endpoint))

    return dependency


# --------------------------------------------------------------------------
# shared work
# --------------------------------------------------------------------------


async def _with_timeout(coro):
    timeout = get_settings().request_timeout_s
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"Timed out after {timeout}s",
        ) from exc


async def run_vlm(
    endpoint: str, loaded: LoadedImage, *, high_priority: bool = True
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    prompt_name, normaliser = VLM_ENDPOINTS[endpoint]
    timeout = get_settings().request_timeout_s
    try:
        output = await vlm_engine.run_json(
            prompt_name,
            loaded.image,
            high_priority=high_priority,
            timeout=timeout,
        )
    except asyncio.TimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"Vision model did not answer within {timeout}s",
        ) from exc
    except VLMUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Vision model unavailable: {exc}. It will be reloaded on the next request.",
        ) from exc

    if not output.parsed:
        result = output.result
    else:
        normalised = normaliser(output.result)
        result = (
            normalised
            if normalised is not None
            else {"error": "unparseable", "raw": output.raw}
        )

    meta = {
        "prompt_tokens": output.prompt_tokens,
        "generation_tokens": output.generation_tokens,
        "peak_memory_gb": output.peak_memory_gb,
        "attempts": output.attempts,
        "parsed": output.parsed,
    }
    return result, output.prompt_version, meta


async def run_faces(loaded: LoadedImage) -> dict[str, Any]:
    try:
        faces = await _with_timeout(face_engine.detect(loaded))
    except FacesUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Face model unavailable: {exc}. It will be reloaded on the next request.",
        ) from exc
    return {
        "image_w": loaded.original_w,
        "image_h": loaded.original_h,
        "faces": faces,
    }


def _envelope(
    ref: str, model: str, elapsed_ms: int, result: dict[str, Any], prompt_version: str | None
) -> dict[str, Any]:
    return Envelope(
        ref=ref,
        model=model,
        elapsed_ms=elapsed_ms,
        prompt_version=prompt_version,
        result=result,
    ).model_dump()


async def _vlm_route(endpoint: str, req: ImageRequest) -> dict[str, Any]:
    started = time.perf_counter()
    result, prompt_version, meta = await run_vlm(endpoint, req.loaded)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    log_event(
        "request",
        endpoint=f"/{endpoint}",
        ref=req.ref,
        bytes=req.loaded.nbytes,
        source=req.loaded.source,
        model=vlm_engine.model_name,
        prompt_version=prompt_version,
        elapsed_ms=elapsed_ms,
        **meta,
    )
    return _envelope(req.ref, vlm_engine.model_name, elapsed_ms, result, prompt_version)


# --------------------------------------------------------------------------
# single-image endpoints
# --------------------------------------------------------------------------


@app.post("/classify", dependencies=[Depends(require_token)])
async def classify(
    req: ImageRequest = Depends(image_request_for("classify")),
) -> dict[str, Any]:
    return await _vlm_route("classify", req)


@app.post("/transcribe-back", dependencies=[Depends(require_token)])
async def transcribe_back(
    req: ImageRequest = Depends(image_request_for("transcribe-back")),
) -> dict[str, Any]:
    return await _vlm_route("transcribe-back", req)


@app.post("/describe", dependencies=[Depends(require_token)])
async def describe(
    req: ImageRequest = Depends(image_request_for("describe")),
) -> dict[str, Any]:
    return await _vlm_route("describe", req)


@app.post("/estimate-date", dependencies=[Depends(require_token)])
async def estimate_date(
    req: ImageRequest = Depends(image_request_for("estimate-date")),
) -> dict[str, Any]:
    return await _vlm_route("estimate-date", req)


@app.post("/detect-faces", dependencies=[Depends(require_token)])
async def detect_faces(
    req: ImageRequest = Depends(image_request_for("detect-faces")),
) -> dict[str, Any]:
    started = time.perf_counter()
    result = await run_faces(req.loaded)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    log_event(
        "request",
        endpoint="/detect-faces",
        ref=req.ref,
        bytes=req.loaded.nbytes,
        source=req.loaded.source,
        model=face_engine.model_name,
        elapsed_ms=elapsed_ms,
        faces=len(result["faces"]),
    )
    return _envelope(req.ref, face_engine.model_name, elapsed_ms, result, None)


@app.post("/match-faces", dependencies=[Depends(require_token)])
async def match_faces(body: MatchFacesRequest) -> dict[str, Any]:
    started = time.perf_counter()
    probe = np.asarray(body.embedding, dtype=np.float32)
    dim = probe.shape[0]

    matches = []
    for index, reference in enumerate(body.references):
        candidate = np.asarray(reference.embedding, dtype=np.float32)
        if candidate.shape[0] != dim:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"references[{index}] has {candidate.shape[0]} dimensions, "
                    f"probe has {dim}"
                ),
            )
        distance = cosine_distance(probe, candidate)
        matches.append(
            {
                "person_id": reference.person_id,
                "reference_index": index,
                "distance": round(distance, 6),
                "similarity": round(1.0 - distance, 6),
            }
        )

    matches.sort(key=lambda m: m["distance"])
    top = matches[: body.top_k]
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    log_event(
        "request",
        endpoint="/match-faces",
        ref=body.ref,
        bytes=0,
        model="cosine",
        references=len(body.references),
        elapsed_ms=elapsed_ms,
    )
    # Distances only. Whether any of these is the person is the caller's call.
    return _envelope(
        body.ref,
        "cosine",
        elapsed_ms,
        {"matches": top, "reference_count": len(body.references), "dimensions": dim},
        None,
    )


# --------------------------------------------------------------------------
# batch — specific routes before the wildcard
# --------------------------------------------------------------------------


@app.post("/batch/upload/{job_name}", dependencies=[Depends(require_token)])
async def batch_upload(job_name: str, request: Request) -> dict[str, Any]:
    """Hand the mini a pile of images to hold and work through on its own.

    Send them as repeated `files` parts whose *filename* is the ref, e.g.
    `-F files=@4711.jpg`. To keep the client's own filenames, send a matching
    list of repeated `refs` fields instead, in the same order as the files.
    Re-uploading a ref overwrites it.
    """
    inbox.check_job_name(job_name)
    inbox.require_inbox_root()

    content_type = (request.headers.get("content-type") or "").split(";")[0].strip()
    if content_type != "multipart/form-data":
        raise HTTPException(
            status_code=415, detail="Send multipart/form-data with one or more `files`"
        )

    form = await request.form()
    uploads = [
        part
        for key in ("files", "file")
        for part in form.getlist(key)
        if hasattr(part, "read")
    ]
    if not uploads:
        raise HTTPException(status_code=400, detail="No files in the request")

    explicit = [str(value) for value in form.getlist("refs")]
    if explicit and len(explicit) != len(uploads):
        raise HTTPException(
            status_code=400,
            detail=f"{len(explicit)} refs for {len(uploads)} files; they must match",
        )

    stored: list[str] = []
    written = 0
    for index, upload in enumerate(uploads):
        filename = getattr(upload, "filename", None) or ""
        ref = explicit[index] if explicit else Path(filename).stem
        if not ref:
            raise HTTPException(
                status_code=400,
                detail=f"File {index} has no usable ref; name it <ref>.jpg or send `refs`",
            )
        inbox.check_ref(ref)
        data = await upload.read()
        if not data:
            raise HTTPException(status_code=400, detail=f"Empty upload for ref {ref!r}")
        inbox.store(job_name, ref, data, filename)
        stored.append(ref)
        written += len(data)

    log_event(
        "inbox.upload", job_name=job_name, stored=len(stored), bytes=written
    )
    return {
        "job_name": job_name,
        "stored": len(stored),
        "bytes": written,
        "refs": stored,
        "held": len(inbox.refs(job_name)),
        "held_bytes": inbox.total_bytes(job_name),
    }


@app.get("/batch/inbox/{job_name}", dependencies=[Depends(require_token)])
async def batch_inbox(job_name: str) -> dict[str, Any]:
    inbox.check_job_name(job_name)
    inbox.require_inbox_root()
    held = inbox.refs(job_name)
    pending = jobqueue.pending_refs(job_name)
    return {
        "job_name": job_name,
        "endpoint": jobqueue.endpoint_for(job_name),
        "count": len(held),
        "bytes": inbox.total_bytes(job_name),
        "refs": held,
        "pending": len(pending),
        "done": len(held) - len(pending),
    }


@app.delete("/batch/inbox/{job_name}/{ref}", dependencies=[Depends(require_token)])
async def batch_inbox_delete(job_name: str, ref: str) -> dict[str, Any]:
    inbox.check_job_name(job_name)
    inbox.require_inbox_root()
    if not inbox.delete(job_name, ref):
        raise HTTPException(status_code=404, detail=f"No such ref in inbox: {ref}")
    return {"job_name": job_name, "removed": [ref]}


@app.delete("/batch/inbox/{job_name}", dependencies=[Depends(require_token)])
async def batch_inbox_sweep(job_name: str, done: bool = False) -> dict[str, Any]:
    """Drop inputs whose results are already in. Pending inputs are never touched."""
    inbox.check_job_name(job_name)
    inbox.require_inbox_root()
    if not done:
        raise HTTPException(
            status_code=400,
            detail="Pass ?done=true to sweep inputs that already have results",
        )
    finished = set(batchmod.completed_refs(jobqueue.results_path(job_name)))
    removed = inbox.sweep(job_name, finished)
    log_event("inbox.sweep", job_name=job_name, removed=len(removed))
    return {
        "job_name": job_name,
        "removed": len(removed),
        "refs": removed,
        "remaining": len(inbox.refs(job_name)),
    }


@app.get("/batch/results/{job_name}/summary", dependencies=[Depends(require_token)])
async def batch_results_summary(job_name: str) -> dict[str, Any]:
    inbox.check_job_name(job_name)
    path = jobqueue.results_path(job_name)
    done = failed = lines = 0
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            lines += 1
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("summary"):
                continue
            if payload.get("ok"):
                done += 1
            elif "ok" in payload:
                failed += 1

    running = batchmod.registry.running(job_name)
    held = inbox.refs(job_name) if get_settings().inbox_dir else []
    return {
        "job_name": job_name,
        "endpoint": jobqueue.endpoint_for(job_name),
        "done": done,
        "failed": failed,
        "pending": len(jobqueue.pending_refs(job_name)) if held else 0,
        "held_in_inbox": len(held),
        "lines": lines,
        "running": running is not None,
        "paused": running.pause_reason if running is not None else None,
        "results_path": str(path),
    }


@app.get("/batch/results/{job_name}", dependencies=[Depends(require_token)])
async def batch_results(job_name: str, after: int = 0) -> StreamingResponse:
    """NDJSON from line `after` onwards; the caller keeps its own cursor."""
    inbox.check_job_name(job_name)
    path = jobqueue.results_path(job_name)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"No results for job {job_name!r}")
    if after < 0:
        raise HTTPException(status_code=400, detail="after must be >= 0")

    def lines():
        with path.open("r", encoding="utf-8") as handle:
            for number, line in enumerate(handle):
                if number >= after and line.strip():
                    yield line if line.endswith("\n") else line + "\n"

    return StreamingResponse(lines(), media_type="application/x-ndjson")


@app.get("/batch/status/{job_id}", dependencies=[Depends(require_token)])
async def batch_status(job_id: str, include_refs: bool = False) -> dict[str, Any]:
    job = batchmod.registry.get(job_id)
    if job is not None:
        return job.status_payload(include_refs=include_refs)
    recovered = batchmod.recover_from_disk(job_id)
    if recovered is not None:
        if include_refs:
            recovered["completed_refs"] = batchmod.completed_refs(
                get_settings().batch_dir / f"{job_id}.ndjson"
            )
        return recovered
    raise HTTPException(status_code=404, detail=f"No such job: {job_id}")


@app.post("/batch/cancel/{job_id}", dependencies=[Depends(require_token)])
async def batch_cancel(job_id: str) -> dict[str, Any]:
    job = batchmod.registry.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No such job: {job_id}")
    job.cancel()
    return {"job_id": job_id, "cancelling": not job.finished, "status": job.status}


@app.get("/batch/stream/{job_id}", dependencies=[Depends(require_token)])
async def batch_stream(job_id: str) -> StreamingResponse:
    job = batchmod.registry.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No such job: {job_id}")
    return StreamingResponse(
        batchmod.stream_job(job), media_type="application/x-ndjson"
    )


def _runner_for(endpoint: str) -> batchmod.ItemRunner:
    max_edge = get_settings().edge_for(endpoint)

    async def run_item(ref: str, path: str) -> dict[str, Any]:
        loaded = load_from_path(path, max_edge)
        if endpoint == "detect-faces":
            return {"model": face_engine.model_name, "result": await run_faces(loaded)}
        result, prompt_version, _meta = await run_vlm(
            endpoint, loaded, high_priority=False
        )
        return {
            "model": vlm_engine.model_name,
            "prompt_version": prompt_version,
            "result": result,
        }

    return run_item


def start_inbox_job(job_name: str, endpoint: str):
    """Run everything in this job's inbox that has no result yet.

    Returns None when there is nothing left to do. Safe to call twice: a job
    already running is returned rather than started again, so an auto-resume and
    a client request cannot both process the same ref.
    """
    already = batchmod.registry.running(job_name)
    if already is not None:
        return already

    held = inbox.paths(job_name)
    done = set(batchmod.completed_refs(jobqueue.results_path(job_name)))
    items = [
        {"ref": ref, "path": str(path)}
        for ref, path in sorted(held.items())
        if ref not in done
    ]
    if not items:
        return None

    job = batchmod.registry.new_job(
        endpoint, total=len(items), skipped=len(held) - len(items), job_name=job_name
    )
    jobqueue.register(job_name, endpoint, status="running")
    batchmod.registry.start(job, items, _runner_for(endpoint))

    async def _record_outcome() -> None:
        while job.status == "running":
            await asyncio.sleep(1)
        jobqueue.mark(job_name, job.status)

    # The event loop only holds a weak reference to a task, so a bare
    # create_task() can be collected before it runs. For a client-started job
    # this is the only thing that closes out queue.json.
    task = asyncio.create_task(_record_outcome())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return job


@app.post("/batch/{endpoint}", dependencies=[Depends(require_token)])
async def batch_run(endpoint: str, body: BatchRequest) -> StreamingResponse:
    if endpoint not in BATCH_ENDPOINTS:
        raise HTTPException(
            status_code=404,
            detail=f"No batch endpoint {endpoint!r}. Try one of: "
            + ", ".join(sorted(BATCH_ENDPOINTS)),
        )
    if get_settings().shared_root_path is None:
        raise HTTPException(
            status_code=400,
            detail="Batch takes shared-folder paths; SHARED_ROOT is not configured",
        )

    if body.from_inbox:
        if not body.job_name:
            raise HTTPException(
                status_code=400, detail="from_inbox needs a job_name"
            )
        inbox.check_job_name(body.job_name)
        job = start_inbox_job(body.job_name, endpoint)
        if job is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Nothing pending in inbox/{body.job_name}: upload images "
                    "first, or everything already has a result"
                ),
            )
        header = {
            "job_id": job.job_id,
            "job_name": body.job_name,
            "endpoint": endpoint,
            "total": job.total,
            "skipped": job.skipped,
            "resumed": job.done > 0 or job.skipped > 0,
        }
        return StreamingResponse(
            batchmod.stream_job(job, header=header), media_type="application/x-ndjson"
        )

    if body.job_name:
        inbox.check_job_name(body.job_name)
        if batchmod.registry.running(body.job_name) is not None:
            raise HTTPException(
                status_code=409, detail=f"Job {body.job_name!r} is already running"
            )

    skip = set(body.skip_refs)
    items = [
        {"ref": item.ref, "path": item.path} for item in body.items if item.ref not in skip
    ]
    skipped = len(body.items) - len(items)

    job = batchmod.registry.new_job(
        endpoint, total=len(items), skipped=skipped, job_name=body.job_name
    )
    header = {
        "job_id": job.job_id,
        "job_name": body.job_name,
        "endpoint": endpoint,
        "total": len(items),
        "skipped": skipped,
    }
    generator = batchmod.stream_job(job, header=header)
    batchmod.registry.start(job, items, _runner_for(endpoint))
    return StreamingResponse(generator, media_type="application/x-ndjson")


# --------------------------------------------------------------------------
# health — deliberately unauthenticated: LAN-only, and launchd checks it
# --------------------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, Any]:
    import psutil

    settings = get_settings()
    virtual = psutil.virtual_memory()
    running = [
        job.status_payload()
        for job in batchmod.registry._jobs.values()
        if job.status == "running"
    ]
    window = blackout.active_window(settings.batch_blackout)
    inbox_state: dict[str, Any] = {}
    if settings.inbox_dir is not None and settings.inbox_dir.is_dir():
        for entry in sorted(settings.inbox_dir.iterdir()):
            if entry.is_dir() and inbox.NAME_RE.match(entry.name):
                held = inbox.refs(entry.name)
                inbox_state[entry.name] = {
                    "held": len(held),
                    "pending": len(jobqueue.pending_refs(entry.name)),
                }
    return {
        "status": "ok",
        "uptime_s": round(time.time() - STARTED_AT, 1),
        "vlm": {
            "model": settings.vlm_model,
            "loaded": vlm_engine.loaded,
            "queue_depth": vlm_engine.lock.queue_depth,
        },
        "faces": {"model": settings.face_model, "loaded": face_engine.loaded},
        "memory": {
            "mlx_active_gb": active_memory_gb(),
            "system_available_gb": round(virtual.available / 1024**3, 2),
            "system_total_gb": round(virtual.total / 1024**3, 2),
            "system_percent_used": virtual.percent,
        },
        "batches": {
            "running": len(running),
            "jobs": running,
            "paused": batchmod.pause_reason(settings),
            "queue": jobqueue.load(),
            "inbox": inbox_state,
        },
        "blackout": {
            "windows": blackout.describe(settings.batch_blackout),
            "active": window is not None,
            "until": window.ends_after(dt.datetime.now()).isoformat()
            if window is not None
            else None,
        },
        "config": {
            "max_image_edge": settings.max_image_edge,
            "max_image_edge_by_endpoint": {
                name: settings.edge_for(name) for name in sorted(BATCH_ENDPOINTS)
            },
            "shared_root": str(settings.shared_root_path)
            if settings.shared_root_path
            else None,
            "inbox_dir": str(settings.inbox_dir) if settings.inbox_dir else None,
            "request_timeout_s": settings.request_timeout_s,
            "batch_min_free_gb": settings.batch_min_free_gb,
        },
    }


@app.exception_handler(VLMUnavailable)
async def _vlm_unavailable_handler(request: Request, exc: VLMUnavailable) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": f"Vision model unavailable: {exc}"})
