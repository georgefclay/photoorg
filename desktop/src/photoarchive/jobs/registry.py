"""Central registry of the five Phase 6 jobs, in queue order."""
from __future__ import annotations

from ..inference_client import JOB_TO_ENDPOINT, QUEUE_ORDER
from .base import Job


def all_jobs() -> list[Job]:
    """Instantiate every job in queue order. Import-lazy so a broken writer
    doesn't blow up the whole registry."""
    from .classify import make_job as make_classify
    from .describe import make_job as make_describe
    from .detect_faces import make_job as make_detect_faces
    from .estimate_date import make_job as make_estimate_date
    from .transcribe_backs import make_job as make_transcribe_backs

    factories = {
        "transcribe_backs": make_transcribe_backs,
        "detect_faces": make_detect_faces,
        "classify": make_classify,
        "describe": make_describe,
        "estimate_date": make_estimate_date,
    }
    return [factories[name]() for name in QUEUE_ORDER]


def get_job(name: str) -> Job:
    for job in all_jobs():
        if job.name == name:
            return job
    raise KeyError(f"unknown job: {name!r} (expected one of {list(JOB_TO_ENDPOINT)})")
