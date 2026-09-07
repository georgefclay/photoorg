"""Process-wide inference client — one LanInferenceClient per app run.
Modules that need it (Jobs panel, Faces mode, tools) all get the same
instance rather than each rebuilding session state / retry counters."""

from __future__ import annotations

from ..config import load as load_settings
from .lan import LanInferenceClient

_client: LanInferenceClient | None = None


def client() -> LanInferenceClient:
    global _client
    if _client is None:
        settings = load_settings()
        _client = LanInferenceClient(settings.INFERENCE_URL, settings.INFERENCE_TOKEN)
    return _client


def reset() -> None:
    """Test helper; drops the cached client so the next call rebuilds it
    from the current settings."""
    global _client
    _client = None
