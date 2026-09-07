"""FastAPI app. Takes images in, returns JSON. Never touches the database:
nothing here is a fact, and the desktop writes every answer to `suggestions`.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

from . import batch as batchmod
from . import normalize
from .auth import require_token
from .config import get_settings
from .faces import FacesUnavailable, cosine_distance, face_engine
from .images import LoadedImage, load_from_bytes, load_from_path
from .logging_conf import log_event, setup_logging
from .schemas import BatchRequest, Envelope, MatchFacesRequest
from .vlm import VLMUnavailable, active_memory_gb, vlm_engine

STARTED_AT = time.time()

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
        shared_root=str(settings.shared_root_path) if settings.shared_root_path else None,
    )
    yield
    log_event("service.stop", uptime_s=round(time.time() - STARTED_AT, 1))


app = FastAPI(title="photoorg inference", version="0.5.0", lifespan=lifespan)


# --------------------------------------------------------------------------
# image intake — multipart upload or a path under SHARED_ROOT
# --------------------------------------------------------------------------


@dataclass
class ImageRequest:
    ref: str
    loaded: LoadedImage


async def image_request(request: Request) -> ImageRequest:
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip()

    if content_type in ("multipart/form-data", "application/x-www-form-urlencoded"):
        form = await request.form()
        ref = str(form.get("ref") or "").strip()
        upload = form.get("file")
        path = form.get("path")
        if not ref:
            raise HTTPException(status_code=400, detail="Missing ref")
        if upload is not None and hasattr(upload, "read"):
            return ImageRequest(ref=ref, loaded=load_from_bytes(await upload.read()))
        if path:
            return ImageRequest(ref=ref, loaded=load_from_path(str(path)))
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
        return ImageRequest(ref=ref, loaded=load_from_path(str(path)))

    raise HTTPException(
        status_code=415,
        detail=(
            "Send multipart/form-data (file + ref), or application/json "
            "(ref + path) for a file under SHARED_ROOT"
        ),
    )


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
async def classify(req: ImageRequest = Depends(image_request)) -> dict[str, Any]:
    return await _vlm_route("classify", req)


@app.post("/transcribe-back", dependencies=[Depends(require_token)])
async def transcribe_back(req: ImageRequest = Depends(image_request)) -> dict[str, Any]:
    return await _vlm_route("transcribe-back", req)


@app.post("/describe", dependencies=[Depends(require_token)])
async def describe(req: ImageRequest = Depends(image_request)) -> dict[str, Any]:
    return await _vlm_route("describe", req)


@app.post("/estimate-date", dependencies=[Depends(require_token)])
async def estimate_date(req: ImageRequest = Depends(image_request)) -> dict[str, Any]:
    return await _vlm_route("estimate-date", req)


@app.post("/detect-faces", dependencies=[Depends(require_token)])
async def detect_faces(req: ImageRequest = Depends(image_request)) -> dict[str, Any]:
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

    skip = set(body.skip_refs)
    items = [
        {"ref": item.ref, "path": item.path} for item in body.items if item.ref not in skip
    ]
    skipped = len(body.items) - len(items)

    async def run_item(ref: str, path: str) -> dict[str, Any]:
        loaded = load_from_path(path)
        if endpoint == "detect-faces":
            return {
                "model": face_engine.model_name,
                "result": await run_faces(loaded),
            }
        result, prompt_version, _meta = await run_vlm(
            endpoint, loaded, high_priority=False
        )
        return {
            "model": vlm_engine.model_name,
            "prompt_version": prompt_version,
            "result": result,
        }

    job = batchmod.registry.new_job(endpoint, total=len(items), skipped=skipped)
    header = {
        "job_id": job.job_id,
        "endpoint": endpoint,
        "total": len(items),
        "skipped": skipped,
    }
    generator = batchmod.stream_job(job, header=header)
    batchmod.registry.start(job, items, run_item)
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
        "batches": {"running": len(running), "jobs": running},
        "config": {
            "max_image_edge": settings.max_image_edge,
            "shared_root": str(settings.shared_root_path)
            if settings.shared_root_path
            else None,
            "request_timeout_s": settings.request_timeout_s,
        },
    }


@app.exception_handler(VLMUnavailable)
async def _vlm_unavailable_handler(request: Request, exc: VLMUnavailable) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": f"Vision model unavailable: {exc}"})
