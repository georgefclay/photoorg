"""Phase 7 split: one photos row per print on a multi-print scan.

  * each region becomes its own photo with its own `photo_masters` row on the
    SAME master file, distinguished by `region_key` (answer 1);
  * the child's sha256 is the identity key, the parent keeps the real
    filename, so re-ingest stays a no-op;
  * a face lands in the child whose print it sits on, and one straddling the
    cut is soft-deleted (invariant 2);
  * album and group memberships are copied;
  * the parent goes to quarantine through the normal triage transition;
  * `detect_faces` is marked done on a child that inherited faces (answer 10);
  * undo puts it all back without deleting anything.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import psycopg
import pytest
from PIL import Image

from photoarchive import db
from photoarchive.modes.cleanup import job as job_mod
from photoarchive.modes.cleanup import repo
from photoarchive.modes.cleanup import split as split_mod
from photoarchive.modes.cleanup.geometry import Rect
from photoarchive.modes.ingest.hasher import sha256_file
from photoarchive.modes.ingest.paths import working_path

from .conftest import DB_AVAILABLE, TEST_DATABASE_URL
from .test_cleanup_accept import (
    _init_pool, _mk_face, _photo_row, _reset, _test_settings,
)

pytestmark = pytest.mark.skipif(not DB_AVAILABLE, reason="no TEST_DATABASE_URL")


def _write_two_print_scan(path: Path, *, w=2000, h=1200) -> list[Rect]:
    """Two prints side by side on a white bed."""
    bed = np.full((h, w), 242, np.uint8)
    rng = np.random.default_rng(9)
    rects: list[Rect] = []
    pw, ph = int(w * 0.36), int(h * 0.62)
    for cx in (w * 0.27, w * 0.73):
        grid = rng.integers(2, 216, size=(ph // 30 + 2, pw // 30 + 2)).astype(np.uint8)
        tile = np.kron(grid, np.ones((30, 30), np.uint8))[:ph, :pw]
        x0, y0 = int(cx - pw / 2), int(h / 2 - ph / 2)
        bed[y0:y0 + ph, x0:x0 + pw] = tile
        rects.append(Rect(cx=x0 + pw / 2, cy=y0 + ph / 2, w=float(pw),
                          h=float(ph), angle=0.0))
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.repeat(bed[:, :, None], 3, axis=2)).save(
        path, "JPEG", quality=96, subsampling=0)
    return rects


def _mk_two_print_photo(settings, *, sha="a" * 64) -> tuple[int, list[Rect]]:
    master = settings.master_roots[0].path / "IMG100.jpg"
    rects = _write_two_print_scan(master)
    with Image.open(master) as im:
        w, h = im.size
    url = settings.DATABASE_URL
    with psycopg.connect(url, autocommit=True) as conn:
        pid = conn.execute("""
            insert into photos
              (sha256, source_root, source_folder, source_filename, mime,
               width, height, file_size, is_scan, scan_batch, scan_sequence,
               triage_status, physical_ref_note, file_version)
            values (%s, 'masters', 'Batch 00001', 'IMG100.jpg', 'image/jpeg',
                    %s, %s, %s, true, 'Batch 00001', 7, 'keep',
                    'envelope 3', 1)
            returning id
        """, (sha, w, h, master.stat().st_size)).fetchone()[0]
        conn.execute("""
            insert into photo_masters
              (photo_id, master_path, sha256, width, height, dpi, mime,
               file_size, is_preferred)
            values (%s, %s, %s, %s, %s, 300, 'image/jpeg', %s, true)
        """, (pid, str(master), sha, w, h, master.stat().st_size))
    wp = working_path(settings, pid, sha, "jpg")
    import shutil
    shutil.copy2(master, wp)
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("update photos set working_path = %s where id = %s",
                     (str(wp), pid))
    return pid, rects


def _analyse_split(settings, pid: int) -> repo.Proposal:
    stats = job_mod.run_cleanup_analyse(settings, reanalyse=True,
                                        write_previews=False)
    assert stats.failed == 0, stats.failures
    with db.connection() as conn:
        conn.autocommit = True
        p = repo.load_pending_for_photo(conn, pid)
    assert p is not None and p.is_split, (
        p.operations if p else "no proposal")
    return p


def _children(url: str, parent_id: int) -> list[dict]:
    with psycopg.connect(url) as conn:
        rows = conn.execute("""
            select p.id, p.sha256, p.source_filename, p.physical_ref_note,
                   p.width, p.height, p.working_path, p.parent_photo_id,
                   p.triage_status::text, p.is_deleted, p.scan_batch,
                   p.scan_sequence, pm.master_path, pm.region_key, pm.region
              from photos p
              left join photo_masters pm on pm.photo_id = p.id
             where p.parent_photo_id = %s
             order by p.id
        """, (parent_id,)).fetchall()
    keys = ("id", "sha256", "source_filename", "physical_ref_note", "width",
            "height", "working_path", "parent_photo_id", "triage_status",
            "is_deleted", "scan_batch", "scan_sequence", "master_path",
            "region_key", "region")
    return [dict(zip(keys, r)) for r in rows]


def test_a_two_print_scan_splits_into_two_photos(tmp_path):
    settings = _test_settings(tmp_path, TEST_DATABASE_URL)
    _init_pool(settings)
    _reset(TEST_DATABASE_URL)
    pid, rects = _mk_two_print_photo(settings)
    master = settings.master_roots[0].path / "IMG100.jpg"
    # The identity key is built from the stored photo_masters.sha256, which is
    # the master's identity in the DB; the bytes on disk are checked separately.
    master_sha = "a" * 64
    master_bytes_sha = sha256_file(master)

    # An album and a group the children must inherit.
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        album = conn.execute(
            "insert into albums (name, source) values ('Batch 00001', 'import')"
            " returning id").fetchone()[0]
        conn.execute("insert into album_photos (album_id, photo_id, position)"
                     " values (%s, %s, 3)", (album, pid))
        group = conn.execute(
            "insert into groups (name, created_by) values ('Clay Family', null)"
            " returning id").fetchone()[0]
        conn.execute("insert into photo_groups (photo_id, group_id)"
                     " values (%s, %s)", (pid, group))
        conn.execute("""
            insert into photo_job_status (photo_id, job_name, status, completed_at)
            values (%s, 'detect_faces', 'done', now())
        """, (pid,))

    # One face on each print, one straddling the gap between them.
    left_face = _mk_face(TEST_DATABASE_URL, pid,
                         {"x": rects[0].cx - 40, "y": rects[0].cy - 40,
                          "w": 80.0, "h": 80.0})
    right_face = _mk_face(TEST_DATABASE_URL, pid,
                          {"x": rects[1].cx - 40, "y": rects[1].cy - 40,
                           "w": 80.0, "h": 80.0})
    straddler = _mk_face(TEST_DATABASE_URL, pid,
                         {"x": (rects[0].cx + rects[1].cx) / 2 - 300,
                          "y": rects[0].cy - 40, "w": 600.0, "h": 80.0})

    proposal = _analyse_split(settings, pid)
    assert len(proposal.split_regions) == 2

    result = split_mod.accept_split(settings, proposal.id)
    assert len(result.children) == 2
    assert result.albums_copied == 2
    assert result.groups_copied == 2
    assert result.faces_lost == [straddler]
    assert result.parent_status == "junk"

    kids = _children(TEST_DATABASE_URL, pid)
    assert len(kids) == 2
    for i, kid in enumerate(kids, start=1):
        # One master file, two rows, told apart by region_key (answer 1).
        assert kid["master_path"] == str(master)
        assert kid["region_key"] != "-"
        assert kid["region"]["frame"] == "display"
        # Identity key, not a file hash.
        assert kid["sha256"] == split_mod.child_sha256(master_sha,
                                                      kid["region_key"])
        # The parent keeps the real filename; children are suffixed.
        assert kid["source_filename"] == f"IMG100.jpg#p{i}"
        assert kid["physical_ref_note"].startswith(f"print {i} of 2 on this scan")
        assert "envelope 3" in kid["physical_ref_note"]
        assert kid["scan_batch"] == "Batch 00001" and kid["scan_sequence"] == 7
        assert kid["triage_status"] == "keep" and kid["is_deleted"] is False
        wp = Path(kid["working_path"])
        assert wp.exists() and wp == working_path(settings, kid["id"],
                                                 kid["sha256"], "jpg")
        with Image.open(wp) as im:
            assert im.size == (kid["width"], kid["height"])
        assert (settings.THUMBS_DIR / f"{kid['id']:08d}.jpg").exists()

    # The master is untouched.
    assert sha256_file(master) == master_bytes_sha

    # Faces landed on the right children, the straddler is soft-deleted.
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        rows = dict(conn.execute(
            "select id, photo_id from faces where id = any(%s)",
            ([left_face, right_face],)).fetchall())
        gone = conn.execute(
            "select is_deleted, delete_reason, photo_id from faces where id = %s",
            (straddler,)).fetchone()
    left_child, right_child = kids[0]["id"], kids[1]["id"]
    assert rows[left_face] == left_child
    assert rows[right_face] == right_child
    assert gone[0] is True and gone[1] == "cleanup_out_of_frame"
    assert gone[2] == pid  # stays on the parent, which is restorable

    # detect_faces is marked done so the next jobs run doesn't duplicate.
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        statuses = dict(conn.execute("""
            select photo_id, status from photo_job_status
             where job_name = 'detect_faces' and photo_id = any(%s)
        """, ([left_child, right_child],)).fetchall())
    assert statuses == {left_child: "done", right_child: "done"}

    # The parent left through the front door: quarantined, restorable.
    parent = _photo_row(TEST_DATABASE_URL, pid)
    assert parent["triage_status"] == "junk"
    assert parent["is_deleted"] is True
    assert parent["working_path"] is None
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        q = conn.execute("select quarantine_path from photos where id = %s",
                         (pid,)).fetchone()[0]
        hint = conn.execute("""
            select new_value from audit_log
             where action = 'triage.decision' and entity_id = %s
             order by id desc limit 1
        """, (pid,)).fetchone()[0]
    assert q and Path(q).exists()
    assert hint["hint"] == "split_parent"


def test_split_undo_returns_the_faces_and_the_parent(tmp_path):
    settings = _test_settings(tmp_path, TEST_DATABASE_URL)
    _init_pool(settings)
    _reset(TEST_DATABASE_URL)
    pid, rects = _mk_two_print_photo(settings)
    face = _mk_face(TEST_DATABASE_URL, pid,
                    {"x": rects[0].cx - 40, "y": rects[0].cy - 40,
                     "w": 80.0, "h": 80.0})
    original_bbox = {"x": rects[0].cx - 40, "y": rects[0].cy - 40,
                     "w": 80.0, "h": 80.0}

    proposal = _analyse_split(settings, pid)
    result = split_mod.accept_split(settings, proposal.id)
    child_ids = [c.photo_id for c in result.children]

    undo = split_mod.undo_split(settings, pid)
    assert undo.children == child_ids
    assert undo.faces_returned == 1
    assert undo.parent_status == "keep"

    with psycopg.connect(TEST_DATABASE_URL) as conn:
        back = conn.execute(
            "select photo_id, bbox from faces where id = %s", (face,)).fetchone()
        kids = conn.execute("""
            select id, is_deleted, triage_status::text from photos
             where id = any(%s) order by id
        """, (child_ids,)).fetchall()
    assert back[0] == pid
    assert back[1] == pytest.approx(original_bbox)
    # No real deletes, ever: the child rows survive, soft-deleted.
    assert [(k[1], k[2]) for k in kids] == [(True, "junk"), (True, "junk")]

    parent = _photo_row(TEST_DATABASE_URL, pid)
    assert parent["triage_status"] == "keep"
    assert parent["is_deleted"] is False
    assert Path(parent["working_path"]).exists()


def test_a_split_cannot_be_accepted_through_the_ordinary_path(tmp_path):
    from photoarchive.modes.cleanup import accept as accept_mod
    settings = _test_settings(tmp_path, TEST_DATABASE_URL)
    _init_pool(settings)
    _reset(TEST_DATABASE_URL)
    pid, _ = _mk_two_print_photo(settings)
    proposal = _analyse_split(settings, pid)
    with pytest.raises(accept_mod.CleanupError, match="split"):
        accept_mod.accept_proposal(settings, proposal.id)
    # …and it is never offered to bulk accept.
    assert job_mod.geometric_only_ids(settings) == []


def test_region_key_and_child_sha_are_deterministic():
    a = Rect(cx=100.0, cy=200.0, w=50.0, h=60.0, angle=0.0)
    assert split_mod.child_region_key(a) == "75,170,50,60"
    assert (split_mod.child_sha256("f" * 64, "75,170,50,60")
            == split_mod.child_sha256("f" * 64, "75,170,50,60"))
    assert (split_mod.child_sha256("f" * 64, "75,170,50,60")
            != split_mod.child_sha256("f" * 64, "0,0,50,60"))


@pytest.fixture(autouse=True)
def _guard_passes(monkeypatch):
    from .test_cleanup_accept import _FakeGuard
    monkeypatch.setattr(job_mod, "run_masters_guard",
                        lambda roots: _FakeGuard(roots))
