from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator


# Endpoint identifiers used by the batch API. The service accepts either these
# names directly (e.g. `/batch/detect-faces`) or a `job_name` route.
ENDPOINT_CLASSIFY = "classify"
ENDPOINT_DESCRIBE = "describe"
ENDPOINT_ESTIMATE_DATE = "estimate-date"
ENDPOINT_TRANSCRIBE_BACK = "transcribe-back"
ENDPOINT_DETECT_FACES = "detect-faces"

# The queue order the mini runs unattended jobs in. The laptop uses the same
# order for its sequential hand-over loop.
QUEUE_ORDER: tuple[str, ...] = (
    "transcribe_backs",
    "detect_faces",
    "classify",
    "describe",
    "estimate_date",
)

# Mapping from job_name (as used in job_cursors, photo_job_status) to the
# service endpoint slug.
JOB_TO_ENDPOINT: dict[str, str] = {
    "transcribe_backs": ENDPOINT_TRANSCRIBE_BACK,
    "detect_faces": ENDPOINT_DETECT_FACES,
    "classify": ENDPOINT_CLASSIFY,
    "describe": ENDPOINT_DESCRIBE,
    "estimate_date": ENDPOINT_ESTIMATE_DATE,
}

# Longest edge, in pixels, that each endpoint is measured at. Client-side
# downscale saves LAN bandwidth *and* matches what the mini would do anyway,
# so we do it once and hand over the small copy.
ENDPOINT_MAX_EDGE: dict[str, int] = {
    ENDPOINT_CLASSIFY: 1024,
    ENDPOINT_DESCRIBE: 1024,
    ENDPOINT_ESTIMATE_DATE: 1024,
    ENDPOINT_TRANSCRIBE_BACK: 1536,
    ENDPOINT_DETECT_FACES: 1536,
}

# Per-endpoint request timeout. VLM endpoints are slow; the upload chunk
# timeout is separate. Faces detection is fast; the timeout is defensive.
ENDPOINT_TIMEOUT_S: dict[str, float] = {
    ENDPOINT_CLASSIFY: 180.0,
    ENDPOINT_DESCRIBE: 180.0,
    ENDPOINT_ESTIMATE_DATE: 180.0,
    ENDPOINT_TRANSCRIBE_BACK: 180.0,
    ENDPOINT_DETECT_FACES: 30.0,
}

UPLOAD_CHUNK_TIMEOUT_S = 60.0
UPLOAD_CHUNK_SIZE = 50


class InferenceError(RuntimeError):
    """Base class for anything the client raises on top of network errors."""


class FatalAuthError(InferenceError):
    """401 from the service. The bearer token is wrong; do not retry."""


class ServiceUnavailable(InferenceError):
    """Service is 503 / connection refused after all retries. Try again later."""


@dataclass(frozen=True)
class HealthStatus:
    ok: bool
    model_name: str | None
    faces_model: str | None
    memory_free_gb: float | None
    inbox: dict[str, int]  # per job_name -> pending refs
    blackout_active: bool
    blackout_until: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class RefImage:
    """One image to upload, identified by an opaque ref (usually the photo id
    as a string, or `b<id>[_f|_r]` for a back and its retry orientations)."""

    ref: str
    path: Path
    # Optional pre-downscaled bytes to upload verbatim. If None, the client
    # reads `path` and downscales at upload time to the endpoint's edge.
    prepared_bytes: bytes | None = None


@dataclass(frozen=True)
class BatchUploadResult:
    accepted: int
    rejected: list[dict[str, Any]]  # from the service


@dataclass(frozen=True)
class BatchInboxStatus:
    total: int
    pending: int
    with_results: int


@dataclass(frozen=True)
class BatchStartResult:
    job_name: str
    endpoint: str
    accepted: bool
    already_running: bool
    raw: dict[str, Any]


@dataclass(frozen=True)
class BatchSummary:
    """From `/batch/results/{job_name}/summary` (or `/summary` if that is the
    service's path). Provides the ETA the Jobs panel shows."""

    job_name: str
    total: int
    done: int
    failed: int
    pending: int
    eta_seconds: float | None
    running: bool
    blackout_active: bool
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ResultLine:
    """One NDJSON line from `/batch/results/{job_name}?after=<cursor>`. The
    body is the raw envelope from the service (see inference/README.md
    "Every response has the same envelope"). `line_no` is the 1-based line
    index within the mini's per-job NDJSON file — the value we advance the
    cursor to after successful application."""

    line_no: int
    ref: str
    ok: bool
    model: str | None
    prompt_version: str | None
    elapsed_ms: int | None
    result: dict[str, Any]
    error: str | None
    raw: dict[str, Any]


class InferenceClient(ABC):
    """Pluggable interface. The LAN implementation talks to the Mac mini.
    Tests substitute a fake."""

    # --- health / single-image -------------------------------------------------

    @abstractmethod
    def health(self) -> HealthStatus: ...

    @abstractmethod
    def call_endpoint(
        self,
        endpoint: str,
        image: RefImage,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Single-image call. Returns the raw envelope."""

    # --- batch (hand-over side) ------------------------------------------------

    @abstractmethod
    def upload(
        self,
        job_name: str,
        images: Iterable[RefImage],
        *,
        endpoint_edge: int,
    ) -> BatchUploadResult:
        """Multipart upload one chunk of images to `/batch/upload/{job_name}`."""

    @abstractmethod
    def inbox(self, job_name: str) -> BatchInboxStatus: ...

    @abstractmethod
    def start_from_inbox(
        self,
        job_name: str,
        endpoint: str,
    ) -> BatchStartResult:
        """POST /batch/{endpoint} with {"job_name": ..., "from_inbox": true}."""

    # --- batch (collect side) --------------------------------------------------

    @abstractmethod
    def results_after(
        self,
        job_name: str,
        after: int,
    ) -> Iterator[ResultLine]:
        """Stream NDJSON result lines strictly after `after` (a line_no)."""

    @abstractmethod
    def summary(self, job_name: str) -> BatchSummary: ...

    @abstractmethod
    def sweep(self, job_name: str, *, done_only: bool = True) -> int:
        """DELETE /batch/inbox/{job_name}?done=<bool>. Returns count removed."""

    @abstractmethod
    def cancel(self, job_name: str) -> bool: ...
