"""Helpers for Phase 6 tests: settings pointing at a temp dir, DB pool wiring,
and factory functions for photos / masters / backs. Shared across the
writer, runner, merge and clustering DB tests."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import psycopg
import pytest
from PIL import Image

from photoarchive import db as dbmod
from photoarchive.config import Settings

from .conftest import DB_AVAILABLE, TEST_DATABASE_URL


PHASE6_TABLES = (
    "job_cursors",
    "suggestions",
    "faces",
    "photo_backs",
    "ingest_pairings",
    "triage_hints",
    "photo_job_status",
    "audit_log",
    "photo_masters",
    "photos",
    "person_name_variants",
    "people",
    "job_runs",
    "job_items",
    "ingest_failures",
)


def phase6_settings(tmp_path: Path, url: str) -> Settings:
    for sub in ("working", "quarantine", "manual-fix", "thumbs", "thumbs/faces", "masters"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    return Settings(
        MASTER_ROOTS=f"m1={tmp_path / 'masters'}",
        WORKING_DIR=tmp_path / "working",
        QUARANTINE_DIR=tmp_path / "quarantine",
        MANUAL_FIX_DIR=tmp_path / "manual-fix",
        THUMBS_DIR=tmp_path / "thumbs",
        DATABASE_URL=url,
        INFERENCE_URL="http://mock",
        INFERENCE_TOKEN="test-token",
        WEB_API_URL="http://mock",
        WEB_API_TOKEN="test-token",
        FACE_CLUSTER_DIST=0.45,
    )


def truncate_all(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "truncate table " + ", ".join(PHASE6_TABLES) + " restart identity cascade"
        )


@pytest.fixture
def phase6(tmp_path):
    if not DB_AVAILABLE:
        pytest.skip("no TEST_DATABASE_URL")
    settings = phase6_settings(tmp_path, TEST_DATABASE_URL)
    if getattr(dbmod, "_pool", None) is not None:
        dbmod.close_pool()
    dbmod.init_pool(settings)
    truncate_all(TEST_DATABASE_URL)
    # Point config.load() at the same tmp settings by tweaking env so any
    # code path that calls it inside the tests picks up the same values.
    saved = {}
    env = {
        "MASTER_ROOTS": settings.MASTER_ROOTS,
        "WORKING_DIR": str(settings.WORKING_DIR),
        "QUARANTINE_DIR": str(settings.QUARANTINE_DIR),
        "MANUAL_FIX_DIR": str(settings.MANUAL_FIX_DIR),
        "THUMBS_DIR": str(settings.THUMBS_DIR),
        "DATABASE_URL": settings.DATABASE_URL,
        "INFERENCE_URL": settings.INFERENCE_URL,
        "INFERENCE_TOKEN": settings.INFERENCE_TOKEN,
        "WEB_API_URL": settings.WEB_API_URL,
        "WEB_API_TOKEN": settings.WEB_API_TOKEN,
        "FACE_CLUSTER_DIST": str(settings.FACE_CLUSTER_DIST),
    }
    for k, v in env.items():
        saved[k] = os.environ.get(k)
        os.environ[k] = v
    try:
        yield settings
    finally:
        for k, prev in saved.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev
        dbmod.close_pool()


# ---- factory helpers -----------------------------------------------------


def insert_photo(
    conn: psycopg.Connection,
    *,
    source_root: str = "m1",
    source_folder: str = "Batch 0001",
    source_filename: str | None = None,
    is_scan: bool = False,
    triage_status: str = "keep",
    is_deleted: bool = False,
    working_path: str | None = None,
    width: int = 800,
    height: int = 600,
    scan_sequence: int | None = None,
    capture_date_confirmed: bool = False,
) -> int:
    row = conn.execute(
        """
        insert into photos
          (working_path, sha256, mime, width, height, is_scan,
           source_root, source_folder, source_filename,
           scan_sequence, triage_status, is_deleted,
           capture_date_confirmed)
        values (%s, %s, 'image/jpeg', %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s)
        returning id
        """,
        (
            working_path,
            _sha(),
            width, height, is_scan,
            source_root, source_folder,
            source_filename or f"IMG_{_sha()[:6]}.jpg",
            scan_sequence, triage_status, is_deleted,
            capture_date_confirmed,
        ),
    ).fetchone()
    return int(row[0])


def insert_master(
    conn: psycopg.Connection,
    photo_id: int,
    *,
    master_path: str | None = None,
) -> int:
    row = conn.execute(
        """
        insert into photo_masters
          (photo_id, master_path, sha256, mime, is_preferred)
        values (%s, %s, %s, 'image/jpeg', true)
        returning id
        """,
        (photo_id, master_path or f"M:\\{_sha()[:8]}.jpg", _sha()),
    ).fetchone()
    return int(row[0])


def insert_back(
    conn: psycopg.Connection,
    *,
    photo_id: int | None,
    working_path: str | None = None,
    master_path: str | None = None,
    transcribed_text: str | None = None,
) -> int:
    row = conn.execute(
        """
        insert into photo_backs
          (photo_id, master_path, sha256, working_path, transcribed_text)
        values (%s, %s, %s, %s, %s)
        returning id
        """,
        (
            photo_id,
            master_path or f"B:\\{_sha()[:8]}.jpg",
            _sha(),
            working_path,
            transcribed_text,
        ),
    ).fetchone()
    return int(row[0])


def write_test_jpeg(path: Path, w: int = 200, h: int = 150) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (w, h), (128, 64, 32)).save(path, format="JPEG")
    return path


def _sha() -> str:
    return os.urandom(32).hex()
