"""Unattended jobs outlive the service.

A multi-day pass cannot depend on a client staying connected or on the process
staying up. Every named job is recorded in LOG_DIR/batches/queue.json; on start-up
anything whose inbox still holds unprocessed refs is picked up again, in the order
the plan cares about: backs first, then faces, then the expensive descriptive work.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from . import batch as batchmod
from . import inbox
from .config import get_settings
from .logging_conf import log_event

# job_name -> endpoint for the jobs the project actually runs. A name outside
# this map still works; it just has to be started once with an explicit endpoint.
DEFAULT_ENDPOINTS: dict[str, str] = {
    "transcribe_backs": "transcribe-back",
    "detect_faces": "detect-faces",
    "classify": "classify",
    "describe": "describe",
    "estimate_date": "estimate-date",
}

# Cheapest and highest-value first: a back is strong evidence, faces are minutes,
# describe and date are days.
PRIORITY = list(DEFAULT_ENDPOINTS)


def queue_path() -> Path:
    return get_settings().batch_dir / "queue.json"


def load() -> dict[str, dict[str, Any]]:
    path = queue_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log_event("queue.unreadable", path=str(path))
        return {}
    jobs = data.get("jobs")
    return jobs if isinstance(jobs, dict) else {}


def save(jobs: dict[str, dict[str, Any]]) -> None:
    path = queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"jobs": jobs}, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)  # atomic, so a crash mid-write cannot lose the queue


def register(job_name: str, endpoint: str, status: str = "running") -> None:
    jobs = load()
    entry = jobs.get(job_name, {})
    entry.update(
        {
            "endpoint": endpoint,
            "status": status,
            "updated_at": time.time(),
        }
    )
    entry.setdefault("created_at", time.time())
    jobs[job_name] = entry
    save(jobs)


def mark(job_name: str, status: str) -> None:
    jobs = load()
    if job_name in jobs:
        jobs[job_name]["status"] = status
        jobs[job_name]["updated_at"] = time.time()
        save(jobs)


def endpoint_for(job_name: str) -> str | None:
    recorded = load().get(job_name, {}).get("endpoint")
    return recorded or DEFAULT_ENDPOINTS.get(job_name)


def results_path(job_name: str) -> Path:
    return get_settings().batch_dir / f"{job_name}.ndjson"


def pending_refs(job_name: str) -> list[str]:
    """Inbox refs with no successful result yet."""
    done = set(batchmod.completed_refs(results_path(job_name)))
    return [ref for ref in inbox.refs(job_name) if ref not in done]


def _priority(job_name: str) -> tuple[int, str]:
    try:
        return (PRIORITY.index(job_name), job_name)
    except ValueError:
        return (len(PRIORITY), job_name)


def resumable() -> list[str]:
    """Named jobs with work left, in priority order.

    Both the queue file and the inbox folders are consulted: a job uploaded but
    never started is still work the mini should pick up.
    """
    root = get_settings().inbox_dir
    if root is None:
        return []
    names = set(load())
    if root.is_dir():
        names |= {entry.name for entry in root.iterdir() if entry.is_dir()}

    out = []
    for name in names:
        if not inbox.NAME_RE.match(name):
            continue
        if endpoint_for(name) is None:
            log_event("queue.skip_unknown_endpoint", job_name=name)
            continue
        if pending_refs(name):
            out.append(name)
    return sorted(out, key=_priority)


class Supervisor:
    """Runs resumable jobs one at a time, in the background, with no client."""

    def __init__(self, start_job) -> None:
        self._start_job = start_job
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _loop(self) -> None:
        try:
            while True:
                names = resumable()
                names = [n for n in names if batchmod.registry.running(n) is None]
                if not names:
                    return
                job_name = names[0]
                endpoint = endpoint_for(job_name)
                log_event(
                    "queue.resume",
                    job_name=job_name,
                    endpoint=endpoint,
                    pending=len(pending_refs(job_name)),
                )
                job = self._start_job(job_name, endpoint)
                if job is None:
                    return
                while job.status == "running":
                    await asyncio.sleep(1)
                mark(job_name, job.status)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the supervisor must never take the app down
            log_event("queue.supervisor_failed", error=str(exc))
