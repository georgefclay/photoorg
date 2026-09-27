"""Phase 7 accept / reject / undo against the test database.

Covers the invariants that matter:
  * Accept keeps the previous working copy in `_versions/` and bumps
    `file_version` (invariant 1) — the master file is never touched;
  * face boxes travel with the pixels, and a box that falls outside the new
    frame is soft-deleted with `cleanup_out_of_frame`, not dropped
    (invariant 2);
  * format follows the source (invariant 3);
  * Reject copies to MANUAL_FIX_DIR and leaves the working copy alone;
  * Undo restores the file and the boxes and bumps `file_version` again;
  * `clean` photos never enter the review queue;
  * the masters probe refuses to run when a root is writable (invariant 4).
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import psycopg
import pytest
from PIL import Image

from photoarchive import db
from photoarchive.config import Settings
from photoarchive.modes.cleanup import accept as accept_mod
from photoarchive.modes.cleanup import job as job_mod
from photoarchive.modes.cleanup import paths as cpaths
from photoarchive.modes.cleanup import render as render_mod
from photoarchive.modes.cleanup import repo
from photoarchive.modes.ingest.hasher import sha256_file
from photoarchive.modes.ingest.paths import working_path

from .conftest import DB_AVAILABLE, TEST_DATABASE_URL

pytestmark = pytest.mark.skipif(not DB_AVAILABLE, reason="no TEST_DATABASE_URL")


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

def _test_settings(tmp_path: Path, url: str, **overrides) -> Settings:
    kwargs = dict(
        MASTER_ROOTS=f"masters={tmp_path / 'masters'}|scan",
        WORKING_DIR=tmp_path / "working",
        QUARANTINE_DIR=tmp_path / "quarantine",
        MANUAL_FIX_DIR=tmp_path / "manual-fix",
        THUMBS_DIR=tmp_path / "thumbs",
        CLEANUP_DIR=tmp_path / "cleanup",
        DATABASE_URL=url,
        INFERENCE_URL="http://x", INFERENCE_TOKEN="x",
        WEB_API_URL="http://x", WEB_API_TOKEN="x",
    )
    kwargs.update(overrides)
    s = Settings(**kwargs)
    for d in (s.WORKING_DIR, s.QUARANTINE_DIR, s.MANUAL_FIX_DIR, s.THUMBS_DIR,
              s.CLEANUP_DIR, Path(str(tmp_path / "masters"))):
        d.mkdir(parents=True, exist_ok=True)
    cpaths.ensure_dirs(s)
    return s


def _init_pool(settings: Settings) -> None:
    if getattr(db, "_pool", None) is not None:
        db.close_pool()
    db.init_pool(settings)


def _reset(url: str) -> None:
    """Clear the tables these tests touch.

    Deliberately NOT `restart identity`: the test database is migrated with
    PHOTOORG_DB_ROLE=web, so `faces`, `people`, `groups` and friends have their
    sequences parked at WEB_ID_FLOOR (Phase 9 fix-up 1). Restarting identity
    would drop them back to 1 and break `test_web_id_floor` later in the run.
    """
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("""
            truncate cleanup_spend, cleanup_proposals,
                     dedupe_exclusions, dedupe_members, dedupe_groups,
                     album_photos, albums, suggestions, photo_groups, groups,
                     photo_backs, photo_masters, faces, people,
                     photo_job_status,
                     audit_log, triage_hints, ingest_pairings, ingest_rescans,
                     ingest_failures, job_items, job_runs, photos
            cascade
        """)


def _write_scan(path: Path, *, w=1200, h=900, angle=0.0, fmt="JPEG") -> None:
    """A skewed print on a white bed, saved as the working copy."""
    import cv2
    import math
    bed = np.full((h, w, 3), 242, np.uint8)
    pw, ph = int(w * 0.72), int(h * 0.72)
    rng = np.random.default_rng(5)
    # 2..215: a real black and a near-white, so the fixture does not also
    # trip the levels op. 215 stays clear of the bed (bed - BED_DELTA = 217),
    # which would otherwise mask the band out and split the print in two.
    grid = rng.integers(2, 216, size=(ph // 30 + 1, pw // 30 + 1)).astype(np.uint8)
    tile = np.kron(grid, np.ones((30, 30), np.uint8))[:ph, :pw]
    tile = np.repeat(tile[:, :, None], 3, axis=2)
    m = cv2.getRotationMatrix2D((pw / 2, ph / 2), -angle, 1.0)
    rw = int(math.ceil(abs(pw * math.cos(math.radians(angle)))
                       + abs(ph * math.sin(math.radians(angle)))))
    rh = int(math.ceil(abs(pw * math.sin(math.radians(angle)))
                       + abs(ph * math.cos(math.radians(angle)))))
    m[0, 2] += rw / 2 - pw / 2
    m[1, 2] += rh / 2 - ph / 2
    rot = cv2.warpAffine(tile, m, (rw, rh), borderValue=(242, 242, 242))
    mask = cv2.warpAffine(np.full((ph, pw), 255, np.uint8), m, (rw, rh),
                          borderValue=0)
    x0, y0 = (w - rw) // 2, (h - rh) // 2
    region = bed[y0:y0 + rh, x0:x0 + rw]
    region[mask > 127] = rot[mask > 127]
    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "TIFF":
        Image.fromarray(bed).save(path, "TIFF", compression="tiff_lzw")
    else:
        Image.fromarray(bed).save(path, "JPEG", quality=96, subsampling=0)


def _mk_scan_photo(
    settings: Settings, *, sha: str, mime="image/jpeg", w=1200, h=900,
    angle=0.0, triage_status="keep", scan_batch="Batch 00001",
    scan_sequence=1, filename="IMG001.jpg", with_master=True,
) -> int:
    url = settings.DATABASE_URL
    ext = "tif" if mime == "image/tiff" else "jpg"
    master = settings.master_roots[0].path / f"{filename}"
    _write_scan(master, w=w, h=h, angle=angle,
                fmt="TIFF" if mime == "image/tiff" else "JPEG")
    with psycopg.connect(url, autocommit=True) as conn:
        pid = conn.execute("""
            insert into photos
              (sha256, source_root, source_folder, source_filename, mime,
               width, height, file_size, is_scan, scan_batch, scan_sequence,
               triage_status, is_private, is_deleted, file_version)
            values (%s, 'masters', %s, %s, %s, %s, %s, %s, true, %s, %s,
                    %s, false, false, 1)
            returning id
        """, (sha, scan_batch, filename, mime, w, h, master.stat().st_size,
              scan_batch, scan_sequence, triage_status)).fetchone()[0]
        if with_master:
            conn.execute("""
                insert into photo_masters
                  (photo_id, master_path, sha256, width, height, dpi, mime,
                   file_size, is_preferred)
                values (%s, %s, %s, %s, %s, 300, %s, %s, true)
            """, (pid, str(master), sha, w, h, mime, master.stat().st_size))
    wp = working_path(settings, pid, sha, ext)
    import shutil
    shutil.copy2(master, wp)
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("update photos set working_path = %s where id = %s",
                     (str(wp), pid))
    return pid


def _mk_face(url: str, photo_id: int, bbox: dict, *, person_id=None) -> int:
    with psycopg.connect(url, autocommit=True) as conn:
        return conn.execute("""
            insert into faces (photo_id, person_id, bbox, source, confidence)
            values (%s, %s, %s::jsonb, 'ai', 0.99) returning id
        """, (photo_id, person_id, json.dumps(bbox))).fetchone()[0]


def _analyse_one(settings: Settings, photo_id: int) -> repo.Proposal:
    stats = job_mod.run_cleanup_analyse(settings, write_previews=False)
    assert stats.failed == 0, stats.failures
    with db.connection() as conn:
        conn.autocommit = True
        p = repo.load_pending_for_photo(conn, photo_id)
    assert p is not None, "no pending proposal was created"
    return p


def _photo_row(url: str, pid: int) -> dict:
    with psycopg.connect(url) as conn:
        r = conn.execute("""
            select file_version, width, height, working_path, phash, dhash,
                   file_size, triage_status::text, is_deleted, mime
              from photos where id = %s
        """, (pid,)).fetchone()
    keys = ("file_version", "width", "height", "working_path", "phash",
            "dhash", "file_size", "triage_status", "is_deleted", "mime")
    return dict(zip(keys, r))


# --------------------------------------------------------------------------
# Accept
# --------------------------------------------------------------------------

def test_accept_keeps_the_previous_version_and_bumps_file_version(tmp_path):
    settings = _test_settings(tmp_path, TEST_DATABASE_URL)
    _init_pool(settings)
    _reset(TEST_DATABASE_URL)
    pid = _mk_scan_photo(settings, sha="a" * 64, angle=3.0)

    before = _photo_row(TEST_DATABASE_URL, pid)
    master = settings.master_roots[0].path / "IMG001.jpg"
    master_sha_before = sha256_file(master)

    proposal = _analyse_one(settings, pid)
    assert "deskew" in proposal.op_names and "crop" in proposal.op_names

    result = accept_mod.accept_proposal(settings, proposal.id)
    after = _photo_row(TEST_DATABASE_URL, pid)

    assert after["file_version"] == before["file_version"] + 1 == 2
    # The previous working copy is kept, forever.
    kept = cpaths.version_path(settings, pid, 1, "jpg")
    assert kept.exists(), list(cpaths.versions_dir(settings).iterdir())
    # The new working copy sits at the standard name and is the cleaned one.
    wp = Path(after["working_path"])
    assert wp == working_path(settings, pid, "a" * 64, "jpg")
    assert wp.exists()
    with Image.open(wp) as im:
        assert im.size == (result.width, result.height)
    assert (after["width"], after["height"]) == (result.width, result.height)
    assert result.width < before["width"]      # cropped to the print
    # Hashes were recomputed from the new pixels (answer 11).
    assert after["phash"] and after["phash"] != before["phash"]
    assert after["dhash"]
    # Masters untouched.
    assert sha256_file(master) == master_sha_before
    # A thumbnail was regenerated.
    assert (settings.THUMBS_DIR / f"{pid:08d}.jpg").exists()

    with db.connection() as conn:
        conn.autocommit = True
        assert repo.load_proposal(conn, proposal.id).status == "accepted"
        audit = conn.execute("""
            select previous_value, new_value from audit_log
             where action = 'cleanup.accept' and entity_id = %s
        """, (pid,)).fetchone()
    assert audit is not None
    assert audit[0]["file_version"] == 1
    assert audit[1]["file_version"] == 2
    assert audit[1]["version_file"] == str(kept)


def test_format_follows_the_source_for_tiff(tmp_path):
    settings = _test_settings(tmp_path, TEST_DATABASE_URL)
    _init_pool(settings)
    _reset(TEST_DATABASE_URL)
    pid = _mk_scan_photo(settings, sha="c" * 64, mime="image/tiff", angle=3.0,
                         filename="IMG002.tif")

    proposal = _analyse_one(settings, pid)
    accept_mod.accept_proposal(settings, proposal.id)

    after = _photo_row(TEST_DATABASE_URL, pid)
    wp = Path(after["working_path"])
    assert wp.suffix == ".tif"
    with Image.open(wp) as im:
        assert im.format == "TIFF"
    assert after["mime"] == "image/tiff"
    assert cpaths.version_path(settings, pid, 1, "tif").exists()


def test_unticking_every_op_changes_nothing(tmp_path):
    settings = _test_settings(tmp_path, TEST_DATABASE_URL)
    _init_pool(settings)
    _reset(TEST_DATABASE_URL)
    pid = _mk_scan_photo(settings, sha="d" * 64, angle=3.0)
    proposal = _analyse_one(settings, pid)

    result = accept_mod.accept_proposal(settings, proposal.id, ticked=[])
    assert result.no_op
    after = _photo_row(TEST_DATABASE_URL, pid)
    assert after["file_version"] == 1
    assert not list(cpaths.versions_dir(settings).glob("*"))


def test_unticking_deskew_still_crops_to_the_axis_aligned_print(tmp_path):
    settings = _test_settings(tmp_path, TEST_DATABASE_URL)
    _init_pool(settings)
    _reset(TEST_DATABASE_URL)
    pid = _mk_scan_photo(settings, sha="e" * 64, angle=4.0)
    proposal = _analyse_one(settings, pid)

    full = render_mod.plan_from(proposal.operations, ("deskew", "crop"),
                                settings=settings)
    crop_only = render_mod.plan_from(proposal.operations, ("crop",),
                                     settings=settings)
    assert full.transform.angle_deg == pytest.approx(
        proposal.operations["ops"]["deskew"]["angle_deg"], abs=1e-3)
    assert crop_only.transform.angle_deg == 0.0
    # The axis-aligned bounds of a rotated print are larger than the print.
    assert crop_only.transform.out_w > full.transform.out_w

    result = accept_mod.accept_proposal(settings, proposal.id, ticked=["crop"])
    assert (result.width, result.height) == (crop_only.transform.out_w,
                                            crop_only.transform.out_h)


# --------------------------------------------------------------------------
# Face boxes
# --------------------------------------------------------------------------

def test_face_boxes_travel_with_the_pixels(tmp_path):
    settings = _test_settings(tmp_path, TEST_DATABASE_URL)
    _init_pool(settings)
    _reset(TEST_DATABASE_URL)
    pid = _mk_scan_photo(settings, sha="f" * 64, angle=3.0)
    # One face in the middle of the print, one out on the scanner bed.
    inside = _mk_face(TEST_DATABASE_URL, pid,
                      {"x": 560.0, "y": 420.0, "w": 80.0, "h": 80.0})
    outside = _mk_face(TEST_DATABASE_URL, pid,
                       {"x": 6.0, "y": 6.0, "w": 40.0, "h": 40.0})

    proposal = _analyse_one(settings, pid)
    result = accept_mod.accept_proposal(settings, proposal.id)

    assert result.faces_moved == 1
    assert result.faces_lost == [outside]

    with psycopg.connect(TEST_DATABASE_URL) as conn:
        kept = conn.execute(
            "select bbox, is_deleted from faces where id = %s", (inside,),
        ).fetchone()
        gone = conn.execute(
            "select is_deleted, delete_reason from faces where id = %s",
            (outside,),
        ).fetchone()
    # Moved into the cropped frame, same size.
    assert kept[1] is False
    assert kept[0]["w"] == pytest.approx(80.0, abs=0.01)
    assert kept[0]["x"] < 560.0
    assert 0 <= kept[0]["x"] <= result.width
    # Lost, but only soft-deleted and with the reason recorded.
    assert gone == (True, "cleanup_out_of_frame")
    # And the crop really is where the transform said.
    plan = render_mod.plan_from(proposal.operations, result.ticked,
                                settings=settings)
    expect = plan.transform.apply_bbox(
        {"x": 560.0, "y": 420.0, "w": 80.0, "h": 80.0})
    assert kept[0]["x"] == pytest.approx(expect["x"], abs=0.01)
    assert kept[0]["y"] == pytest.approx(expect["y"], abs=0.01)
    # A crop was written for the surviving face.
    assert (settings.THUMBS_DIR / "faces" / f"{inside}.jpg").exists()


def test_a_tonal_only_accept_leaves_boxes_alone(tmp_path):
    settings = _test_settings(tmp_path, TEST_DATABASE_URL)
    _init_pool(settings)
    _reset(TEST_DATABASE_URL)
    pid = _mk_scan_photo(settings, sha="1" * 64, angle=3.0)
    face = _mk_face(TEST_DATABASE_URL, pid,
                    {"x": 560.0, "y": 420.0, "w": 80.0, "h": 80.0})
    proposal = _analyse_one(settings, pid)

    # Tick only the tonal ops the analyser found (if any); with none ticked
    # the accept is a no-op, which is itself the point: boxes never move.
    tonal = [n for n in ("colour", "levels") if n in proposal.op_names]
    result = accept_mod.accept_proposal(settings, proposal.id, ticked=tonal)
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        bbox = conn.execute("select bbox from faces where id = %s",
                            (face,)).fetchone()[0]
    assert bbox == {"x": 560.0, "y": 420.0, "w": 80.0, "h": 80.0}
    assert result.faces_moved == 0


# --------------------------------------------------------------------------
# Reject
# --------------------------------------------------------------------------

def test_reject_copies_to_manual_fix_and_leaves_the_working_copy(tmp_path):
    settings = _test_settings(tmp_path, TEST_DATABASE_URL)
    _init_pool(settings)
    _reset(TEST_DATABASE_URL)
    pid = _mk_scan_photo(settings, sha="2" * 64, angle=3.0)
    proposal = _analyse_one(settings, pid)

    before = _photo_row(TEST_DATABASE_URL, pid)
    result = accept_mod.reject_proposal(settings, proposal.id, reason="too tight")

    target = cpaths.manual_fix_path(settings, pid, "2" * 64, "jpg")
    assert Path(result.manual_path) == target
    assert target.exists()
    assert target.read_bytes() == Path(before["working_path"]).read_bytes()

    after = _photo_row(TEST_DATABASE_URL, pid)
    assert after["file_version"] == before["file_version"]
    assert after["working_path"] == before["working_path"]

    with db.connection() as conn:
        conn.autocommit = True
        assert repo.load_proposal(conn, proposal.id).status == "manual"
        assert repo.pending_ids(conn) == []
        queue = repo.manual_queue(conn)
    assert [p.photo_id for p in queue] == [pid]

    with psycopg.connect(TEST_DATABASE_URL) as conn:
        row = conn.execute("""
            select new_value from audit_log
             where action = 'cleanup.reject' and entity_id = %s
        """, (pid,)).fetchone()
    assert row[0]["manual_path"] == str(target)
    assert row[0]["reason"] == "too tight"


# --------------------------------------------------------------------------
# Undo
# --------------------------------------------------------------------------

def test_undo_restores_the_file_and_the_boxes_and_bumps_again(tmp_path):
    settings = _test_settings(tmp_path, TEST_DATABASE_URL)
    _init_pool(settings)
    _reset(TEST_DATABASE_URL)
    pid = _mk_scan_photo(settings, sha="3" * 64, angle=3.0)
    inside = _mk_face(TEST_DATABASE_URL, pid,
                      {"x": 560.0, "y": 420.0, "w": 80.0, "h": 80.0})
    outside = _mk_face(TEST_DATABASE_URL, pid,
                       {"x": 6.0, "y": 6.0, "w": 40.0, "h": 40.0})

    original = _photo_row(TEST_DATABASE_URL, pid)
    original_bytes = Path(original["working_path"]).read_bytes()

    proposal = _analyse_one(settings, pid)
    accept_mod.accept_proposal(settings, proposal.id)
    cleaned = _photo_row(TEST_DATABASE_URL, pid)

    undo = accept_mod.undo_accept(settings, pid)
    restored = _photo_row(TEST_DATABASE_URL, pid)

    # The pixels are back…
    assert Path(restored["working_path"]).read_bytes() == original_bytes
    assert (restored["width"], restored["height"]) == (original["width"],
                                                      original["height"])
    assert restored["phash"] == original["phash"]
    # …and history is not rewritten: the version goes up again, and the
    # cleaned file is kept too.
    assert restored["file_version"] == 3 == undo.new_version
    assert cpaths.version_path(settings, pid, 2, "jpg").exists()

    with psycopg.connect(TEST_DATABASE_URL) as conn:
        back = conn.execute("select bbox from faces where id = %s",
                            (inside,)).fetchone()[0]
        revived = conn.execute(
            "select is_deleted, delete_reason from faces where id = %s",
            (outside,),
        ).fetchone()
        undo_audit = conn.execute("""
            select new_value from audit_log
             where action = 'cleanup.undo' and entity_id = %s
             order by id desc limit 1
        """, (pid,)).fetchone()
    assert back == {"x": 560.0, "y": 420.0, "w": 80.0, "h": 80.0}
    assert revived == (False, None)
    assert undo_audit[0]["faces_restored"] == 2
    assert undo_audit[0]["faces_undeleted"] == 1
    assert cleaned["file_version"] == 2


# --------------------------------------------------------------------------
# Queue rules
# --------------------------------------------------------------------------

def test_clean_photos_never_enter_the_queue(tmp_path):
    settings = _test_settings(tmp_path, TEST_DATABASE_URL)
    _init_pool(settings)
    _reset(TEST_DATABASE_URL)
    # A print that fills the bed, square on, neutral and full-range: nothing
    # to do. `_write_scan` leaves a bed margin, so shrink the margin away by
    # making the scan the same size as the print.
    pid = _mk_scan_photo(settings, sha="4" * 64, angle=0.0, w=4000, h=3000)
    path = Path(_photo_row(TEST_DATABASE_URL, pid)["working_path"])
    rng = np.random.default_rng(2)
    grid = rng.integers(2, 216, size=(101, 134)).astype(np.uint8)
    arr = np.kron(grid, np.ones((30, 30), np.uint8))[:3000, :4000]
    Image.fromarray(np.repeat(arr[:, :, None], 3, axis=2)).save(
        path, "JPEG", quality=96, subsampling=0)

    stats = job_mod.run_cleanup_analyse(settings, reanalyse=True,
                                        write_previews=False)
    assert stats.clean == 1, stats.to_dict()
    with db.connection() as conn:
        conn.autocommit = True
        assert repo.pending_ids(conn) == []
        counts = repo.status_counts(conn)
    assert counts.get("clean") == 1


def test_analysis_is_resumable_and_skips_decided_proposals(tmp_path):
    settings = _test_settings(tmp_path, TEST_DATABASE_URL)
    _init_pool(settings)
    _reset(TEST_DATABASE_URL)
    pid = _mk_scan_photo(settings, sha="5" * 64, angle=3.0)

    first = job_mod.run_cleanup_analyse(settings, write_previews=False)
    assert first.analysed == 1
    second = job_mod.run_cleanup_analyse(settings, write_previews=False)
    assert second.total == 0 and second.analysed == 0
    third = job_mod.run_cleanup_analyse(settings, reanalyse=True,
                                        write_previews=False)
    assert third.analysed == 1
    # The superseded row is kept; only one proposal is live.
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        rows = conn.execute("""
            select status::text, count(*) from cleanup_proposals
             where photo_id = %s group by 1
        """, (pid,)).fetchall()
    assert dict(rows) == {"pending": 1, "superseded": 1}


def test_reanalysing_a_clean_photo_does_not_double_count_it(tmp_path):
    """A `clean` row is superseded like a `pending` one — it carries no
    decision, and leaving it behind reported the photo as clean twice."""
    settings = _test_settings(tmp_path, TEST_DATABASE_URL)
    _init_pool(settings)
    _reset(TEST_DATABASE_URL)
    pid = _mk_scan_photo(settings, sha="c" * 64, angle=0.0, w=4000, h=3000)
    path = Path(_photo_row(TEST_DATABASE_URL, pid)["working_path"])
    rng = np.random.default_rng(3)
    grid = rng.integers(2, 216, size=(101, 134)).astype(np.uint8)
    arr = np.kron(grid, np.ones((30, 30), np.uint8))[:3000, :4000]
    Image.fromarray(np.repeat(arr[:, :, None], 3, axis=2)).save(
        path, "JPEG", quality=96, subsampling=0)

    for _ in range(3):
        job_mod.run_cleanup_analyse(settings, reanalyse=True, write_previews=False)

    with db.connection() as conn:
        conn.autocommit = True
        counts = repo.status_counts(conn)
    assert counts.get("clean") == 1, counts
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        rows = dict(conn.execute("""
            select status::text, count(*) from cleanup_proposals
             where photo_id = %s group by 1
        """, (pid,)).fetchall())
    assert rows == {"clean": 1, "superseded": 2}


def test_a_decision_is_never_superseded_by_a_re_analysis(tmp_path):
    settings = _test_settings(tmp_path, TEST_DATABASE_URL)
    _init_pool(settings)
    _reset(TEST_DATABASE_URL)
    pid = _mk_scan_photo(settings, sha="d" * 64, angle=3.0)
    proposal = _analyse_one(settings, pid)
    accept_mod.reject_proposal(settings, proposal.id)

    job_mod.run_cleanup_analyse(settings, reanalyse=True, write_previews=False)
    with db.connection() as conn:
        conn.autocommit = True
        still = repo.load_proposal(conn, proposal.id)
    assert still.status == "manual", "a decision must survive a re-analysis"


def test_back_shaped_scans_are_out_of_scope(tmp_path):
    settings = _test_settings(tmp_path, TEST_DATABASE_URL)
    _init_pool(settings)
    _reset(TEST_DATABASE_URL)
    keep = _mk_scan_photo(settings, sha="6" * 64, angle=3.0,
                          filename="IMG010.jpg", scan_sequence=10)
    hinted = _mk_scan_photo(settings, sha="7" * 64, angle=3.0,
                            filename="IMG011.jpg", scan_sequence=11)
    paired = _mk_scan_photo(settings, sha="8" * 64, angle=3.0,
                            filename="IMG012.jpg", scan_sequence=12)
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("""
            insert into triage_hints (photo_id, hint, confidence, details)
            values (%s, 'possible_back', 0.8, '{}'::jsonb)
        """, (hinted,))
        conn.execute("""
            insert into ingest_pairings
              (back_photo_id, back_sha256, back_master_path,
               back_source_folder, back_source_filename, back_score,
               staging_working_path, status)
            values (%s, %s, 'Z:/whatever.jpg', 'Batch 00001', 'IMG012.jpg',
                    0.9, 'Z:/staging.jpg', 'pending')
        """, (paired, "8" * 64))

    with db.connection() as conn:
        conn.autocommit = True
        ids = [r.photo_id for r in repo.select_scope(conn)]
    assert ids == [keep]


def test_junk_and_digital_photos_are_out_of_scope(tmp_path):
    settings = _test_settings(tmp_path, TEST_DATABASE_URL)
    _init_pool(settings)
    _reset(TEST_DATABASE_URL)
    scan = _mk_scan_photo(settings, sha="9" * 64, angle=3.0)
    junk = _mk_scan_photo(settings, sha="b" * 64, angle=3.0,
                          triage_status="junk", filename="IMG020.jpg",
                          scan_sequence=20)
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("update photos set is_scan = false where id = %s", (junk,))
        digital = conn.execute("""
            insert into photos
              (sha256, source_root, source_folder, source_filename, mime,
               width, height, is_scan, triage_status, working_path)
            values (%s, 'photos', '', 'phone.jpg', 'image/jpeg', 100, 100,
                    false, 'keep', 'x.jpg')
            returning id
        """, ("e" * 64,)).fetchone()[0]

    with db.connection() as conn:
        conn.autocommit = True
        ids = [r.photo_id for r in repo.select_scope(conn)]
    assert ids == [scan]
    assert digital not in ids


def test_bulk_accept_only_takes_geometric_only_proposals(tmp_path):
    # The tonal ops are opt-in since fix-up 2, so switch levels on: this test
    # is about bulk accept refusing anything that is not pure geometry, which
    # needs a non-geometric proposal to exist at all.
    settings = _test_settings(tmp_path, TEST_DATABASE_URL,
                              CLEANUP_LEVELS_ENABLED=True)
    _init_pool(settings)
    _reset(TEST_DATABASE_URL)
    geo = _mk_scan_photo(settings, sha="a" * 64, angle=3.0,
                         filename="IMG030.jpg", scan_sequence=30)
    tonal = _mk_scan_photo(settings, sha="c" * 64, angle=3.0,
                           filename="IMG031.jpg", scan_sequence=31)
    # Fade the second one so it picks up a levels op.
    path = Path(_photo_row(TEST_DATABASE_URL, tonal)["working_path"])
    with Image.open(path) as im:
        arr = np.asarray(im).astype(np.float32)
    faded = np.clip(arr * 0.3 + 120.0, 0, 255).astype(np.uint8)
    Image.fromarray(faded).save(path, "JPEG", quality=96, subsampling=0)

    job_mod.run_cleanup_analyse(settings, write_previews=False)
    with db.connection() as conn:
        conn.autocommit = True
        by_photo = {p.photo_id: p for p in
                    (repo.load_proposal(conn, i) for i in repo.pending_ids(conn))}
    assert by_photo[geo].is_geometric_only
    assert not by_photo[tonal].is_geometric_only

    ids = job_mod.geometric_only_ids(settings)
    assert ids == [by_photo[geo].id]
    stats = job_mod.bulk_accept_geometric(settings, ids)
    assert (stats.accepted, stats.failed) == (1, 0)
    assert _photo_row(TEST_DATABASE_URL, geo)["file_version"] == 2
    assert _photo_row(TEST_DATABASE_URL, tonal)["file_version"] == 1


def test_needs_manual_proposals_offer_no_geometry(tmp_path):
    settings = _test_settings(tmp_path, TEST_DATABASE_URL)
    _init_pool(settings)
    _reset(TEST_DATABASE_URL)
    pid = _mk_scan_photo(settings, sha="d" * 64, angle=3.0)
    # Blank out most of the scan so the print covers too little of it.
    path = Path(_photo_row(TEST_DATABASE_URL, pid)["working_path"])
    with Image.open(path) as im:
        arr = np.asarray(im).copy()
    arr[:, :] = 242
    arr[380:520, 500:700] = 40
    Image.fromarray(arr).save(path, "JPEG", quality=96, subsampling=0)

    job_mod.run_cleanup_analyse(settings, reanalyse=True, write_previews=False)
    with db.connection() as conn:
        conn.autocommit = True
        p = repo.load_pending_for_photo(conn, pid)
    assert p is not None and p.needs_manual
    assert p.manual_reason in ("print_too_small", "no_print_found")
    assert "deskew" not in p.op_names and "crop" not in p.op_names
    # It still reaches the queue (answer 7) rather than the manual list.
    with db.connection() as conn:
        conn.autocommit = True
        assert p.id in repo.pending_ids(conn)
    assert job_mod.geometric_only_ids(settings) == []


# --------------------------------------------------------------------------
# Masters guard (invariant 4)
# --------------------------------------------------------------------------

def test_the_masters_probe_refuses_when_a_root_is_writable(tmp_path):
    settings = _test_settings(tmp_path, TEST_DATABASE_URL)
    _init_pool(settings)
    _reset(TEST_DATABASE_URL)
    _mk_scan_photo(settings, sha="a" * 64, angle=3.0)
    # tmp_path is writable by construction, so the probe must fail.
    with pytest.raises(job_mod.MastersWritable) as exc:
        job_mod.run_cleanup_analyse(settings, write_previews=False)
    assert "icacls" in str(exc.value)
    with db.connection() as conn:
        conn.autocommit = True
        assert repo.pending_ids(conn) == []


@pytest.fixture(autouse=True)
def _guard_passes(monkeypatch, request):
    """Every test but the guard test wants the probe to pass; a temp dir is
    writable by definition, so stub it out rather than chmod-ing Windows ACLs."""
    if request.node.name.endswith("refuses_when_a_root_is_writable"):
        return
    monkeypatch.setattr(job_mod, "run_masters_guard",
                        lambda roots: _FakeGuard(roots))


class _FakeGuard:
    def __init__(self, roots):
        self._roots = list(roots)

    @property
    def all_read_only(self) -> bool:
        return True

    def to_params(self) -> dict:
        return {"stubbed": True, "roots": [r.label for r in self._roots]}
