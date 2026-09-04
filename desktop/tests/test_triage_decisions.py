"""Decision state machine against TEST_DATABASE_URL.

Verifies:
  keep → junk → undo → keep leaves the file in working/;
  junk moves it to quarantine/ and restore moves it back;
  private never touches the file;
  every transition writes an audit row.
"""
from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest

from photoarchive import db
from photoarchive.config import Settings
from photoarchive.modes.triage import decisions

from .conftest import DB_AVAILABLE, TEST_DATABASE_URL


pytestmark = pytest.mark.skipif(not DB_AVAILABLE, reason="no TEST_DATABASE_URL")


def _test_settings(tmp_path: Path, url: str) -> Settings:
    s = Settings(
        MASTER_ROOTS="dummy=Z:\\",
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
    return s


def _init_pool_for_url(url: str, settings: Settings) -> None:
    if getattr(db, "_pool", None) is not None:
        db.close_pool()
    db.init_pool(settings)


def _reset(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("truncate audit_log, triage_hints, photos "
                     "restart identity cascade")


def _make_photo(url: str, settings: Settings, *, sha: str, ext: str = "jpg",
                content: bytes = b"stub") -> int:
    """Insert a photos row and place a file at the working path."""
    with psycopg.connect(url, autocommit=True) as conn:
        pid = conn.execute(
            """
            insert into photos
              (sha256, source_root, source_folder, source_filename,
               mime, width, height, working_path, triage_status, is_deleted)
            values (%s, 'tst', '', %s, %s, 100, 100, %s, 'untriaged', false)
            returning id
            """,
            (sha, f"stub.{ext}", f"image/{ext}", None),
        ).fetchone()[0]
    from photoarchive.modes.ingest.paths import working_path
    wp = working_path(settings, pid, sha, ext)
    wp.parent.mkdir(parents=True, exist_ok=True)
    wp.write_bytes(content)
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("update photos set working_path = %s where id = %s",
                     (str(wp), pid))
    return pid


def _state(url: str, pid: int) -> dict:
    with psycopg.connect(url) as conn:
        r = conn.execute(
            "select triage_status, is_private, is_deleted, working_path, "
            "quarantine_path from photos where id = %s", (pid,),
        ).fetchone()
    return {
        "triage_status": r[0], "is_private": r[1], "is_deleted": r[2],
        "working_path": r[3], "quarantine_path": r[4],
    }


def _audit_count(url: str, pid: int) -> int:
    with psycopg.connect(url) as conn:
        return int(conn.execute(
            "select count(*) from audit_log where entity_type='photo' "
            "and entity_id=%s and action='triage.decision'",
            (pid,),
        ).fetchone()[0])


def test_keep_junk_undo_keep_leaves_file_in_working(tmp_path):
    settings = _test_settings(tmp_path, TEST_DATABASE_URL)
    _init_pool_for_url(TEST_DATABASE_URL, settings)
    _reset(TEST_DATABASE_URL)
    pid = _make_photo(TEST_DATABASE_URL, settings, sha="a" * 64)

    # keep
    r1 = decisions.apply_decision(settings, pid, "keep")
    st = _state(TEST_DATABASE_URL, pid)
    assert st["triage_status"] == "keep"
    assert st["is_deleted"] is False
    assert st["working_path"] and Path(st["working_path"]).exists()
    assert st["quarantine_path"] is None

    # junk → moves to quarantine
    r2 = decisions.apply_decision(settings, pid, "junk", hint="document")
    st = _state(TEST_DATABASE_URL, pid)
    assert st["triage_status"] == "junk"
    assert st["is_deleted"] is True
    assert st["working_path"] is None
    assert st["quarantine_path"] and Path(st["quarantine_path"]).exists()
    assert Path(st["quarantine_path"]).parent.samefile(settings.QUARANTINE_DIR)

    # undo → back to keep, file back in working
    decisions.undo(settings, r2)
    st = _state(TEST_DATABASE_URL, pid)
    assert st["triage_status"] == "keep"
    assert st["is_deleted"] is False
    assert st["working_path"] and Path(st["working_path"]).exists()
    assert st["quarantine_path"] is None
    assert Path(st["working_path"]).parent.samefile(settings.WORKING_DIR)

    # 3 transitions = 3 audit rows
    assert _audit_count(TEST_DATABASE_URL, pid) == 3

    db.close_pool()


def test_junk_then_restore_from_quarantine(tmp_path):
    settings = _test_settings(tmp_path, TEST_DATABASE_URL)
    _init_pool_for_url(TEST_DATABASE_URL, settings)
    _reset(TEST_DATABASE_URL)
    pid = _make_photo(TEST_DATABASE_URL, settings, sha="b" * 64)

    decisions.apply_decision(settings, pid, "junk")
    st = _state(TEST_DATABASE_URL, pid)
    assert st["triage_status"] == "junk"
    assert Path(st["quarantine_path"]).exists()

    decisions.restore_from_quarantine(settings, pid)
    st = _state(TEST_DATABASE_URL, pid)
    assert st["triage_status"] == "untriaged"
    assert st["is_deleted"] is False
    assert st["working_path"] and Path(st["working_path"]).exists()
    assert st["quarantine_path"] is None

    db.close_pool()


def test_private_never_touches_file(tmp_path):
    settings = _test_settings(tmp_path, TEST_DATABASE_URL)
    _init_pool_for_url(TEST_DATABASE_URL, settings)
    _reset(TEST_DATABASE_URL)
    pid = _make_photo(TEST_DATABASE_URL, settings, sha="c" * 64)

    before = _state(TEST_DATABASE_URL, pid)
    working = before["working_path"]
    assert working and Path(working).exists()

    decisions.apply_decision(settings, pid, "private")
    st = _state(TEST_DATABASE_URL, pid)
    assert st["triage_status"] == "private"
    assert st["is_private"] is True
    assert st["working_path"] == working
    assert Path(working).exists()
    assert st["quarantine_path"] is None

    # Toggling to keep clears is_private and file stays.
    decisions.unprivate(settings, pid)
    st = _state(TEST_DATABASE_URL, pid)
    assert st["triage_status"] == "keep"
    assert st["is_private"] is False
    assert Path(working).exists()

    db.close_pool()


def test_audit_row_contains_hint_and_prev_status(tmp_path):
    settings = _test_settings(tmp_path, TEST_DATABASE_URL)
    _init_pool_for_url(TEST_DATABASE_URL, settings)
    _reset(TEST_DATABASE_URL)
    pid = _make_photo(TEST_DATABASE_URL, settings, sha="d" * 64)

    decisions.apply_decision(settings, pid, "junk", hint="screenshot")

    with psycopg.connect(TEST_DATABASE_URL) as conn:
        r = conn.execute(
            "select previous_value, new_value from audit_log "
            "where entity_type='photo' and entity_id=%s and action='triage.decision' "
            "order by id desc limit 1",
            (pid,),
        ).fetchone()
    prev, new = r
    assert prev["triage_status"] == "untriaged"
    assert new["triage_status"] == "junk"
    assert new["hint"] == "screenshot"

    db.close_pool()
