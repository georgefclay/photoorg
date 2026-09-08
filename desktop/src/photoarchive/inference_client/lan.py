from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Iterable, Iterator

import requests

from .base import (
    BatchInboxStatus,
    BatchStartResult,
    BatchSummary,
    BatchUploadResult,
    ENDPOINT_TIMEOUT_S,
    FatalAuthError,
    HealthStatus,
    InferenceClient,
    InferenceError,
    RefImage,
    ResultLine,
    ServiceUnavailable,
    UPLOAD_CHUNK_TIMEOUT_S,
)
from .image_prep import prepare_jpeg

log = logging.getLogger(__name__)


RETRIABLE_STATUS = {502, 503, 504}


class LanInferenceClient(InferenceClient):
    """HTTP client for the Mac mini service. See inference/README.md."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        max_retries: int = 4,
        backoff_base_s: float = 0.5,
        health_timeout_s: float = 5.0,
        session: requests.Session | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._max_retries = max_retries
        self._backoff_base_s = backoff_base_s
        self._health_timeout_s = health_timeout_s
        self._session = session or requests.Session()

    # --- public API -----------------------------------------------------------

    def health(self) -> HealthStatus:
        # /health itself is unauthenticated (see README) — a green dot
        # while the token is wrong is the exact case that made fix-up 1
        # necessary, so combine the anonymous check with an authenticated
        # no-op probe and expose `token_ok` separately.
        try:
            r = self._session.get(
                f"{self._base_url}/health",
                timeout=self._health_timeout_s,
            )
        except requests.RequestException as e:
            return HealthStatus(
                ok=False, token_ok=True,
                model_name=None, faces_model=None,
                memory_free_gb=None, inbox={}, blackout_active=False,
                blackout_until=None, raw={"error": str(e)},
            )
        if r.status_code != 200:
            return HealthStatus(
                ok=False, token_ok=True,
                model_name=None, faces_model=None,
                memory_free_gb=None, inbox={}, blackout_active=False,
                blackout_until=None, raw={"status": r.status_code, "body": r.text},
            )
        body = r.json()
        return HealthStatus(
            ok=True,
            token_ok=self._probe_token(),
            model_name=_get(body, "vlm", "model") or body.get("model_name"),
            faces_model=_get(body, "faces", "model") or body.get("faces_model"),
            memory_free_gb=_get(body, "memory", "free_gb"),
            inbox=body.get("inbox") or {},
            blackout_active=bool(_get(body, "blackout", "active")),
            blackout_until=_get(body, "blackout", "until"),
            raw=body,
        )

    def _probe_token(self) -> bool:
        """Cheap authenticated call — any 2xx/4xx that isn't 401 counts as
        "the token was accepted". Connection errors during the probe don't
        say anything about the token, so treat as True."""
        try:
            r = self._session.get(
                f"{self._base_url}/batch/inbox/_probe",
                headers=self._headers(),
                timeout=self._health_timeout_s,
            )
        except requests.RequestException:
            return True
        return r.status_code != 401

    def call_endpoint(
        self,
        endpoint: str,
        image: RefImage,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        timeout = timeout if timeout is not None else ENDPOINT_TIMEOUT_S.get(endpoint, 60.0)
        payload_bytes = image.prepared_bytes
        if payload_bytes is None:
            payload_bytes = _read_bytes(image.path)
        files = {"file": (f"{image.ref}.jpg", payload_bytes, "image/jpeg")}
        data = {"ref": image.ref}
        return self._request_json(
            "POST",
            f"/{endpoint}",
            files=files,
            data=data,
            timeout=timeout,
        )

    def upload(
        self,
        job_name: str,
        images: Iterable[RefImage],
        *,
        endpoint_edge: int,
    ) -> BatchUploadResult:
        files: list[tuple[str, tuple[str, bytes, str]]] = []
        for img in images:
            body = img.prepared_bytes
            if body is None:
                body = prepare_jpeg(img.path, endpoint_edge)
            files.append(("files", (f"{img.ref}.jpg", body, "image/jpeg")))
        if not files:
            return BatchUploadResult(accepted=0, rejected=[])
        body = self._request_json(
            "POST",
            f"/batch/upload/{job_name}",
            files=files,
            timeout=UPLOAD_CHUNK_TIMEOUT_S,
        )
        return BatchUploadResult(
            accepted=int(body.get("accepted", 0)),
            rejected=list(body.get("rejected", [])),
        )

    def inbox(self, job_name: str) -> BatchInboxStatus:
        body = self._request_json(
            "GET", f"/batch/inbox/{job_name}", timeout=self._health_timeout_s
        )
        return BatchInboxStatus(
            total=int(body.get("total", 0)),
            pending=int(body.get("pending", 0)),
            with_results=int(body.get("with_results", 0)),
        )

    def start_from_inbox(self, job_name: str, endpoint: str) -> BatchStartResult:
        # `POST /batch/{endpoint}` with from_inbox streams NDJSON for the
        # life of the batch: one header line, then one line per item, then
        # a summary line. The header lands within milliseconds. Reading
        # the whole body would block for hours, so we stream the response,
        # read only the first non-blank line, and close (fix-up 1).
        body = self._stream_first_json(
            method="POST",
            path=f"/batch/{endpoint}",
            json_body={"job_name": job_name, "from_inbox": True},
            timeout=self._health_timeout_s,
        )
        accepted = (
            "job_id" in body
            or bool(body.get("accepted", False))
            or body.get("total") is not None
        )
        return BatchStartResult(
            job_name=job_name,
            endpoint=endpoint,
            accepted=bool(accepted),
            already_running=bool(body.get("already_running", False)),
            raw=body,
        )

    def results_after(self, job_name: str, after: int) -> Iterator[ResultLine]:
        url = f"{self._base_url}/batch/results/{job_name}"
        params = {"after": after}
        headers = self._headers()
        # Stream NDJSON. This can be big — do not buffer the whole body.
        with self._session.get(
            url, params=params, headers=headers, stream=True, timeout=60.0
        ) as r:
            if r.status_code == 404:
                # The service returns 404 for a job that has never been
                # handed over. That's "nothing to collect yet", not an
                # error — swallow quietly (fix-up 1).
                log.debug("results_after: no results file yet for %s", job_name)
                return
            self._raise_for_status(r)
            line_no = after
            for raw_line in r.iter_lines(decode_unicode=True):
                if not raw_line:
                    continue
                line_no += 1
                try:
                    obj = json.loads(raw_line)
                except json.JSONDecodeError as e:
                    log.warning(
                        "results_after: skipping unparseable line %d for %s: %s",
                        line_no, job_name, e,
                    )
                    continue
                # The service's line envelope includes the ref; skip the
                # header line (which has "job_id"/"total" but no "ref")
                # if present. The unattended-batch NDJSON is one line per
                # item and does not emit a header, but be defensive.
                ref = obj.get("ref")
                if ref is None:
                    continue
                yield ResultLine(
                    line_no=line_no,
                    ref=str(ref),
                    ok=bool(obj.get("ok", "result" in obj)),
                    model=obj.get("model"),
                    prompt_version=obj.get("prompt_version"),
                    elapsed_ms=obj.get("elapsed_ms"),
                    result=obj.get("result") or {},
                    error=obj.get("error"),
                    raw=obj,
                )

    def summary(self, job_name: str) -> BatchSummary:
        # The service exposes /batch/results/{job_name}/summary (README).
        body = self._request_json(
            "GET",
            f"/batch/results/{job_name}/summary",
            timeout=self._health_timeout_s,
        )
        return BatchSummary(
            job_name=job_name,
            total=int(body.get("total", 0)),
            done=int(body.get("done", 0)),
            failed=int(body.get("failed", 0)),
            pending=int(body.get("pending", 0)),
            eta_seconds=body.get("eta_seconds"),
            running=bool(body.get("running", False)),
            blackout_active=bool(body.get("blackout_active", False)),
            raw=body,
        )

    def sweep(self, job_name: str, *, done_only: bool = True) -> int:
        params = {"done": "true" if done_only else "false"}
        body = self._request_json(
            "DELETE",
            f"/batch/inbox/{job_name}",
            params=params,
            timeout=self._health_timeout_s,
        )
        return int(body.get("removed", 0))

    def cancel(self, job_name: str) -> bool:
        # Batches started with from_inbox get cancelled by hitting
        # /batch/cancel/{job_id}. The unattended-batch code keys by
        # job_name; try a name-keyed cancel first, then fall back to
        # posting an explicit stop flag if the service exposes it.
        try:
            body = self._request_json(
                "POST",
                f"/batch/cancel/{job_name}",
                timeout=self._health_timeout_s,
            )
            return bool(body.get("cancelled", True))
        except InferenceError:
            return False

    # --- request plumbing -----------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        files: Any = None,
        data: Any = None,
        json_body: Any = None,
        params: Any = None,
        timeout: float,
    ) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        headers = self._headers()
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                r = self._session.request(
                    method,
                    url,
                    headers=headers,
                    files=files,
                    data=data,
                    json=json_body,
                    params=params,
                    timeout=timeout,
                )
            except (requests.ConnectionError, requests.Timeout) as e:
                last_exc = e
                if attempt >= self._max_retries:
                    raise ServiceUnavailable(f"{method} {url}: {e}") from e
                self._sleep(attempt)
                continue
            if r.status_code == 401:
                raise FatalAuthError("401 from inference service: bad token")
            if r.status_code in RETRIABLE_STATUS:
                if attempt >= self._max_retries:
                    raise ServiceUnavailable(
                        f"{method} {url}: HTTP {r.status_code} after {attempt + 1} attempts"
                    )
                self._sleep(attempt)
                continue
            self._raise_for_status(r)
            try:
                return r.json()
            except ValueError as e:
                raise InferenceError(
                    f"{method} {url}: expected JSON, got {r.text[:200]!r}"
                ) from e
        # unreachable
        raise ServiceUnavailable(f"{method} {url}: exhausted retries; last error {last_exc}")

    def _stream_first_json(
        self,
        *,
        method: str,
        path: str,
        json_body: Any = None,
        timeout: float,
    ) -> dict[str, Any]:
        """Open a streaming request, parse the first non-blank NDJSON line,
        close the response. Retries on connection errors / 503; 401 fatal."""
        url = f"{self._base_url}{path}"
        headers = self._headers()
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                with self._session.request(
                    method, url,
                    headers=headers,
                    json=json_body,
                    stream=True,
                    timeout=timeout,
                ) as r:
                    if r.status_code == 401:
                        raise FatalAuthError("401 from inference service: bad token")
                    if r.status_code in RETRIABLE_STATUS:
                        if attempt >= self._max_retries:
                            raise ServiceUnavailable(
                                f"{method} {url}: HTTP {r.status_code} after {attempt + 1} attempts"
                            )
                        # fall through to sleep+retry
                    elif not (200 <= r.status_code < 300):
                        # Best-effort surface a bit of the body for context.
                        try:
                            body_snippet = next(r.iter_lines(decode_unicode=True), "")
                        except Exception:
                            body_snippet = ""
                        raise InferenceError(
                            f"{method} {url}: HTTP {r.status_code}: {body_snippet[:200]}"
                        )
                    else:
                        for raw_line in r.iter_lines(decode_unicode=True):
                            if not raw_line:
                                continue
                            try:
                                return json.loads(raw_line)
                            except json.JSONDecodeError as e:
                                raise InferenceError(
                                    f"{method} {url}: first line not JSON: {raw_line[:200]!r}"
                                ) from e
                        return {}
            except (requests.ConnectionError, requests.Timeout) as e:
                last_exc = e
                if attempt >= self._max_retries:
                    raise ServiceUnavailable(f"{method} {url}: {e}") from e
            self._sleep(attempt)
        raise ServiceUnavailable(
            f"{method} {url}: exhausted retries; last error {last_exc}"
        )

    def _raise_for_status(self, r: requests.Response) -> None:
        if r.status_code == 401:
            raise FatalAuthError("401 from inference service: bad token")
        if 200 <= r.status_code < 300:
            return
        raise InferenceError(
            f"{r.request.method} {r.url}: HTTP {r.status_code}: {r.text[:200]}"
        )

    def _sleep(self, attempt: int) -> None:
        # Exponential with a cap: 0.5, 1.0, 2.0, 4.0, capped at 8s.
        delay = min(self._backoff_base_s * (2 ** attempt), 8.0)
        time.sleep(delay)


def _read_bytes(path: Path) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _get(d: dict[str, Any], *keys: str) -> Any:
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur
