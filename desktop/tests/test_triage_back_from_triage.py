"""Fix-up 2: B key ('this is a back') behaviour.

Requires TEST_DATABASE_URL because we insert real rows into photos,
photo_masters, and ingest_pairings.
"""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from photoarchive import db
from photoarchive.config import MasterRoot, Settings
from photoarchive.modes.triage import back_from_triage

from .conftest import DB_AVAILABLE, TEST_DATABASE_URL


pytestmark = pytest.mark.skipif(not DB_AVAILABLE, reason="no TEST_DATABASE_URL")


def _settings(tmp_path: Path, url: str,
              scans_dir: Path, photos_dir: Path) -> Settings:
    s = Settings(
        MASTER_ROOTS=f"scans={scans_dir}|scan;photos={photos_dir}",
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
    scans_dir.mkdir(parents=True, exist_ok=True)
    photos_dir.mkdir(parents=True, exist_ok=True)
    return s


def _reset(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "truncate audit_log, ingest_pairings, ingest_rescans, "
            "ingest_failures, triage_hints, photo_masters, photos, "
            "job_items, job_runs restart identity cascade"
        )


def _init_pool(url: str, settings: Settings) -> None:
    if getattr(db, "_pool", None) is not None:
        db.close_pool()
    db.init_pool(settings)


def _insert_photo(
    url: str, settings: Settings, *, root: str, folder: str, filename: str,
    sha: str, scan_sequence: int | None,
) -> int:
    from photoarchive.modes.ingest.paths import working_path
    with psycopg.connect(url, autocommit=True) as conn:
        pid = conn.execute(
            """
            insert into photos
              (sha256, source_root, source_folder, source_filename,
               mime, width, height, scan_batch, scan_sequence,
               working_path, triage_status, is_deleted, is_scan)
            values (%s, %s, %s, %s, 'image/jpeg', 100, 100, %s, %s,
                    %s, 'untriaged', false, true)
            returning id
            """,
            (sha, root, folder, filename,
             folder if folder else None, scan_sequence, None),
        ).fetchone()[0]
    wp = working_path(settings, pid, sha, "jpg")
    wp.write_bytes(b"stub")
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("update photos set working_path=%s where id=%s",
                     (str(wp), pid))
        conn.execute(
            """
            insert into photo_masters
              (photo_id, master_path, sha256, width, height, mime, is_preferred)
            values (%s, %s, %s, 100, 100, 'image/jpeg', true)
            """,
            (pid, f"D:\\Scanned\\{folder}\\{filename}", sha),
        )
    return pid


def test_b_on_scan_photo_creates_pending_pairing_with_predecessor_front(tmp_path):
    settings = _settings(tmp_path, TEST_DATABASE_URL,
                          tmp_path / "scans", tmp_path / "photos")
    _init_pool(TEST_DATABASE_URL, settings)
    _reset(TEST_DATABASE_URL)

    front = _insert_photo(TEST_DATABASE_URL, settings, root="scans",
                           folder="Batch 00001", filename="IMG_0001.jpg",
                           sha="a" * 64, scan_sequence=1)
    back = _insert_photo(TEST_DATABASE_URL, settings, root="scans",
                          folder="Batch 00001", filename="IMG_0002.jpg",
                          sha="b" * 64, scan_sequence=2)

    result = back_from_triage.propose_back_from_triage(settings, back)
    assert result.front_photo_id == front
    assert result.reason == "ok"
    assert result.pairing_id > 0

    with psycopg.connect(TEST_DATABASE_URL) as conn:
        row = conn.execute(
            """
            select back_photo_id, front_photo_id, back_score, status, details
            from ingest_pairings where id = %s
            """,
            (result.pairing_id,),
        ).fetchone()
        st = conn.execute(
            "select triage_status from photos where id = %s", (back,),
        ).fetchone()[0]

    assert row[0] == back
    assert row[1] == front
    assert row[2] == pytest.approx(1.0)
    assert row[3] == "pending"
    assert row[4]["source"] == "triage"
    assert st == "keep"  # so it doesn't get junked meanwhile

    db.close_pool()


def test_b_first_in_folder_is_orphan(tmp_path):
    settings = _settings(tmp_path, TEST_DATABASE_URL,
                          tmp_path / "scans", tmp_path / "photos")
    _init_pool(TEST_DATABASE_URL, settings)
    _reset(TEST_DATABASE_URL)

    pid = _insert_photo(TEST_DATABASE_URL, settings, root="scans",
                        folder="Batch 00042", filename="IMG_0001.jpg",
                        sha="c" * 64, scan_sequence=1)
    result = back_from_triage.propose_back_from_triage(settings, pid)
    assert result.front_photo_id is None
    assert result.reason == "orphan_no_predecessor"

    db.close_pool()


def test_b_predecessor_is_pending_back_becomes_orphan(tmp_path):
    settings = _settings(tmp_path, TEST_DATABASE_URL,
                          tmp_path / "scans", tmp_path / "photos")
    _init_pool(TEST_DATABASE_URL, settings)
    _reset(TEST_DATABASE_URL)

    a = _insert_photo(TEST_DATABASE_URL, settings, root="scans",
                       folder="Batch 00007", filename="a.jpg",
                       sha="d" * 64, scan_sequence=1)
    b = _insert_photo(TEST_DATABASE_URL, settings, root="scans",
                       folder="Batch 00007", filename="b.jpg",
                       sha="e" * 64, scan_sequence=2)
    c = _insert_photo(TEST_DATABASE_URL, settings, root="scans",
                       folder="Batch 00007", filename="c.jpg",
                       sha="f" * 64, scan_sequence=3)

    # First, mark b as a pending back off of a.
    r1 = back_from_triage.propose_back_from_triage(settings, b)
    assert r1.front_photo_id == a

    # Now B on c — predecessor b is a pending back → orphan.
    r2 = back_from_triage.propose_back_from_triage(settings, c)
    assert r2.front_photo_id is None
    assert r2.reason == "orphan_predecessor_is_back"

    db.close_pool()


def test_b_on_digital_root_raises_not_a_scan(tmp_path):
    settings = _settings(tmp_path, TEST_DATABASE_URL,
                          tmp_path / "scans", tmp_path / "photos")
    _init_pool(TEST_DATABASE_URL, settings)
    _reset(TEST_DATABASE_URL)

    pid = _insert_photo(TEST_DATABASE_URL, settings, root="photos",
                        folder="_2020-06", filename="f001.jpg",
                        sha="1" * 64, scan_sequence=None)
    with pytest.raises(back_from_triage.NotAScan):
        back_from_triage.propose_back_from_triage(settings, pid)

    db.close_pool()


def test_b_twice_on_same_photo_raises_already_proposed(tmp_path):
    settings = _settings(tmp_path, TEST_DATABASE_URL,
                          tmp_path / "scans", tmp_path / "photos")
    _init_pool(TEST_DATABASE_URL, settings)
    _reset(TEST_DATABASE_URL)

    a = _insert_photo(TEST_DATABASE_URL, settings, root="scans",
                       folder="Batch 00099", filename="a.jpg",
                       sha="2" * 64, scan_sequence=1)
    b = _insert_photo(TEST_DATABASE_URL, settings, root="scans",
                       folder="Batch 00099", filename="b.jpg",
                       sha="3" * 64, scan_sequence=2)
    back_from_triage.propose_back_from_triage(settings, b)
    with pytest.raises(back_from_triage.AlreadyProposed):
        back_from_triage.propose_back_from_triage(settings, b)

    db.close_pool()
