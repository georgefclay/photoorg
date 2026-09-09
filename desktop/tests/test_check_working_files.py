"""Working-file integrity scan (Phase 6 fix-up 7).

Three shapes to cover:
  1. Pointer stale but file is on disk at the standard name — the tool
     updates the DB pointer only, no file touched.
  2. File lives under `_staging/{sha}.{ext}` — the tool moves it to the
     standard name and bumps `file_version`.
  3. Nothing on disk at either name — the tool copies from the master
     (masters are read-only; copy never move).

Also: `repair_face_boxes` must never write `photos.working_path`.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from PIL import Image

from photoarchive import db as dbmod
from photoarchive.modes.ingest import paths as ingest_paths
from photoarchive.tools import check_working_files, repair_face_boxes

from .phase6_fixtures import insert_master, insert_photo, phase6, write_test_jpeg  # noqa: F401


def _sha_from_photo(pid: int) -> str:
    with dbmod.connection() as conn:
        conn.autocommit = True
        return conn.execute(
            "select sha256 from photos where id = %s", (pid,)
        ).fetchone()[0]


def _wp(pid: int) -> str:
    with dbmod.connection() as conn:
        conn.autocommit = True
        return conn.execute(
            "select working_path from photos where id = %s", (pid,)
        ).fetchone()[0]


def _file_version(pid: int) -> int:
    with dbmod.connection() as conn:
        conn.autocommit = True
        return conn.execute(
            "select file_version from photos where id = %s", (pid,)
        ).fetchone()[0]


def test_check_repairs_pointer_when_file_at_standard_name(phase6):
    """Photo row has working_path pointing at a stale location, but the
    file is present at the standard name — the tool updates the pointer
    only, no file operations."""
    with dbmod.connection() as conn:
        conn.autocommit = False
        pid = insert_photo(
            conn, width=100, height=80, working_path="/does/not/exist.jpg",
        )
        insert_master(conn, pid, master_path="C:\\masters\\stale.jpg")
        conn.commit()

    sha = _sha_from_photo(pid)
    standard = ingest_paths.working_path(phase6, pid, sha, "jpg")
    write_test_jpeg(standard)
    v0 = _file_version(pid)

    counts = check_working_files.check(dry_run=False, limit=None)
    assert counts.pointer_repaired_standard >= 1
    assert counts.already_ok >= 0

    assert _wp(pid) == str(standard)
    # No file version bump — file didn't actually move.
    assert _file_version(pid) == v0
    # Audit row records the change.
    with dbmod.connection() as conn:
        conn.autocommit = True
        n = conn.execute(
            """
            select count(*) from audit_log
            where entity_type='photo' and entity_id=%s
              and action = 'photo.working_path_repaired.pointer'
            """,
            (pid,),
        ).fetchone()[0]
        assert n >= 1


def test_check_moves_staging_file_to_standard_name(phase6):
    """File lives under _staging/{sha}.{ext} — the tool moves it into
    place and points the DB at the new path. file_version bumps."""
    with dbmod.connection() as conn:
        conn.autocommit = False
        pid = insert_photo(
            conn, width=100, height=80,
            working_path="/definitely/not/there.jpg",
        )
        insert_master(conn, pid)
        conn.commit()

    sha = _sha_from_photo(pid)
    staging = ingest_paths.staging_working_path(phase6, sha, "jpg")
    write_test_jpeg(staging)
    v0 = _file_version(pid)

    counts = check_working_files.check(dry_run=False, limit=None)
    assert counts.pointer_repaired_staging >= 1

    standard = ingest_paths.working_path(phase6, pid, sha, "jpg")
    assert standard.exists()
    assert not staging.exists()
    assert _wp(pid) == str(standard)
    assert _file_version(pid) == v0 + 1


def test_check_recopies_from_master_when_nothing_on_disk(phase6):
    """Neither standard nor staging exists — the tool copies the master
    into the standard working path."""
    with dbmod.connection() as conn:
        conn.autocommit = False
        pid = insert_photo(conn, width=100, height=80, working_path=None)
        # Master needs to actually exist on disk so shutil.copy2 has a source.
        master_path = phase6.WORKING_DIR.parent / "masters" / "orig.jpg"
        write_test_jpeg(master_path)
        insert_master(conn, pid, master_path=str(master_path))
        conn.commit()

    sha = _sha_from_photo(pid)
    v0 = _file_version(pid)
    counts = check_working_files.check(dry_run=False, limit=None)
    assert counts.recopied_from_master >= 1

    standard = ingest_paths.working_path(phase6, pid, sha, "jpg")
    assert standard.exists()
    assert _wp(pid) == str(standard)
    # Master must still be present (copy, never move).
    master = phase6.WORKING_DIR.parent / "masters" / "orig.jpg"
    assert master.exists()
    assert _file_version(pid) == v0 + 1


def test_check_flags_truly_missing_photos(phase6):
    """No file anywhere — the tool records the id for manual attention."""
    with dbmod.connection() as conn:
        conn.autocommit = False
        pid = insert_photo(conn, width=100, height=80, working_path=None)
        # Master path names a non-existent file.
        insert_master(conn, pid, master_path="/nowhere/nothing.jpg")
        conn.commit()

    counts = check_working_files.check(dry_run=False, limit=None)
    assert counts.truly_missing >= 1
    assert pid in counts.truly_missing_ids


def test_check_dry_run_touches_nothing(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        pid = insert_photo(
            conn, width=100, height=80,
            working_path="/does/not/exist.jpg",
        )
        insert_master(conn, pid)
        conn.commit()
    sha = _sha_from_photo(pid)
    standard = ingest_paths.working_path(phase6, pid, sha, "jpg")
    write_test_jpeg(standard)

    counts = check_working_files.check(dry_run=True, limit=None)
    assert counts.pointer_repaired_standard >= 1
    # DB pointer unchanged.
    assert _wp(pid) == "/does/not/exist.jpg"


def test_repair_face_boxes_never_touches_working_path(phase6):
    """Fix-up 7 item 4: the fix-up 6 repair tool must leave working_path
    alone even when it swaps dims and rescales bboxes."""
    import piexif
    wp = phase6.WORKING_DIR / "for-repair.jpg"
    im = Image.new("RGB", (40, 20), (100, 100, 100))
    exif = piexif.dump({"0th": {piexif.ImageIFD.Orientation: 6}})
    im.save(wp, format="JPEG", exif=exif, quality=90)

    with dbmod.connection() as conn:
        conn.autocommit = False
        # Pre-fix-up-6 row: raw dims, orientation NULL.
        pid = insert_photo(
            conn, width=40, height=20, working_path=str(wp),
        )
        insert_master(conn, pid)
        # A bogus face bbox so repair actually runs.
        conn.execute(
            """
            insert into faces
              (photo_id, person_id, bbox, embedding, embedding_model,
               confidence, source, is_disputed, is_deleted)
            values (%s, null, %s::jsonb, %s, 'test', 0.9, 'ai', false, false)
            """,
            (pid, '{"x": 8.0, "y": 2.5, "w": 8.0, "h": 2.0}', [0.1] * 512),
        )
        conn.commit()

    before = _wp(pid)
    counts = repair_face_boxes.repair(dry_run=False, limit=None)
    assert counts.dims_swapped >= 1
    after = _wp(pid)
    assert after == before, "repair_face_boxes must not modify working_path"
