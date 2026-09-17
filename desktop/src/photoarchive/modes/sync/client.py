"""HTTP client for the web /sync/* endpoints.

Small, focused wrapper around requests. All routes are idempotent
server-side so a socket hiccup mid-batch is safe to retry as-is. The
Push and Pull workflows in `service.py` layer per-item state on top.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable

import requests

log = logging.getLogger(__name__)


class WebSyncError(Exception):
    pass


class WebSyncClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        session: requests.Session | None = None,
        timeout_s: float = 60.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._session = session or requests.Session()
        self._timeout = timeout_s

    def _hdr(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self._token}"}
        if extra:
            h.update(extra)
        return h

    # --- Photos --------------------------------------------------------

    def push_photos(self, photos: list[dict[str, Any]]) -> dict:
        r = self._session.post(
            f"{self._base}/sync/photos",
            json={"photos": photos},
            headers=self._hdr(),
            timeout=self._timeout,
        )
        return _decode(r, "photos")

    def push_photo_file(self, photo_id: int, path: Path) -> dict:
        with open(path, "rb") as f:
            r = self._session.put(
                f"{self._base}/sync/photos/{photo_id}/file",
                files={"file": (path.name, f, "application/octet-stream")},
                headers=self._hdr(),
                timeout=self._timeout * 4,
            )
        return _decode(r, f"photo file {photo_id}")

    def push_back_file(self, back_id: int, path: Path) -> dict:
        with open(path, "rb") as f:
            r = self._session.put(
                f"{self._base}/sync/photo_backs/{back_id}/file",
                files={"file": (path.name, f, "application/octet-stream")},
                headers=self._hdr(),
                timeout=self._timeout * 4,
            )
        return _decode(r, f"back file {back_id}")

    def push_face_crop(self, face_id: int, path: Path) -> dict:
        with open(path, "rb") as f:
            r = self._session.put(
                f"{self._base}/sync/faces/{face_id}/crop",
                files={"file": (path.name, f, "application/octet-stream")},
                headers=self._hdr(),
                timeout=self._timeout * 2,
            )
        return _decode(r, f"face crop {face_id}")

    # --- Batched metadata upserts --------------------------------------

    def push_batch(self, name: str, items: list[dict[str, Any]]) -> dict:
        r = self._session.post(
            f"{self._base}/sync/{name}",
            json={"items": items},
            headers=self._hdr(),
            timeout=self._timeout,
        )
        return _decode(r, name)

    # --- Groups + confirmed + contributions pulls ----------------------

    def pull_groups(self, since_iso: str | None) -> dict:
        params = {"since": since_iso} if since_iso else {}
        r = self._session.get(
            f"{self._base}/sync/pull/groups",
            headers=self._hdr(), params=params, timeout=self._timeout,
        )
        return _decode(r, "pull/groups")

    def pull_confirmed(self, since_iso: str | None) -> dict:
        params = {"since": since_iso} if since_iso else {}
        r = self._session.get(
            f"{self._base}/sync/pull/confirmed",
            headers=self._hdr(), params=params, timeout=self._timeout,
        )
        return _decode(r, "pull/confirmed")

    def pull_web_origin(self, since_iso: str | None) -> dict:
        params = {"since": since_iso} if since_iso else {}
        r = self._session.get(
            f"{self._base}/sync/pull/web_origin",
            headers=self._hdr(), params=params, timeout=self._timeout,
        )
        return _decode(r, "pull/web_origin")

    def pull_contributions(self) -> dict:
        r = self._session.get(
            f"{self._base}/sync/pull/contributions",
            headers=self._hdr(),
            params={"status": "approved", "pulled": "false"},
            timeout=self._timeout,
        )
        return _decode(r, "pull/contributions")

    def pull_contribution_file(self, contribution_id: int, file_id: int, dest: Path) -> None:
        r = self._session.get(
            f"{self._base}/sync/pull/contributions/{contribution_id}/files/{file_id}",
            headers=self._hdr(), timeout=self._timeout * 4, stream=True,
        )
        if r.status_code != 200:
            raise WebSyncError(f"pull contribution file {contribution_id}/{file_id}: HTTP {r.status_code} — {r.text[:200]}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)

    def mark_contribution_pulled(self, contribution_id: int) -> dict:
        r = self._session.post(
            f"{self._base}/sync/pull/contributions/{contribution_id}/pulled",
            headers=self._hdr(), json={}, timeout=self._timeout,
        )
        return _decode(r, f"mark contribution {contribution_id} pulled")

    def status(self) -> dict:
        r = self._session.get(f"{self._base}/sync/status", headers=self._hdr(), timeout=self._timeout)
        return _decode(r, "status")


def _decode(r: requests.Response, what: str) -> dict:
    if r.status_code >= 400:
        raise WebSyncError(f"{what}: HTTP {r.status_code} — {r.text[:400]}")
    try:
        return r.json()
    except Exception as e:
        raise WebSyncError(f"{what}: bad JSON — {e}") from e
