"""Provider interface for remote enhance.

The provider is chosen by config (`CLEANUP_REMOTE_PROVIDER`), never by code,
so swapping Claid for something tuned to family portraits rather than
e-commerce product shots touches nothing that calls this.

The returned image is always a **new proposal** through the same review
queue. Nothing a provider sends back is auto-accepted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


class RemoteError(RuntimeError):
    pass


class RemoteUnavailable(RemoteError):
    """No provider configured, or no API key. The E key is disabled."""


@dataclass
class RemoteJob:
    provider: str
    job_ref: str
    cost_estimate_usd: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class RemoteProvider(Protocol):
    name: str

    @property
    def available(self) -> bool:
        """False when the provider cannot run — no key, not configured."""

    def unavailable_reason(self) -> str | None:
        """One line for the disabled E key's tooltip."""

    def cost_estimate(self, n: int = 1) -> float:
        """US dollars for `n` operations."""

    def submit(self, image_path: Path) -> RemoteJob:
        ...

    def await_result(self, job: RemoteJob, out_path: Path) -> Path:
        """Block until the job finishes and write the image to `out_path`."""
