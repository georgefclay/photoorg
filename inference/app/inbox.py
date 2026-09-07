"""The mini holds the work as well as doing it.

George's laptop cannot stay on for a multi-day job, so images are uploaded once
into SHARED_ROOT/inbox/{job_name}/ and the mini works through them on its own.
Inputs stay until their results are collected and swept.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import HTTPException

from .config import get_settings

# Job names and refs both become path segments, so they are strict.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp", ".gif"}


def require_inbox_root() -> Path:
    root = get_settings().inbox_dir
    if root is None:
        raise HTTPException(
            status_code=400,
            detail="SHARED_ROOT is not configured; the inbox needs somewhere to live",
        )
    root.mkdir(parents=True, exist_ok=True)
    return root


def check_job_name(job_name: str) -> str:
    if not NAME_RE.match(job_name or ""):
        raise HTTPException(
            status_code=400,
            detail="job_name must be 1-64 chars of letters, digits, _ or -",
        )
    return job_name


def check_ref(ref: str) -> str:
    if not REF_RE.match(ref or "") or ".." in ref:
        raise HTTPException(
            status_code=400,
            detail="ref must be 1-128 chars of letters, digits, dot, _ or -",
        )
    return ref


def job_dir(job_name: str, create: bool = False) -> Path:
    root = require_inbox_root()
    path = root / check_job_name(job_name)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def store(job_name: str, ref: str, data: bytes, filename: str | None = None) -> Path:
    """Write one upload. Re-uploading the same ref overwrites it."""
    check_ref(ref)
    suffix = ".jpg"
    if filename:
        candidate = Path(filename).suffix.lower()
        if candidate in IMAGE_SUFFIXES:
            suffix = candidate

    directory = job_dir(job_name, create=True)
    for existing in directory.glob(f"{ref}.*"):
        # A ref has exactly one file; a re-upload in another format replaces it.
        existing.unlink()
    path = directory / f"{ref}{suffix}"
    path.write_bytes(data)
    return path


def paths(job_name: str) -> dict[str, Path]:
    """ref -> file, for everything currently held for this job."""
    directory = job_dir(job_name)
    if not directory.is_dir():
        return {}
    found: dict[str, Path] = {}
    for entry in sorted(directory.iterdir()):
        if entry.is_file() and entry.suffix.lower() in IMAGE_SUFFIXES:
            found[entry.stem] = entry
    return found


def refs(job_name: str) -> list[str]:
    return sorted(paths(job_name))


def total_bytes(job_name: str) -> int:
    return sum(p.stat().st_size for p in paths(job_name).values())


def delete(job_name: str, ref: str) -> bool:
    check_ref(ref)
    removed = False
    for path in job_dir(job_name).glob(f"{ref}.*"):
        if path.is_file():
            path.unlink()
            removed = True
    return removed


def sweep(job_name: str, done_refs: set[str]) -> list[str]:
    """Drop inputs whose results are already in. Never touches anything pending."""
    removed = []
    for ref, path in paths(job_name).items():
        if ref in done_refs:
            path.unlink()
            removed.append(ref)
    return removed
