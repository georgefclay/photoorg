"""The default provider: does nothing, costs nothing, says why.

`CLEANUP_REMOTE_PROVIDER=null` (the default) means the E key is disabled with
a tooltip rather than absent, so the affordance is discoverable when George
does get a key.
"""
from __future__ import annotations

from pathlib import Path

from .base import RemoteJob, RemoteUnavailable


class NullProvider:
    name = "null"

    def __init__(self, *, reason: str | None = None) -> None:
        self._reason = reason or (
            "Remote enhance is off. Set CLEANUP_REMOTE_PROVIDER=claid and "
            "CLAID_API_KEY in desktop/.env to enable it."
        )

    @property
    def available(self) -> bool:
        return False

    def unavailable_reason(self) -> str | None:
        return self._reason

    def cost_estimate(self, n: int = 1) -> float:
        return 0.0

    def submit(self, image_path: Path) -> RemoteJob:
        raise RemoteUnavailable(self._reason)

    def await_result(self, job: RemoteJob, out_path: Path) -> Path:
        raise RemoteUnavailable(self._reason)
