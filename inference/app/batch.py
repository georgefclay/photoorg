"""Batch jobs: NDJSON out, one line per item, resumable by the caller.

A batch runs as a background task, so a dropped client connection does not kill
it. Every line is also appended to LOG_DIR/batches/{job_id}.ndjson, so a client
can re-attach and replay. Job state itself is in memory: after a service restart
the file is still there, and resume is caller-driven via skip_refs.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from . import blackout
from .config import get_settings
from .logging_conf import log_event

JOB_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

ItemRunner = Callable[[str, str], Awaitable[dict[str, Any]]]

_SENTINEL = object()


@dataclass
class BatchJob:
    job_id: str
    endpoint: str
    total: int
    skipped: int
    ndjson_path: Path
    status: str = "running"  # running | completed | cancelled | failed
    done: int = 0
    failed: int = 0
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    pause_reason: str | None = None

    def __post_init__(self) -> None:
        self._lock = asyncio.Lock()
        self._subscribers: list[asyncio.Queue] = []
        self._cancel = asyncio.Event()
        self._task: asyncio.Task | None = None

    # ---- fan-out -----------------------------------------------------------

    async def publish(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False, default=str)
        async with self._lock:
            with self.ndjson_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            for queue in self._subscribers:
                queue.put_nowait(line)

    async def _close_subscribers(self) -> None:
        async with self._lock:
            for queue in self._subscribers:
                queue.put_nowait(_SENTINEL)
            self._subscribers.clear()

    async def subscribe(self) -> tuple[list[str], asyncio.Queue | None]:
        """Replay what has already been written, then follow live lines."""
        async with self._lock:
            backlog: list[str] = []
            if self.ndjson_path.exists():
                backlog = self.ndjson_path.read_text(encoding="utf-8").splitlines()
            if self.finished:
                return backlog, None
            queue: asyncio.Queue = asyncio.Queue()
            self._subscribers.append(queue)
            return backlog, queue

    async def unsubscribe(self, queue: asyncio.Queue) -> None:
        async with self._lock:
            if queue in self._subscribers:
                self._subscribers.remove(queue)

    # ---- control -----------------------------------------------------------

    @property
    def finished(self) -> bool:
        return self.status != "running"

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def status_payload(self, include_refs: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "job_id": self.job_id,
            "endpoint": self.endpoint,
            "status": self.status,
            "total": self.total,
            "done": self.done,
            "failed": self.failed,
            "skipped": self.skipped,
            "remaining": max(0, self.total - self.done - self.failed),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_s": round((self.finished_at or time.time()) - self.started_at, 2),
            "ndjson_path": str(self.ndjson_path),
            "paused": self.pause_reason is not None,
            "pause_reason": self.pause_reason,
        }
        if include_refs:
            payload["completed_refs"] = completed_refs(self.ndjson_path)
        return payload


class BatchRegistry:
    def __init__(self) -> None:
        self._jobs: dict[str, BatchJob] = {}

    def get(self, job_id: str) -> BatchJob | None:
        return self._jobs.get(job_id)

    def running(self, job_name: str) -> BatchJob | None:
        job = self._jobs.get(job_name)
        return job if job is not None and job.status == "running" else None

    def new_job(
        self, endpoint: str, total: int, skipped: int, job_name: str | None = None
    ) -> BatchJob:
        # A named job keeps its results file across restarts, so a resumed run
        # appends to the same NDJSON instead of starting a fresh random one.
        job_id = job_name or (
            f"{endpoint}-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
        )
        path = get_settings().batch_dir / f"{job_id}.ndjson"
        path.parent.mkdir(parents=True, exist_ok=True)
        job = BatchJob(
            job_id=job_id, endpoint=endpoint, total=total, skipped=skipped, ndjson_path=path
        )
        self._jobs[job_id] = job
        return job

    def start(self, job: BatchJob, items: list[dict[str, str]], runner: ItemRunner) -> None:
        job._task = asyncio.create_task(_run_job(job, items, runner))


registry = BatchRegistry()


def pause_reason(settings=None) -> str | None:
    """Why an unattended batch should stand down right now, or None."""
    settings = settings or get_settings()

    window = blackout.active_window(settings.batch_blackout)
    if window is not None:
        return f"blackout until {window.ends_after(dt.datetime.now()):%a %H:%M}"

    try:
        import psutil

        free_gb = psutil.virtual_memory().available / 1024**3
        if free_gb < settings.batch_min_free_gb:
            return f"only {free_gb:.1f} GB free (need {settings.batch_min_free_gb})"
    except Exception:  # noqa: BLE001 - never let the memory check stop a batch
        pass
    return None


async def _wait_while_paused(job: BatchJob) -> None:
    """Finish the item in flight, then sleep through the window."""
    settings = get_settings()
    announced = None
    while not job.cancelled:
        reason = pause_reason(settings)
        if reason is None:
            if job.pause_reason is not None:
                log_event("batch.resume", job_id=job.job_id, was=job.pause_reason)
            job.pause_reason = None
            return
        job.pause_reason = reason
        if reason != announced:
            announced = reason
            log_event("batch.pause", job_id=job.job_id, reason=reason)
        await asyncio.sleep(settings.batch_pause_poll_s)


async def _run_job(job: BatchJob, items: list[dict[str, str]], runner: ItemRunner) -> None:
    log_event("batch.start", job_id=job.job_id, endpoint=job.endpoint, total=job.total)
    try:
        for item in items:
            await _wait_while_paused(job)
            if job.cancelled:
                job.status = "cancelled"
                break
            ref = item["ref"]
            path = item["path"]
            started = time.perf_counter()
            try:
                result = await runner(ref, path)
                job.done += 1
                await job.publish(
                    {
                        "ref": ref,
                        "path": path,
                        "ok": True,
                        "elapsed_ms": int((time.perf_counter() - started) * 1000),
                        **result,
                    }
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad file must not stop a batch
                job.failed += 1
                await job.publish(
                    {
                        "ref": ref,
                        "path": path,
                        "ok": False,
                        "elapsed_ms": int((time.perf_counter() - started) * 1000),
                        "error": _error_text(exc),
                    }
                )
        else:
            job.status = "completed"
    except asyncio.CancelledError:
        job.status = "cancelled"
    except Exception as exc:  # noqa: BLE001
        job.status = "failed"
        log_event("batch.failed", job_id=job.job_id, error=str(exc))
    finally:
        job.finished_at = time.time()
        if job.status == "running":
            job.status = "completed"
        summary = {"summary": True, **job.status_payload()}
        await job.publish(summary)
        await job._close_subscribers()
        log_event("batch.finish", **{k: v for k, v in summary.items() if k != "summary"})


def _error_text(exc: Exception) -> str:
    detail = getattr(exc, "detail", None)
    return str(detail) if detail else f"{type(exc).__name__}: {exc}"


async def stream_job(job: BatchJob, header: dict[str, Any] | None = None) -> AsyncIterator[str]:
    backlog, queue = await job.subscribe()
    try:
        if header is not None:
            yield json.dumps(header, ensure_ascii=False) + "\n"
        for line in backlog:
            yield line + "\n"
        if queue is None:
            return
        while True:
            line = await queue.get()
            if line is _SENTINEL:
                return
            yield line + "\n"
    finally:
        if queue is not None:
            await job.unsubscribe(queue)


def completed_refs(path: Path) -> list[str]:
    """Refs that finished successfully, read back off disk. Survives a restart."""
    refs: list[str] = []
    if not path.exists():
        return refs
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("summary"):
            continue
        if payload.get("ok") and payload.get("ref"):
            refs.append(str(payload["ref"]))
    return refs


def recover_from_disk(job_id: str) -> dict[str, Any] | None:
    """Job state is in memory, but the NDJSON file outlives a restart."""
    if not JOB_ID_RE.match(job_id):
        return None
    path = get_settings().batch_dir / f"{job_id}.ndjson"
    if not path.exists():
        return None
    done = failed = 0
    summary: dict[str, Any] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("summary"):
            summary = payload
        elif payload.get("ok"):
            done += 1
        else:
            failed += 1
    return {
        "job_id": job_id,
        "status": (summary or {}).get("status", "interrupted"),
        "done": done,
        "failed": failed,
        "recovered_from_disk": True,
        "ndjson_path": str(path),
        "note": (
            "Job state is in memory and did not survive a service restart. "
            "Counts were rebuilt from the NDJSON file; resume by passing the "
            "completed refs back as skip_refs."
        ),
    }
