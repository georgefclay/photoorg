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


def test_b_on_digital_root_returns_not_a_scan(tmp_path):
    settings = _settings(tmp_path, TEST_DATABASE_URL,
                          tmp_path / "scans", tmp_path / "photos")
    _init_pool(TEST_DATABASE_URL, settings)
    _reset(TEST_DATABASE_URL)

    pid = _insert_photo(TEST_DATABASE_URL, settings, root="photos",
                        folder="_2020-06", filename="f001.jpg",
                        sha="1" * 64, scan_sequence=None)
    r = back_from_triage.propose_back_from_triage(settings, pid)
    assert r.outcome == back_from_triage.OUTCOME_NOT_A_SCAN
    assert r.is_refusal
    assert r.pairing_id is None
    # Photo state must be unchanged — B on a digital root does nothing.
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        st = conn.execute(
            "select triage_status from photos where id = %s", (pid,),
        ).fetchone()[0]
    assert st == "untriaged"

    db.close_pool()


def test_b_on_pending_returns_already_pending_with_front_label(tmp_path):
    """Fix-up 3 branch: existing pending row → status bar note, advance."""
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
    first = back_from_triage.propose_back_from_triage(settings, b)
    assert first.outcome == back_from_triage.OUTCOME_INSERTED

    # Second B — same photo — should not refuse.
    r = back_from_triage.propose_back_from_triage(settings, b)
    assert r.outcome == back_from_triage.OUTCOME_ALREADY_PENDING
    assert r.pairing_id == first.pairing_id
    assert r.front_photo_id == a
    assert r.front_label == "Batch 00099 #1"

    # Still only one pairing row.
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        n = conn.execute(
            "select count(*) from ingest_pairings where back_photo_id = %s",
            (b,),
        ).fetchone()[0]
    assert n == 1

    db.close_pool()


def _mark_pairing(url: str, pairing_id: int, status: str) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "update ingest_pairings set status = %s, decided_at = now() "
            "where id = %s",
            (status, pairing_id),
        )


def test_b_on_rejected_reopens_with_recomputed_front_and_audit(tmp_path):
    """Fix-up 3 branch: rejected row is reopened, front is recomputed,
    details.reopened_from records the prior status, audit row written."""
    settings = _settings(tmp_path, TEST_DATABASE_URL,
                          tmp_path / "scans", tmp_path / "photos")
    _init_pool(TEST_DATABASE_URL, settings)
    _reset(TEST_DATABASE_URL)

    a = _insert_photo(TEST_DATABASE_URL, settings, root="scans",
                       folder="Batch 00042", filename="a.jpg",
                       sha="4" * 64, scan_sequence=1)
    b = _insert_photo(TEST_DATABASE_URL, settings, root="scans",
                       folder="Batch 00042", filename="b.jpg",
                       sha="5" * 64, scan_sequence=2)

    inserted = back_from_triage.propose_back_from_triage(settings, b)
    assert inserted.outcome == back_from_triage.OUTCOME_INSERTED
    _mark_pairing(TEST_DATABASE_URL, inserted.pairing_id, "rejected")

    # Reopen via B.
    r = back_from_triage.propose_back_from_triage(settings, b)
    assert r.outcome == back_from_triage.OUTCOME_REOPENED
    assert r.pairing_id == inserted.pairing_id
    assert r.reopened_from == "rejected"
    assert r.front_photo_id == a  # recomputed

    with psycopg.connect(TEST_DATABASE_URL) as conn:
        row = conn.execute(
            "select status, back_score, front_photo_id, decided_at, details "
            "from ingest_pairings where id = %s",
            (inserted.pairing_id,),
        ).fetchone()
        aud = conn.execute(
            "select count(*) from audit_log where entity_type = %s "
            "and entity_id = %s and action = 'triage.reopen_back'",
            ("ingest_pairing", inserted.pairing_id),
        ).fetchone()[0]
    assert row[0] == "pending"
    assert row[1] == pytest.approx(1.0)
    assert row[2] == a
    assert row[3] is None
    assert row[4]["source"] == "triage"
    assert row[4]["reopened_from"] == "rejected"
    assert aud == 1

    db.close_pool()


def test_b_on_accepted_warns_and_no_change(tmp_path):
    """Fix-up 3 branch: accepted → warn + no-op."""
    settings = _settings(tmp_path, TEST_DATABASE_URL,
                          tmp_path / "scans", tmp_path / "photos")
    _init_pool(TEST_DATABASE_URL, settings)
    _reset(TEST_DATABASE_URL)

    a = _insert_photo(TEST_DATABASE_URL, settings, root="scans",
                       folder="Batch 00011", filename="a.jpg",
                       sha="6" * 64, scan_sequence=1)
    b = _insert_photo(TEST_DATABASE_URL, settings, root="scans",
                       folder="Batch 00011", filename="b.jpg",
                       sha="7" * 64, scan_sequence=2)
    inserted = back_from_triage.propose_back_from_triage(settings, b)
    _mark_pairing(TEST_DATABASE_URL, inserted.pairing_id, "accepted")

    r = back_from_triage.propose_back_from_triage(settings, b)
    assert r.outcome == back_from_triage.OUTCOME_ALREADY_ACCEPTED
    assert r.is_refusal
    assert r.pairing_id == inserted.pairing_id
    assert r.front_photo_id == a

    with psycopg.connect(TEST_DATABASE_URL) as conn:
        # Row is unchanged (still accepted, same front).
        row = conn.execute(
            "select status, front_photo_id from ingest_pairings where id = %s",
            (inserted.pairing_id,),
        ).fetchone()
    assert row[0] == "accepted"
    assert row[1] == a

    db.close_pool()


def test_b_with_pending_and_rejected_treats_as_pending(tmp_path):
    """Fix-up 3 branch: multiple rows — any pending → treat as pending
    regardless of which is 'most recent'."""
    settings = _settings(tmp_path, TEST_DATABASE_URL,
                          tmp_path / "scans", tmp_path / "photos")
    _init_pool(TEST_DATABASE_URL, settings)
    _reset(TEST_DATABASE_URL)

    a = _insert_photo(TEST_DATABASE_URL, settings, root="scans",
                       folder="Batch 00021", filename="a.jpg",
                       sha="8" * 64, scan_sequence=1)
    b = _insert_photo(TEST_DATABASE_URL, settings, root="scans",
                       folder="Batch 00021", filename="b.jpg",
                       sha="9" * 64, scan_sequence=2)

    # Directly seed a rejected row and a separate pending row for the
    # same back_photo_id. We can't call propose_back_from_triage twice
    # because it would refuse-or-reopen; construct the rows by hand.
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        rej_id = conn.execute(
            """
            insert into ingest_pairings
              (front_photo_id, back_master_path, back_sha256,
               back_source_folder, back_source_filename,
               back_scan_sequence, back_score,
               staging_working_path, staging_thumb_path,
               back_photo_id, back_aspect_mismatch, status, decided_at)
            values (%s, %s, %s, %s, %s, %s, 1.0, %s, %s, %s, false, 'rejected', now())
            returning id
            """,
            (a, "D:\\Scanned\\Batch 00021\\b.jpg", "9" * 64,
             "Batch 00021", "b.jpg", 2,
             "/tmp/w.jpg", "/tmp/t.jpg", b),
        ).fetchone()[0]
        pend_id = conn.execute(
            """
            insert into ingest_pairings
              (front_photo_id, back_master_path, back_sha256,
               back_source_folder, back_source_filename,
               back_scan_sequence, back_score,
               staging_working_path, staging_thumb_path,
               back_photo_id, back_aspect_mismatch, status)
            values (%s, %s, %s, %s, %s, %s, 1.0, %s, %s, %s, false, 'pending')
            returning id
            """,
            (a, "D:\\Scanned\\Batch 00021\\b2.jpg", "a" * 63 + "9",
             "Batch 00021", "b.jpg", 2,
             "/tmp/w2.jpg", "/tmp/t2.jpg", b),
        ).fetchone()[0]

    r = back_from_triage.propose_back_from_triage(settings, b)
    assert r.outcome == back_from_triage.OUTCOME_ALREADY_PENDING
    assert r.pairing_id == pend_id  # not the more-recent rejected

    # The rejected row must still be rejected — pending wins, rejected
    # is not reopened as a side effect.
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        st = conn.execute(
            "select status from ingest_pairings where id = %s", (rej_id,),
        ).fetchone()[0]
    assert st == "rejected"

    db.close_pool()


def test_b_with_multiple_rejected_reopens_most_recent(tmp_path):
    """Fix-up 3 branch: no pending, several rejected → reopen the most
    recent (highest id)."""
    settings = _settings(tmp_path, TEST_DATABASE_URL,
                          tmp_path / "scans", tmp_path / "photos")
    _init_pool(TEST_DATABASE_URL, settings)
    _reset(TEST_DATABASE_URL)

    a = _insert_photo(TEST_DATABASE_URL, settings, root="scans",
                       folder="Batch 00033", filename="a.jpg",
                       sha="a" * 64, scan_sequence=1)
    b = _insert_photo(TEST_DATABASE_URL, settings, root="scans",
                       folder="Batch 00033", filename="b.jpg",
                       sha="b" * 64, scan_sequence=2)

    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        older = conn.execute(
            """
            insert into ingest_pairings
              (front_photo_id, back_master_path, back_sha256,
               back_source_folder, back_source_filename,
               back_scan_sequence, back_score,
               staging_working_path, staging_thumb_path,
               back_photo_id, back_aspect_mismatch, status, decided_at)
            values (%s, %s, %s, %s, %s, %s, 0.7, %s, %s, %s, false, 'rejected', now())
            returning id
            """,
            (None, "D:\\Scanned\\Batch 00033\\bA.jpg", "c" * 64,
             "Batch 00033", "b.jpg", 2,
             "/tmp/wA.jpg", "/tmp/tA.jpg", b),
        ).fetchone()[0]
        newer = conn.execute(
            """
            insert into ingest_pairings
              (front_photo_id, back_master_path, back_sha256,
               back_source_folder, back_source_filename,
               back_scan_sequence, back_score,
               staging_working_path, staging_thumb_path,
               back_photo_id, back_aspect_mismatch, status, decided_at)
            values (%s, %s, %s, %s, %s, %s, 0.65, %s, %s, %s, false, 'rejected', now())
            returning id
            """,
            (None, "D:\\Scanned\\Batch 00033\\bB.jpg", "d" * 64,
             "Batch 00033", "b.jpg", 2,
             "/tmp/wB.jpg", "/tmp/tB.jpg", b),
        ).fetchone()[0]

    r = back_from_triage.propose_back_from_triage(settings, b)
    assert r.outcome == back_from_triage.OUTCOME_REOPENED
    assert r.pairing_id == newer  # most recent

    with psycopg.connect(TEST_DATABASE_URL) as conn:
        older_status = conn.execute(
            "select status from ingest_pairings where id = %s", (older,),
        ).fetchone()[0]
        newer_status, newer_front = conn.execute(
            "select status, front_photo_id from ingest_pairings where id = %s",
            (newer,),
        ).fetchone()
    assert older_status == "rejected"  # untouched
    assert newer_status == "pending"
    assert newer_front == a  # recomputed

    db.close_pool()
