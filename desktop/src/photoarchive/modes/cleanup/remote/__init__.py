"""Remote enhance: provider chosen by config, not by code."""
from __future__ import annotations

import logging

from ....config import Settings
from .base import RemoteError, RemoteJob, RemoteProvider, RemoteUnavailable
from .claid import ClaidProvider
from .null_provider import NullProvider

log = logging.getLogger(__name__)

__all__ = [
    "RemoteError", "RemoteJob", "RemoteProvider", "RemoteUnavailable",
    "ClaidProvider", "NullProvider", "build_provider",
]


def build_provider(settings: Settings) -> RemoteProvider:
    name = (settings.CLEANUP_REMOTE_PROVIDER or "null").strip().lower()
    if name == "claid":
        provider = ClaidProvider(
            api_key=settings.CLAID_API_KEY,
            base_url=settings.CLAID_BASE_URL,
            cost_per_op_usd=settings.CLAID_COST_PER_OP_USD,
        )
        if not provider.available:
            log.info("cleanup: CLEANUP_REMOTE_PROVIDER=claid but %s",
                     provider.unavailable_reason())
        return provider
    return NullProvider()
