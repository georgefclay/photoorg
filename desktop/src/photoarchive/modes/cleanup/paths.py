"""Where cleanup puts things.

The standard working name and the one stored-path resolver still live in
`modes/ingest/paths.py` — nothing here builds a working path from the naming
scheme itself (CLAUDE.md, fix-up 11). These are the *new* locations cleanup
introduces: the kept-forever version archive, the on-demand full-resolution
render, and the preview the review queue reads.
"""
from __future__ import annotations

from pathlib import Path

from ...config import Settings

VERSIONS_SUBDIR = "_versions"
PREVIEW_SUBDIR = "_preview"
REPORT_SUBDIR = "_report"


def ext_for(mime: str | None, fallback: str = "jpg") -> str:
    m = (mime or "").lower()
    if m == "image/tiff":
        return "tif"
    if m == "image/png":
        return "png"
    if m == "image/jpeg":
        return "jpg"
    return fallback


def versions_dir(settings: Settings) -> Path:
    return settings.WORKING_DIR / VERSIONS_SUBDIR


def version_path(settings: Settings, photo_id: int, file_version: int, ext: str) -> Path:
    """Where the working copy of `file_version` is kept, forever. Invariant 1:
    cleanup never overwrites — the previous bytes move here."""
    return versions_dir(settings) / f"{photo_id:08d}_v{file_version}.{ext.lstrip('.')}"


def derived_path(settings: Settings, photo_id: int, file_version: int, ext: str) -> Path:
    """The full-resolution render of the proposal, cut on demand."""
    return settings.CLEANUP_DIR / f"{photo_id:08d}_v{file_version}.{ext.lstrip('.')}"


def preview_path(settings: Settings, photo_id: int, proposal_id: int) -> Path:
    return (settings.CLEANUP_DIR / PREVIEW_SUBDIR
            / f"{photo_id:08d}_p{proposal_id}.jpg")


def region_preview_path(
    settings: Settings, photo_id: int, proposal_id: int, index: int,
) -> Path:
    return (settings.CLEANUP_DIR / PREVIEW_SUBDIR
            / f"{photo_id:08d}_p{proposal_id}_r{index}.jpg")


def remote_path(settings: Settings, photo_id: int, job_ref: str, ext: str) -> Path:
    safe = "".join(ch for ch in job_ref if ch.isalnum() or ch in "-_")[:40]
    return (settings.CLEANUP_DIR / "_remote"
            / f"{photo_id:08d}_{safe or 'job'}.{ext.lstrip('.')}")


def report_dir(settings: Settings, stamp: str) -> Path:
    return settings.CLEANUP_DIR / REPORT_SUBDIR / stamp


def manual_fix_path(settings: Settings, photo_id: int, sha256: str, ext: str) -> Path:
    return (settings.MANUAL_FIX_DIR
            / f"{photo_id:08d}_{sha256[:8]}.{ext.lstrip('.')}")


def ensure_dirs(settings: Settings) -> None:
    for d in (
        settings.CLEANUP_DIR,
        settings.CLEANUP_DIR / PREVIEW_SUBDIR,
        settings.CLEANUP_DIR / REPORT_SUBDIR,
        settings.CLEANUP_DIR / "_remote",
        versions_dir(settings),
        settings.MANUAL_FIX_DIR,
    ):
        d.mkdir(parents=True, exist_ok=True)
