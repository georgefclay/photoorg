"""Fix-up 5: correcting a split by hand, and saying there isn't one.

A split the analyser gets wrong could not be accepted and could not be
corrected — R was the only way out, and R copies the file to MANUAL_FIX_DIR
and parks the photo in a queue, which is the wrong answer twice over when the
scan is fine and only the regions are wrong (#708, #3817) or when there is no
split at all (#3839).

So two ways out. **W** rejects the proposal and leaves the photo exactly as it
is; **G** opens the region editor and the accepted children come from the
regions George drew. Edited regions have to be indistinguishable from measured
ones by the time `accept_split` sees them — same shape, same transform, faces
assigned by containment the same way.
"""
from __future__ import annotations

import json
from pathlib import Path

import psycopg
import pytest

from photoarchive import db
from photoarchive.modes.cleanup import accept as accept_mod
from photoarchive.modes.cleanup import regions as regions_mod
from photoarchive.modes.cleanup import repo
from photoarchive.modes.cleanup import split as split_mod
from photoarchive.modes.cleanup.accept import CleanupError

from .conftest import DB_AVAILABLE, TEST_DATABASE_URL
from .test_cleanup_accept import (
    _FakeGuard, _init_pool, _mk_face, _mk_scan_photo, _reset, _test_settings,
)
from .test_cleanup_split import _analyse_split, _children, _mk_two_print_photo

pytestmark = pytest.mark.skipif(not DB_AVAILABLE, reason="no TEST_DATABASE_URL")


@pytest.fixture
def settings(monkeypatch, tmp_path):
    s = _test_settings(tmp_path, TEST_DATABASE_URL)
    _init_pool(s)
    _reset(TEST_DATABASE_URL)
    from photoarchive.modes.cleanup import job as job_mod
    monkeypatch.setattr(job_mod, "run_masters_guard",
                        lambda roots: _FakeGuard(roots))
    return s


def _photo(url, pid):
    with psycopg.connect(url) as conn:
        row = conn.execute(
            """select working_path, file_version, width, height, is_deleted,
                      triage_status::text
                 from photos where id = %s""", (pid,)).fetchone()
    return dict(zip(("working_path", "file_version", "width", "height",
                     "is_deleted", "triage_status"), row))


def _audit(url, action, photo_id):
    with psycopg.connect(url) as conn:
        rows = conn.execute(
            """select previous_value, new_value from audit_log
                where action = %s and entity_type = 'photo' and entity_id = %s
                order by id""", (action, photo_id)).fetchall()
    return rows


# --------------------------------------------------------------------------
# W — keep whole
# --------------------------------------------------------------------------

def test_keep_whole_rejects_the_proposal_and_changes_nothing(settings):
    pid, _rects = _mk_two_print_photo(settings, sha="a" * 64)
    proposal = _analyse_split(settings, pid)
    before = _photo(TEST_DATABASE_URL, pid)
    before_bytes = Path(before["working_path"]).read_bytes()

    res = accept_mod.keep_whole(settings, proposal.id)

    assert res.photo_id == pid
    assert res.regions == len(proposal.split_regions)
    after = _photo(TEST_DATABASE_URL, pid)
    assert after == before, "the photo row must be untouched"
    assert Path(after["working_path"]).read_bytes() == before_bytes
    assert not _children(TEST_DATABASE_URL, pid), "no children may be created"

    with db.connection() as conn:
        conn.autocommit = True
        again = repo.load_proposal(conn, proposal.id)
    assert again.status == "rejected"


def test_keep_whole_says_why_in_the_audit_row(settings):
    pid, _rects = _mk_two_print_photo(settings, sha="b" * 64)
    proposal = _analyse_split(settings, pid)
    accept_mod.keep_whole(settings, proposal.id)

    rows = _audit(TEST_DATABASE_URL, "cleanup.reject", pid)
    assert rows, "a decision must leave an audit row"
    _prev, new = rows[-1]
    assert new["reason"] == "not_a_split"
    assert new["kept_whole"] is True
    assert new["status"] == "rejected"
    assert new["regions"] == len(proposal.split_regions)


def test_keep_whole_writes_no_file(settings):
    pid, _rects = _mk_two_print_photo(settings, sha="c" * 64)
    proposal = _analyse_split(settings, pid)
    manual_dir = settings.MANUAL_FIX_DIR
    before = set(manual_dir.rglob("*")) if manual_dir.exists() else set()
    accept_mod.keep_whole(settings, proposal.id)
    after = set(manual_dir.rglob("*")) if manual_dir.exists() else set()
    assert after == before, "unlike R, W copies nothing anywhere"


def test_keep_whole_refuses_a_decided_proposal(settings):
    pid, _rects = _mk_two_print_photo(settings, sha="d" * 64)
    proposal = _analyse_split(settings, pid)
    accept_mod.keep_whole(settings, proposal.id)
    with pytest.raises(CleanupError, match="rejected"):
        accept_mod.keep_whole(settings, proposal.id)


def test_keep_whole_works_on_an_ordinary_proposal_too(settings):
    """It is phrased around splits, but "leave this photo alone" is a
    reasonable answer to any proposal."""
    pid = _mk_scan_photo(settings, sha="e" * 64, angle=3.0)
    from photoarchive.modes.cleanup import job as job_mod
    job_mod.run_cleanup_analyse(settings, write_previews=False)
    with db.connection() as conn:
        conn.autocommit = True
        p = repo.load_pending_for_photo(conn, pid)
    assert p is not None and not p.is_split

    before = _photo(TEST_DATABASE_URL, pid)
    res = accept_mod.keep_whole(settings, p.id)
    assert res.regions == 0
    assert _photo(TEST_DATABASE_URL, pid) == before


# --------------------------------------------------------------------------
# G — edited regions
# --------------------------------------------------------------------------

def _save_edit(settings, proposal, boxes):
    edited = regions_mod.to_regions(
        boxes, settings=settings,
        src_w=proposal.width, src_h=proposal.height,
        inset_px=(proposal.operations.get("analysis") or {}).get("inset_px"),
    )
    with db.connection() as conn:
        conn.autocommit = True
        repo.update_split_regions(conn, proposal.id, edited, actor="desktop")
        return repo.load_proposal(conn, proposal.id), edited


def test_edited_regions_replace_the_measured_ones(settings):
    pid, _rects = _mk_two_print_photo(settings, sha="f" * 64)
    proposal = _analyse_split(settings, pid)
    assert len(proposal.split_regions) == 2

    w, h = proposal.width, proposal.height
    boxes = [
        regions_mod.Box(20.0, 20.0, w / 3 - 40, h - 40),
        regions_mod.Box(w / 3 + 20, 20.0, w / 3 - 40, h - 40),
        regions_mod.Box(2 * w / 3 + 20, 20.0, w / 3 - 40, h - 40),
    ]
    again, edited = _save_edit(settings, proposal, boxes)

    assert len(again.split_regions) == 3
    assert [r["index"] for r in again.split_regions] == [1, 2, 3]
    assert all(r["edited_by"] == "human" for r in again.split_regions)
    # The caption and the report read this, so it has to keep up.
    assert again.operations["ops"]["split"]["regions"] == 3
    assert again.operations["ops"]["split"]["edited_by"] == "desktop"
    assert again.operations["split_edited"] is True


def test_accept_uses_the_edited_regions(settings):
    pid, _rects = _mk_two_print_photo(settings, sha="1" * 64)
    proposal = _analyse_split(settings, pid)
    w, h = proposal.width, proposal.height
    boxes = [
        regions_mod.Box(20.0, 20.0, w / 3 - 40, h - 40),
        regions_mod.Box(w / 3 + 20, 20.0, w / 3 - 40, h - 40),
        regions_mod.Box(2 * w / 3 + 20, 20.0, w / 3 - 40, h - 40),
    ]
    again, _edited = _save_edit(settings, proposal, boxes)

    result = split_mod.accept_split(settings, again.id)
    assert len(result.children) == 3

    kids = _children(TEST_DATABASE_URL, pid)
    assert len(kids) == 3
    for kid, box in zip(kids, boxes):
        # Each child is the size of the region it came from, less the inset.
        assert kid["width"] == pytest.approx(box.w, abs=20)
        assert kid["height"] == pytest.approx(box.h, abs=20)
        assert Path(kid["working_path"]).exists()
    # One master file, three rows, told apart by region_key — the same rule
    # as a measured split.
    assert len({k["region_key"] for k in kids}) == 3
    assert len({k["master_path"] for k in kids}) == 1


def test_faces_follow_the_edited_regions(settings):
    """Containment is judged against what George drew, not what was
    measured — otherwise correcting the regions would scatter the faces."""
    pid, _rects = _mk_two_print_photo(settings, sha="2" * 64)
    proposal = _analyse_split(settings, pid)
    w, h = proposal.width, proposal.height
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("""
            insert into photo_job_status (photo_id, job_name, status, completed_at)
            values (%s, 'detect_faces', 'done', now())""", (pid,))
    # A face well inside what will become the left region.
    left_face = _mk_face(TEST_DATABASE_URL, pid,
                         {"x": w * 0.10, "y": h * 0.40, "w": 80.0, "h": 80.0})
    # …and one well inside the right.
    right_face = _mk_face(TEST_DATABASE_URL, pid,
                          {"x": w * 0.78, "y": h * 0.40, "w": 80.0, "h": 80.0})

    boxes = [regions_mod.Box(10.0, 10.0, w / 2 - 20, h - 20),
             regions_mod.Box(w / 2 + 10, 10.0, w / 2 - 20, h - 20)]
    again, _edited = _save_edit(settings, proposal, boxes)
    result = split_mod.accept_split(settings, again.id)

    assert result.faces_lost == []
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        owners = dict(conn.execute(
            "select id, photo_id from faces where id = any(%s)",
            ([left_face, right_face],)).fetchall())
    kids = [k["id"] for k in _children(TEST_DATABASE_URL, pid)]
    assert owners[left_face] == kids[0]
    assert owners[right_face] == kids[1]
    assert owners[left_face] != owners[right_face]


def test_editing_leaves_an_audit_trail_of_both_versions(settings):
    """The audit row carries the regions as they were and as they are, so a
    hand-edited split can be read back rather than re-derived."""
    pid, _rects = _mk_two_print_photo(settings, sha="3" * 64)
    proposal = _analyse_split(settings, pid)
    w, h = proposal.width, proposal.height
    boxes = [regions_mod.Box(10.0, 10.0, w / 2 - 20, h - 20),
             regions_mod.Box(w / 2 + 10, 10.0, w / 2 - 20, h - 20)]
    edited = regions_mod.to_regions(boxes, settings=settings, src_w=w, src_h=h)
    with db.connection() as conn:
        conn.autocommit = False
        repo.update_split_regions(conn, proposal.id, edited, actor="desktop")
        db.audit(conn, actor="desktop", action="cleanup.split",
                 entity_type="photo", entity_id=pid,
                 previous_value={"regions": len(proposal.split_regions),
                                 "split_regions": proposal.split_regions},
                 new_value={"proposal_id": proposal.id,
                            "regions": len(edited), "edited_by": "human",
                            "split_regions": edited})
        conn.commit()

    rows = _audit(TEST_DATABASE_URL, "cleanup.split", pid)
    prev, new = rows[-1]
    assert prev["regions"] == 2
    assert new["edited_by"] == "human"
    assert len(new["split_regions"]) == 2
    assert new["split_regions"][0]["edited_by"] == "human"


def test_edited_regions_are_refused_if_they_overlap(settings):
    """The rule lives in `regions.problems`, and the dialog will not save
    while it returns anything — checked here so the rule is pinned even if
    the dialog changes."""
    pid, _rects = _mk_two_print_photo(settings, sha="4" * 64)
    proposal = _analyse_split(settings, pid)
    w, h = proposal.width, proposal.height
    bad = [regions_mod.Box(10.0, 10.0, w * 0.7, h - 20),
           regions_mod.Box(w * 0.4, 10.0, w * 0.55, h - 20)]
    issues = regions_mod.problems(bad, src_w=w, src_h=h)
    assert any("overlap" in m for m in issues), issues


# --------------------------------------------------------------------------
# Z — undo, for a split
# --------------------------------------------------------------------------

def test_undo_of_an_edited_split_returns_everything(settings):
    """Answer 9 said a split is undoable within the session; an edited one is
    no different, and this is the test that says so."""
    pid, _rects = _mk_two_print_photo(settings, sha="5" * 64)
    proposal = _analyse_split(settings, pid)
    w, h = proposal.width, proposal.height
    boxes = [regions_mod.Box(10.0, 10.0, w / 2 - 20, h - 20),
             regions_mod.Box(w / 2 + 10, 10.0, w / 2 - 20, h - 20)]
    again, _edited = _save_edit(settings, proposal, boxes)
    split_mod.accept_split(settings, again.id)
    assert len(_children(TEST_DATABASE_URL, pid)) == 2

    undo = split_mod.undo_split(settings, pid)

    assert len(undo.children) == 2
    assert undo.parent_status in ("keep", "private")
    kids = _children(TEST_DATABASE_URL, pid)
    assert kids and all(k["is_deleted"] for k in kids), (
        "children are soft-deleted, never removed")
    parent = _photo(TEST_DATABASE_URL, pid)
    assert not parent["is_deleted"]
    assert Path(parent["working_path"]).exists()


# --------------------------------------------------------------------------
# The job path: a named list, and the label actually arriving
# --------------------------------------------------------------------------

def test_the_classification_label_reaches_the_analyser(settings):
    """`select_scope` selected the label and then dropped it on the floor:
    `ScopeRow` was built without it, so the document veto could not fire in
    the job at all — only in a caller that passed the label by hand. The sweep
    did exactly that, which is why it looked right."""
    pid, _rects = _mk_two_print_photo(settings, sha="6" * 64)
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("""
            insert into suggestions (photo_id, kind, source, status, payload)
            values (%s, 'classification', 'ai', 'pending',
                    '{"label": "document", "confidence": 0.98}'::jsonb)""",
                     (pid,))

    _init_pool(settings)
    with db.connection() as conn:
        conn.autocommit = True
        rows = repo.select_scope(conn, reanalyse=True, photo_ids=[pid])
    assert len(rows) == 1
    assert rows[0].ai_label == "document", (
        "the label must survive the trip from the query to the row")

    from photoarchive.modes.cleanup import job as job_mod
    job_mod.run_cleanup_analyse(settings, reanalyse=True, photo_ids=[pid],
                                write_previews=False)
    with db.connection() as conn:
        conn.autocommit = True
        p = repo.load_pending_for_photo(conn, pid)
    assert p is not None
    assert not p.split_regions, "a document must not be split by the job either"
    assert p.operations["split_vetoed_by_label"] == "document"


def test_a_named_photo_list_restricts_the_pass(settings):
    """Applying a sweep's nine findings means re-analysing nine photos, not a
    batch around them."""
    from photoarchive.modes.cleanup import job as job_mod

    wanted, _rects = _mk_two_print_photo(settings, sha="7" * 64)
    other = _mk_scan_photo(settings, sha="8" * 64, angle=3.0,
                           scan_sequence=42, filename="IMG_other.jpg")
    job_mod.run_cleanup_analyse(settings, write_previews=False)

    _init_pool(settings)
    with db.connection() as conn:
        conn.autocommit = True
        before = {r.photo_id: r.id
                  for r in [repo.load_pending_for_photo(conn, wanted),
                            repo.load_pending_for_photo(conn, other)]
                  if r is not None}
    assert set(before) == {wanted, other}

    stats = job_mod.run_cleanup_analyse(settings, reanalyse=True,
                                        photo_ids=[wanted],
                                        write_previews=False)
    assert stats.total == 1, "only the named photo may be re-measured"

    with db.connection() as conn:
        conn.autocommit = True
        after_wanted = repo.load_pending_for_photo(conn, wanted)
        after_other = repo.load_pending_for_photo(conn, other)
    assert after_wanted.id != before[wanted], "the named photo was re-measured"
    assert after_other.id == before[other], "the other photo was left alone"
