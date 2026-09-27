"""Claid.ai provider.

Credit-based pay-as-you-go, roughly $0.03 per operation at volume, and tuned
for e-commerce product photos rather than family portraits — so this is one
implementation of the interface, not the shape of the interface.

Only ever exercised against a mock HTTP server in the tests: `CLAID_BASE_URL`
is configurable precisely so no test can reach the real API, and this phase
spends nothing.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import requests

from .base import RemoteError, RemoteJob, RemoteUnavailable

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 2.0
POLL_TIMEOUT_S = 180.0
CONNECT_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 60.0


class ClaidProvider:
    name = "claid"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.claid.ai",
        cost_per_op_usd: float = 0.03,
        session: Any | None = None,
        poll_interval_s: float = POLL_INTERVAL_S,
        poll_timeout_s: float = POLL_TIMEOUT_S,
    ) -> None:
        self._key = (api_key or "").strip()
        self._base = base_url.rstrip("/")
        self._cost = float(cost_per_op_usd)
        self._session = session or requests.Session()
        self._poll_interval = poll_interval_s
        self._poll_timeout = poll_timeout_s

    # -- interface ------------------------------------------------------

    @property
    def available(self) -> bool:
        return bool(self._key)

    def unavailable_reason(self) -> str | None:
        if self.available:
            return None
        return "CLAID_API_KEY is not set in desktop/.env."

    def cost_estimate(self, n: int = 1) -> float:
        return round(self._cost * max(0, int(n)), 4)

    def submit(self, image_path: Path) -> RemoteJob:
        if not self.available:
            raise RemoteUnavailable(self.unavailable_reason() or "no api key")
        url = f"{self._base}/v1-beta1/image/edit/upload"
        # Restoration-ish operations: no resize, no background work — the
        # geometry is ours, the provider only touches tone and grain.
        operations = {
            "operations": {
                "restorations": {"decompress": "auto", "upscale": "smart_enhance"},
                "adjustments": {"hdr": 30},
            },
            "output": {"format": {"type": "jpeg", "quality": 95}},
        }
        with open(image_path, "rb") as f:
            files = {"file": (image_path.name, f, "image/jpeg")}
            resp = self._session.post(
                url,
                headers={"Authorization": f"Bearer {self._key}"},
                data={"data": _json_dumps(operations)},
                files=files,
                timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
            )
        payload = _payload(resp, url)
        data = payload.get("data") or {}
        job_ref = str(
            data.get("id") or data.get("request_id") or payload.get("id") or ""
        )
        result_url = _result_url(payload)
        if not job_ref and not result_url:
            raise RemoteError(f"Claid returned neither a job id nor a result: {payload}")
        return RemoteJob(
            provider=self.name, job_ref=job_ref or "inline",
            cost_estimate_usd=self.cost_estimate(1),
            details={"result_url": result_url, "raw": data},
        )

    def await_result(self, job: RemoteJob, out_path: Path) -> Path:
        if not self.available:
            raise RemoteUnavailable(self.unavailable_reason() or "no api key")
        result_url = job.details.get("result_url")
        deadline = time.monotonic() + self._poll_timeout
        while not result_url:
            if time.monotonic() > deadline:
                raise RemoteError(f"Claid job {job.job_ref} timed out")
            time.sleep(self._poll_interval)
            url = f"{self._base}/v1-beta1/image/requests/{job.job_ref}"
            resp = self._session.get(
                url, headers={"Authorization": f"Bearer {self._key}"},
                timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
            )
            payload = _payload(resp, url)
            status = str(((payload.get("data") or {}).get("status")
                          or payload.get("status") or "")).lower()
            if status in ("failed", "error"):
                raise RemoteError(f"Claid job {job.job_ref} failed: {payload}")
            result_url = _result_url(payload)

        resp = self._session.get(result_url, timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S))
        if resp.status_code != 200:
            raise RemoteError(
                f"Claid result download returned {resp.status_code} for {result_url}"
            )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(resp.content)
        return out_path


def _payload(resp: Any, url: str) -> dict[str, Any]:
    if resp.status_code == 401:
        raise RemoteUnavailable("Claid rejected CLAID_API_KEY (401).")
    if resp.status_code >= 400:
        raise RemoteError(f"Claid returned {resp.status_code} for {url}: {resp.text[:400]}")
    try:
        return resp.json()
    except Exception as e:
        raise RemoteError(f"Claid returned non-JSON from {url}: {e}") from e


def _result_url(payload: dict[str, Any]) -> str | None:
    data = payload.get("data") or {}
    output = data.get("output") or payload.get("output") or {}
    for key in ("tmp_url", "url", "result_url"):
        v = output.get(key) if isinstance(output, dict) else None
        if v:
            return str(v)
    for key in ("tmp_url", "url", "result_url"):
        v = data.get(key)
        if v:
            return str(v)
    return None


def _json_dumps(v: Any) -> str:
    import json
    return json.dumps(v)
