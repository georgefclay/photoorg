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


# --- back working files + the ONE resolver (Phase 6 fix-up 11) ------------


def back_working_name(back_id: int, sha256_hex: str, ext: str) -> str:
    ext = ext.lower().lstrip(".")
    return f"back_{back_id:08d}_{sha256_hex[:8]}.{ext}"


def back_working_path(settings: Settings, back_id: int, sha256_hex: str, ext: str) -> Path:
    return settings.WORKING_DIR / back_working_name(back_id, sha256_hex, ext)


def is_absolute_working_path(stored: str | None) -> bool:
    return bool(stored) and Path(stored).is_absolute()


def resolve_working_path(working_dir: Path | str, stored: str | None) -> Path | None:
    """The ONE place a stored `working_path` (photos or photo_backs) becomes a
    filesystem path.

    Absolute values are returned as-is. A bare or relative value is joined
    onto `working_dir` by its basename only (never a parent traversal).
    This tolerance is a safety net, not the new normal: the DB is expected
    to hold absolute paths, and `check_working_files` rewrites bare rows
    back to absolute. Nothing else may build a working-file path from the
    naming scheme on its own.
    """
    if not stored:
        return None
    p = Path(stored)
    if p.is_absolute():
        return p
    return Path(working_dir) / p.name
