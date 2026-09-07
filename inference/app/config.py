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

    vlm_max_tokens: int = 768
    vlm_temperature: float = 0.0
    request_timeout_s: int = 120

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
