"""Phase 2 fix-up 6: accept a photo-as-back proposal end to end.

Three scenarios:
  1. Happy path: photo lives at photos.working_path, files move into
     the back working name, DB is consistent.
  2. Photo in quarantine: photos.working_path is null, quarantine_path
     is set — accept must resolve to the quarantine copy.
  3. Source file missing: accept must NOT touch the DB and must raise
     SourceFileMissing (never a raw WinError). The needs_file_repair
     set stays empty because nothing committed.
"""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from PIL import Image

from photoarchive import db
from photoarchive.config import Settings
from photoarchive.modes.ingest import decisions

from .conftest import DB_AVAILABLE, TEST_DATABASE_URL


pytestmark = pytest.mark.skipif(not DB_AVAILABLE, reason="no TEST_DATABASE_URL")


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
    # Reset the module-level repair set so tests don't leak state.
    decisions._NEEDS_FILE_REPAIR.clear()


def _init_pool(url: str, settings: Settings) -> None:
    if getattr(db, "_pool", None) is not None:
        db.close_pool()
    db.init_pool(settings)


def _insert_scan_photo(
    url: str, settings: Settings, *, sha: str, folder: str,
    filename: str, scan_sequence: int, working_dir: Path | None = None,
) -> tuple[int, Path]:
    """Insert a scan photo with a real working file. Returns
    (photo_id, working_path)."""
    from photoarchive.modes.ingest.paths import working_path
    with psycopg.connect(url, autocommit=True) as conn:
        pid = conn.execute(
            """
            insert into photos
              (sha256, source_root, source_folder, source_filename,
               mime, width, height, scan_batch, scan_sequence,
               working_path, triage_status, is_deleted, is_scan)
            values (%s, 'scans', %s, %s, 'image/jpeg', 200, 300, %s, %s,
                    null, 'keep', false, true)
            returning id
            """,
            (sha, folder, filename, folder, scan_sequence),
        ).fetchone()[0]
    wp = (working_dir or settings.WORKING_DIR) / working_path(
        settings, pid, sha, "jpg",
    ).name
    wp.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (200, 300), (240, 240, 240)).save(wp, "JPEG")
    # Thumb too — makes the accept test cover the thumb move path.
    tp = settings.THUMBS_DIR / f"{pid:08d}.jpg"
    Image.new("RGB", (100, 150), (240, 240, 240)).save(tp, "JPEG")
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "update photos set working_path = %s where id = %s",
            (str(wp), pid),
        )
        conn.execute(
            """
            insert into photo_masters
              (photo_id, master_path, sha256, width, height, mime, is_preferred)
            values (%s, %s, %s, 200, 300, 'image/jpeg', true)
            """,
            (pid, f"D:\\Scanned\\{folder}\\{filename}", sha),
        )
    return pid, wp


def _insert_pending_photo_as_back(
    url: str, *, back_photo_id: int, front_photo_id: int | None,
    back_sha: str, back_master_path: str, folder: str, filename: str,
    scan_sequence: int, staging_working: str, staging_thumb: str,
) -> int:
    with psycopg.connect(url, autocommit=True) as conn:
        pair_id = conn.execute(
            """
            insert into ingest_pairings
              (front_photo_id, back_master_path, back_sha256,
               back_source_folder, back_source_filename, back_scan_sequence,
               back_score, staging_working_path, staging_thumb_path,
               back_photo_id, back_aspect_mismatch)
            values (%s, %s, %s, %s, %s, %s, 0.9, %s, %s, %s, false)
            returning id
            """,
            (front_photo_id, back_master_path, back_sha,
             folder, filename, scan_sequence,
             staging_working, staging_thumb, back_photo_id),
        ).fetchone()[0]
    return int(pair_id)


def test_accept_photo_as_back_happy_path(tmp_path):
    settings = _settings(tmp_path, TEST_DATABASE_URL)
    _init_pool(TEST_DATABASE_URL, settings)
    _reset(TEST_DATABASE_URL)

    front_id, _ = _insert_scan_photo(
        TEST_DATABASE_URL, settings, sha="a" * 64,
        folder="Batch 00001", filename="a.jpg", scan_sequence=1,
    )
    back_id, back_wp = _insert_scan_photo(
        TEST_DATABASE_URL, settings, sha="b" * 64,
        folder="Batch 00001", filename="b.jpg", scan_sequence=2,
    )
    pair_id = _insert_pending_photo_as_back(
        TEST_DATABASE_URL,
        back_photo_id=back_id, front_photo_id=front_id,
        back_sha="b" * 64,
        back_master_path=f"D:\\Scanned\\Batch 00001\\b.jpg",
        folder="Batch 00001", filename="b.jpg", scan_sequence=2,
        staging_working=str(back_wp),  # rebuild-style: points at photo's file
        staging_thumb=str(settings.THUMBS_DIR / f"{back_id:08d}.jpg"),
    )

    # Accept.
    decisions.accept_pairing(settings, pair_id)

    # Pairing status updated.
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        st = conn.execute(
            "select status from ingest_pairings where id = %s", (pair_id,),
        ).fetchone()[0]
        pb = conn.execute(
            "select id, photo_id, working_path from photo_backs "
            "where sha256 = %s",
            ("b" * 64,),
        ).fetchone()
        demoted = conn.execute(
            "select is_deleted, physical_ref_note, working_path, "
            "quarantine_path from photos where id = %s",
            (back_id,),
        ).fetchone()
    assert st == "accepted"
    assert pb is not None
    pb_id, pb_photo_id, pb_wp = pb
    assert pb_photo_id == front_id
    assert Path(pb_wp).exists()
    # File moved from photo.working_path to back working name.
    assert not back_wp.exists() or back_wp == Path(pb_wp)
    # Photo is demoted and no longer has a working/quarantine reference.
    assert demoted[0] is True
    assert "back of photo" in demoted[1]
    assert demoted[2] is None
    assert demoted[3] is None

    # Repair set is empty — this was a clean move.
    assert decisions.needs_file_repair_count() == 0

    db.close_pool()


def test_accept_photo_as_back_when_photo_is_quarantined(tmp_path):
    """The photo lives in quarantine (Triage junked it before George
    got to the review grid). Accept must resolve to quarantine_path."""
    settings = _settings(tmp_path, TEST_DATABASE_URL)
    _init_pool(TEST_DATABASE_URL, settings)
    _reset(TEST_DATABASE_URL)

    front_id, _ = _insert_scan_photo(
        TEST_DATABASE_URL, settings, sha="c" * 64,
        folder="Batch 00002", filename="a.jpg", scan_sequence=1,
    )
    back_id, back_wp = _insert_scan_photo(
        TEST_DATABASE_URL, settings, sha="d" * 64,
        folder="Batch 00002", filename="b.jpg", scan_sequence=2,
    )
    # Move the photo file to quarantine and update the DB.
    qp = settings.QUARANTINE_DIR / back_wp.name
    back_wp.rename(qp)
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute(
            "update photos set working_path = null, "
            "quarantine_path = %s, is_deleted = true, "
            "triage_status = 'junk' where id = %s",
            (str(qp), back_id),
        )

    pair_id = _insert_pending_photo_as_back(
        TEST_DATABASE_URL,
        back_photo_id=back_id, front_photo_id=front_id,
        back_sha="d" * 64,
        back_master_path=f"D:\\Scanned\\Batch 00002\\b.jpg",
        folder="Batch 00002", filename="b.jpg", scan_sequence=2,
        # Rebuild's stored path is now stale, but the resolver ignores
        # it in favour of the photo's current quarantine_path.
        staging_working=str(back_wp),
        staging_thumb=str(settings.THUMBS_DIR / f"{back_id:08d}.jpg"),
    )

    decisions.accept_pairing(settings, pair_id)

    with psycopg.connect(TEST_DATABASE_URL) as conn:
        st = conn.execute(
            "select status from ingest_pairings where id = %s", (pair_id,),
        ).fetchone()[0]
        pb = conn.execute(
            "select working_path from photo_backs where sha256 = %s",
            ("d" * 64,),
        ).fetchone()
    assert st == "accepted"
    assert pb is not None
    assert Path(pb[0]).exists()
    # File left quarantine.
    assert not qp.exists()
    assert decisions.needs_file_repair_count() == 0

    db.close_pool()


def test_accept_photo_as_back_when_file_missing_raises_and_leaves_db_untouched(
    tmp_path,
):
    settings = _settings(tmp_path, TEST_DATABASE_URL)
    _init_pool(TEST_DATABASE_URL, settings)
    _reset(TEST_DATABASE_URL)

    front_id, _ = _insert_scan_photo(
        TEST_DATABASE_URL, settings, sha="e" * 64,
        folder="Batch 00003", filename="a.jpg", scan_sequence=1,
    )
    back_id, back_wp = _insert_scan_photo(
        TEST_DATABASE_URL, settings, sha="f" * 64,
        folder="Batch 00003", filename="b.jpg", scan_sequence=2,
    )
    # Simulate the WinError 2 case: the file the DB thinks is at
    # working_path is gone.
    back_wp.unlink()

    pair_id = _insert_pending_photo_as_back(
        TEST_DATABASE_URL,
        back_photo_id=back_id, front_photo_id=front_id,
        back_sha="f" * 64,
        back_master_path=f"D:\\Scanned\\Batch 00003\\b.jpg",
        folder="Batch 00003", filename="b.jpg", scan_sequence=2,
        staging_working=str(back_wp),
        staging_thumb=str(settings.THUMBS_DIR / f"{back_id:08d}.jpg"),
    )

    with pytest.raises(decisions.SourceFileMissing) as excinfo:
        decisions.accept_pairing(settings, pair_id)
    assert excinfo.value.pairing_id == pair_id
    # Never a raw OSError leaking through.
    assert not isinstance(excinfo.value, OSError)

    # DB unchanged — pairing still pending, no photo_backs row, photo
    # still active.
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        st = conn.execute(
            "select status from ingest_pairings where id = %s", (pair_id,),
        ).fetchone()[0]
        pb = conn.execute(
            "select count(*) from photo_backs where sha256 = %s",
            ("f" * 64,),
        ).fetchone()[0]
        photo_active = conn.execute(
            "select is_deleted from photos where id = %s", (back_id,),
        ).fetchone()[0]
    assert st == "pending"
    assert pb == 0
    assert photo_active is False
    # Nothing committed → nothing to repair.
    assert decisions.needs_file_repair_count() == 0

    db.close_pool()


def test_pairing_integrity_reports_missing_photo_back_file(tmp_path):
    """After accept, if the working file is deleted from disk by hand,
    the integrity tool should flag it and populate needs_file_repair."""
    settings = _settings(tmp_path, TEST_DATABASE_URL)
    _init_pool(TEST_DATABASE_URL, settings)
    _reset(TEST_DATABASE_URL)

    front_id, _ = _insert_scan_photo(
        TEST_DATABASE_URL, settings, sha="1" * 64,
        folder="Batch 00004", filename="a.jpg", scan_sequence=1,
    )
    back_id, _ = _insert_scan_photo(
        TEST_DATABASE_URL, settings, sha="2" * 64,
        folder="Batch 00004", filename="b.jpg", scan_sequence=2,
    )
    pair_id = _insert_pending_photo_as_back(
        TEST_DATABASE_URL,
        back_photo_id=back_id, front_photo_id=front_id,
        back_sha="2" * 64,
        back_master_path=f"D:\\Scanned\\Batch 00004\\b.jpg",
        folder="Batch 00004", filename="b.jpg", scan_sequence=2,
        staging_working=str(
            settings.WORKING_DIR / f"{back_id:08d}_22222222.jpg"
        ),
        staging_thumb=str(settings.THUMBS_DIR / f"{back_id:08d}.jpg"),
    )
    decisions.accept_pairing(settings, pair_id)

    # Delete the destination file by hand — simulates a post-accept
    # cleanup or the file-move failure we protect against.
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        pb_wp = conn.execute(
            "select working_path from photo_backs where sha256 = %s",
            ("2" * 64,),
        ).fetchone()[0]
    Path(pb_wp).unlink()

    from photoarchive.tools.pairing_integrity import check_all
    summary = check_all(settings)
    assert any(
        r["issue"] == "photo_backs_file_missing"
        and r["pairing_id"] == pair_id
        for r in summary["recent_issues"]
    )
    assert pair_id in summary["needs_file_repair_ids"]

    db.close_pool()
