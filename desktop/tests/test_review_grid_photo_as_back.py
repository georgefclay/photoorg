"""Phase 2 fix-up 5: photo-as-back proposals must render both thumbs
in the review grid (not "(no thumb)"), and the filmstrip should land
on the front tile — not tile #1."""
from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest
from PIL import Image

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from photoarchive import db
from photoarchive.config import Settings

from .conftest import DB_AVAILABLE, TEST_DATABASE_URL


pytestmark = pytest.mark.skipif(not DB_AVAILABLE, reason="no TEST_DATABASE_URL")


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app  # type: ignore[return-value]


def _settings(tmp_path: Path, url: str) -> Settings:
    s = Settings(
        MASTER_ROOTS=f"scans={tmp_path / 'scans'}|scan",
        WORKING_DIR=tmp_path / "working",
        QUARANTINE_DIR=tmp_path / "quarantine",
        MANUAL_FIX_DIR=tmp_path / "manual-fix",
        THUMBS_DIR=tmp_path / "thumbs",
        DATABASE_URL=url,
        INFERENCE_URL="http://x", INFERENCE_TOKEN="x",
        WEB_API_URL="http://x", WEB_API_TOKEN="x",
    )
    for d in (s.WORKING_DIR, s.QUARANTINE_DIR, s.MANUAL_FIX_DIR, s.THUMBS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    (tmp_path / "scans").mkdir(parents=True, exist_ok=True)
    return s


def _reset(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "truncate audit_log, ingest_pairings, ingest_rescans, "
            "ingest_failures, triage_hints, photo_backs, photo_masters, "
            "photos, job_items, job_runs restart identity cascade"
        )


def _seed_photo_with_thumb(
    url: str, settings: Settings, *, sha: str, folder: str,
    filename: str, seq: int, color: tuple[int, int, int],
) -> int:
    from photoarchive.modes.ingest.paths import working_path
    with psycopg.connect(url, autocommit=True) as conn:
        pid = conn.execute(
            """
            insert into photos
              (sha256, source_root, source_folder, source_filename,
               mime, width, height, scan_batch, scan_sequence,
               working_path, triage_status, is_deleted, is_scan)
            values (%s, 'scans', %s, %s, 'image/jpeg', 100, 150, %s, %s,
                    null, 'keep', false, true)
            returning id
            """,
            (sha, folder, filename, folder, seq),
        ).fetchone()[0]
    wp = working_path(settings, pid, sha, "jpg")
    Image.new("RGB", (100, 150), color).save(wp, "JPEG")
    tp = settings.THUMBS_DIR / f"{pid:08d}.jpg"
    Image.new("RGB", (80, 120), color).save(tp, "JPEG")
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("update photos set working_path=%s where id=%s",
                     (str(wp), pid))
        conn.execute(
            """
            insert into photo_masters
              (photo_id, master_path, sha256, width, height, mime, is_preferred)
            values (%s, %s, %s, 100, 150, 'image/jpeg', true)
            """,
            (pid, f"D:\\Scanned\\{folder}\\{filename}", sha),
        )
    return pid


def test_photo_as_back_pane_shows_real_pixmap(tmp_path):
    _app()
    settings = _settings(tmp_path, TEST_DATABASE_URL)
    if getattr(db, "_pool", None) is not None:
        db.close_pool()
    db.init_pool(settings)
    _reset(TEST_DATABASE_URL)

    front = _seed_photo_with_thumb(
        TEST_DATABASE_URL, settings, sha="a" * 64,
        folder="Batch 00001", filename="a.jpg", seq=1,
        color=(30, 200, 30),
    )
    back = _seed_photo_with_thumb(
        TEST_DATABASE_URL, settings, sha="b" * 64,
        folder="Batch 00001", filename="b.jpg", seq=2,
        color=(200, 30, 30),
    )
    # Give the surrounding sequence some neighbours for the filmstrip.
    for i, sha in enumerate(("c", "d"), start=3):
        _seed_photo_with_thumb(
            TEST_DATABASE_URL, settings, sha=sha * 64,
            folder="Batch 00001", filename=f"n{i}.jpg", seq=i,
            color=(30, 30, 200),
        )
    # Insert a photo-as-back proposal. The stored staging path is
    # deliberately something that DOES NOT EXIST — this reproduces the
    # bug: fix-up 5 must resolve the thumb from THUMBS_DIR/{back:08d}.jpg
    # regardless of what the staging column says.
    bogus = str(settings.WORKING_DIR / "_staging" / "does-not-exist.jpg")
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute(
            """
            insert into ingest_pairings
              (front_photo_id, back_master_path, back_sha256,
               back_source_folder, back_source_filename, back_scan_sequence,
               back_score, staging_working_path, staging_thumb_path,
               back_photo_id, back_aspect_mismatch)
            values (%s, %s, %s, %s, %s, %s, 0.9, %s, %s, %s, false)
            """,
            (front, f"D:\\Scanned\\Batch 00001\\b.jpg", "b" * 64,
             "Batch 00001", "b.jpg", 2,
             bogus, bogus, back),
        )

    from photoarchive.modes.ingest.review_grid import ReviewGridDialog
    dlg = ReviewGridDialog(settings)
    dlg.resize(1400, 900)
    dlg.show()

    # Force a layout pass so the fitted pixmaps are set.
    QApplication.processEvents()

    # Both panes have a non-null pixmap (not "(no thumb)").
    assert dlg._left.pixmap() is not None and not dlg._left.pixmap().isNull()
    assert dlg._right.pixmap() is not None and not dlg._right.pixmap().isNull()

    # Filmstrip's initial selection is the FRONT tile, not tile #1.
    fs = dlg._filmstrip
    cells = [c for c, _ in fs._cells]
    front_idx = next(i for i, c in enumerate(cells) if c.is_current_front)
    assert fs.selected_index() == front_idx

    dlg.close()
    db.close_pool()
