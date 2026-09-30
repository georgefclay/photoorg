"""Fix-up 5: the rules an edited set of split regions has to obey.

`regions.py` is deliberately free of Qt and of the database, because this is
where the rules live and the dialog is only a way of calling them with a
mouse. What matters is that a region George drew and a region the analyser
measured are the same thing to everything downstream: `accept_split` renders
from `region["transform"]` and assigns faces by containment against
`region["rect"]`, and it must not be able to tell them apart.
"""
from __future__ import annotations

import pytest

from photoarchive.modes.cleanup import regions as R
from photoarchive.modes.cleanup.geometry import Rect, transform_for

from .test_cleanup_analyse import _settings


# --------------------------------------------------------------------------
# Boxes
# --------------------------------------------------------------------------

def test_a_box_survives_the_round_trip_through_a_rect():
    b = R.Box(x=100.0, y=60.0, w=400.0, h=300.0, angle=2.5)
    again = R.Box.from_rect(b.to_rect())
    # `from_rect` takes the axis-aligned bounds, which for a tilted rectangle
    # are wider than the rectangle — the tilt is what survives exactly.
    assert again.angle == pytest.approx(2.5)
    assert again.cx == pytest.approx(b.cx, abs=0.01)
    assert again.cy == pytest.approx(b.cy, abs=0.01)
    assert again.w >= b.w - 0.01


def test_a_box_is_clamped_into_the_scan():
    b = R.Box(x=-50.0, y=-20.0, w=900.0, h=700.0).clamped(800, 600)
    assert b.x == 0.0 and b.y == 0.0
    assert b.x + b.w <= 800.0 + 1e-6
    assert b.y + b.h <= 600.0 + 1e-6


def test_boxes_come_back_from_stored_regions(tmp_path):
    settings = _settings(tmp_path)
    boxes = [R.Box(0.0, 0.0, 400.0, 300.0), R.Box(420.0, 0.0, 400.0, 300.0)]
    stored = R.to_regions(boxes, settings=settings, src_w=900, src_h=400)
    back = R.boxes_from_regions(stored)
    assert len(back) == 2
    for a, b in zip(boxes, back):
        assert a.cx == pytest.approx(b.cx, abs=1.0)
        assert a.w == pytest.approx(b.w, abs=1.0)


# --------------------------------------------------------------------------
# The rules
# --------------------------------------------------------------------------

def test_two_clean_regions_have_no_problems():
    boxes = [R.Box(10.0, 10.0, 380.0, 280.0), R.Box(410.0, 10.0, 380.0, 280.0)]
    assert R.problems(boxes, src_w=800, src_h=300) == []


def test_one_region_is_not_a_split():
    assert any("at least two" in m
               for m in R.problems([R.Box(0.0, 0.0, 100.0, 100.0)],
                                   src_w=800, src_h=600))


def test_overlapping_regions_are_refused():
    boxes = [R.Box(0.0, 0.0, 400.0, 300.0), R.Box(200.0, 0.0, 400.0, 300.0)]
    msgs = R.problems(boxes, src_w=800, src_h=300)
    assert any("overlap" in m for m in msgs), msgs


def test_regions_that_merely_touch_are_allowed():
    """Adjacent prints on a proof sheet share an edge — that is the normal
    case for the scans this editor exists for, not an error."""
    boxes = [R.Box(0.0, 0.0, 400.0, 300.0), R.Box(400.0, 0.0, 400.0, 300.0)]
    assert R.problems(boxes, src_w=800, src_h=300) == []


def test_a_region_outside_the_scan_is_refused():
    boxes = [R.Box(0.0, 0.0, 400.0, 300.0), R.Box(700.0, 0.0, 400.0, 300.0)]
    msgs = R.problems(boxes, src_w=800, src_h=300)
    assert any("outside" in m for m in msgs), msgs


def test_a_slip_of_the_mouse_is_refused():
    boxes = [R.Box(0.0, 0.0, 400.0, 300.0), R.Box(500.0, 0.0, 4.0, 4.0)]
    msgs = R.problems(boxes, src_w=800, src_h=300)
    assert any("too small" in m for m in msgs), msgs


# --------------------------------------------------------------------------
# The grid helper
# --------------------------------------------------------------------------

def test_the_grid_covers_the_area_without_overlapping():
    area = R.Box(100.0, 50.0, 800.0, 600.0)
    boxes = R.grid_boxes(area, 3, 4)
    assert len(boxes) == 12
    assert R.problems(boxes, src_w=1200, src_h=900) == []
    covered = sum(b.area for b in boxes)
    assert covered == pytest.approx(area.area, rel=1e-6)
    got = R.bounds_of(boxes)
    assert got.x == pytest.approx(area.x)
    assert got.w == pytest.approx(area.w)


def test_the_grid_can_leave_a_gutter():
    boxes = R.grid_boxes(R.Box(0.0, 0.0, 800.0, 600.0), 2, 2, gap=20.0)
    assert len(boxes) == 4
    assert R.overlap_area(boxes[0], boxes[1]) == 0.0
    assert boxes[1].x - (boxes[0].x + boxes[0].w) == pytest.approx(20.0)


def test_the_grid_starts_from_the_regions_already_there():
    """#3817's ten prints sit inside the sheet, not the whole bed, so the
    grid has to be laid over what was found rather than over the scan."""
    found = [R.Box(120.0, 90.0, 600.0, 200.0), R.Box(120.0, 300.0, 600.0, 200.0)]
    area = R.bounds_of(found)
    boxes = R.grid_boxes(area, 2, 4)
    assert R.bounds_of(boxes).x == pytest.approx(120.0)
    assert R.bounds_of(boxes).w == pytest.approx(600.0)


# --------------------------------------------------------------------------
# Regions the rest of the pipeline can use
# --------------------------------------------------------------------------

def test_an_edited_region_is_shaped_like_a_measured_one(tmp_path):
    settings = _settings(tmp_path)
    boxes = [R.Box(0.0, 0.0, 400.0, 300.0), R.Box(420.0, 0.0, 400.0, 300.0)]
    out = R.to_regions(boxes, settings=settings, src_w=900, src_h=320, dpi=300)
    assert [r["index"] for r in out] == [1, 2]
    for r in out:
        assert set(r) >= {"index", "rect", "area_frac", "transform"}
        assert r["edited_by"] == "human"
        assert 0.0 < r["area_frac"] <= 1.0
        t = r["transform"]
        assert {"m", "src_w", "src_h", "out_w", "out_h"} <= set(t)
        # Fix-up 4's rule holds for hand-drawn regions too.
        assert t["out_w"] <= 900 and t["out_h"] <= 320


def test_the_transform_is_the_analysers_own(tmp_path):
    """Built by the same function with the same arguments — if these ever
    diverge, an edited split renders differently from a measured one."""
    settings = _settings(tmp_path)
    box = R.Box(40.0, 30.0, 500.0, 360.0, angle=1.4)
    out = R.to_regions([box, R.Box(600.0, 30.0, 300.0, 360.0)],
                       settings=settings, src_w=1000, src_h=500,
                       inset_px=6.0)
    expected = transform_for(
        box.to_rect(), src_w=1000, src_h=500,
        deskew=abs(1.4) >= settings.CLEANUP_DESKEW_MIN_DEG,
        crop=True, inset_px=6.0,
    )
    assert out[0]["transform"] == expected.to_json()


def test_an_edited_region_is_clamped_into_the_scan(tmp_path):
    settings = _settings(tmp_path)
    out = R.to_regions([R.Box(-40.0, -40.0, 500.0, 400.0),
                        R.Box(600.0, 10.0, 500.0, 300.0)],
                       settings=settings, src_w=900, src_h=400)
    for r in out:
        rect = Rect.from_json(r["rect"])
        x, y, w, h = rect.axis_aligned_bounds()
        assert x >= -0.5 and y >= -0.5
        assert x + w <= 900.5 and y + h <= 400.5


def test_regions_are_numbered_the_way_they_sit_on_the_bed():
    """Children are named `#p1`, `#p2`; George will look for them in the
    order they lie on the scan, not in the order he happened to draw them."""
    drawn = [
        R.Box(400.0, 400.0, 300.0, 200.0),      # bottom right, drawn first
        R.Box(40.0, 40.0, 300.0, 200.0),        # top left
        R.Box(400.0, 40.0, 300.0, 200.0),       # top right
        R.Box(40.0, 400.0, 300.0, 200.0),       # bottom left
    ]
    ordered = R.reading_order(drawn)
    assert [(b.x, b.y) for b in ordered] == [
        (40.0, 40.0), (400.0, 40.0), (40.0, 400.0), (400.0, 400.0)]
