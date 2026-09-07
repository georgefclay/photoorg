"""Settings, read from inference/.env. Everything tunable is config, not code."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

SERVICE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.environ.get("INFERENCE_ENV_FILE", str(SERVICE_DIR / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    inference_host: str = "0.0.0.0"
    inference_port: int = 8500
    inference_token: str = "CHANGEME"
    vlm_model: str = "mlx-community/Qwen3-VL-8B-Instruct-4bit"
    face_model: str = "buffalo_l"
    max_image_edge: int = 1536
    log_dir: Path = SERVICE_DIR / "logs"

    # Empty disables the `path` request variant entirely (400 on any path request).
    shared_root: str = ""

    # InsightFace detector input size. Larger finds smaller faces in group scans.
    face_det_size: int = 1024

    # Per-endpoint longest-edge overrides, "endpoint=px;endpoint=px". Anything not
    # named here uses max_image_edge. Image tokens dominate cost, so this is the
    # throughput lever.
    max_image_edge_overrides: str = "classify=1024;describe=1024;estimate-date=1024"

    vlm_max_tokens: int = 768
    vlm_temperature: float = 0.0
    request_timeout_s: int = 120

    # Windows when unattended batches stand down, "Tue 04:30-07:30;Fri 04:30-07:30"
    # in local time. Empty means never. Interactive requests are never affected.
    batch_blackout: str = ""

    # Batches also pause while the machine is this close to full.
    batch_min_free_gb: float = 1.0

    # How long a paused batch waits before looking again.
    batch_pause_poll_s: int = 60

    @property
    def edge_overrides(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for clause in self.max_image_edge_overrides.split(";"):
            clause = clause.strip()
            if not clause:
                continue
            name, _, value = clause.partition("=")
            try:
                out[name.strip()] = int(value)
            except ValueError:
                continue
        return out

    def edge_for(self, endpoint: str | None) -> int:
        """Longest edge for one endpoint's images."""
        if endpoint is None:
            return self.max_image_edge
        return self.edge_overrides.get(endpoint, self.max_image_edge)

    @property
    def inbox_dir(self) -> Path | None:
        root = self.shared_root_path
        return None if root is None else root / "inbox"

    @property
    def shared_root_path(self) -> Path | None:
        if not self.shared_root.strip():
            return None
        return Path(self.shared_root).expanduser().resolve()

    @property
    def batch_dir(self) -> Path:
        return self.log_dir / "batches"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.log_dir.mkdir(parents=True, exist_ok=True)
    s.batch_dir.mkdir(parents=True, exist_ok=True)
    return s
