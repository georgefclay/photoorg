from __future__ import annotations

from pathlib import Path

from ...config import Settings


def working_name(photo_id: int, sha256_hex: str, ext: str) -> str:
    ext = ext.lower().lstrip(".")
    return f"{photo_id:08d}_{sha256_hex[:8]}.{ext}"


def working_path(settings: Settings, photo_id: int, sha256_hex: str, ext: str) -> Path:
    return settings.WORKING_DIR / working_name(photo_id, sha256_hex, ext)


def thumb_path(settings: Settings, photo_id: int) -> Path:
    return settings.THUMBS_DIR / f"{photo_id:08d}.jpg"


def staging_working_path(settings: Settings, sha256_hex: str, ext: str) -> Path:
    ext = ext.lower().lstrip(".")
    return settings.WORKING_DIR / "_staging" / f"{sha256_hex}.{ext}"


def staging_thumb_path(settings: Settings, sha256_hex: str) -> Path:
    return settings.THUMBS_DIR / "_staging" / f"{sha256_hex}.jpg"


def ensure_dirs(settings: Settings) -> None:
    settings.WORKING_DIR.mkdir(parents=True, exist_ok=True)
    (settings.WORKING_DIR / "_staging").mkdir(parents=True, exist_ok=True)
    settings.THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    (settings.THUMBS_DIR / "_staging").mkdir(parents=True, exist_ok=True)
    settings.QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
    settings.MANUAL_FIX_DIR.mkdir(parents=True, exist_ok=True)
