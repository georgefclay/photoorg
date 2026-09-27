"""Phase 7 geometry: the one affine, and the face boxes that ride on it.

No database, no files — pure maths.
"""
from __future__ import annotations

import pytest

from photoarchive.modes.cleanup.geometry import (
    MIN_BOX_OVERLAP, Rect, Transform, bbox_overlap_frac, inset_px_for_dpi,
    normalise_angle, transform_for,
)


def test_identity_is_identity():
    t = Transform.identity(100, 80)
    assert t.is_identity
    assert t.apply_point(12, 34) == (12, 34)
    assert t.apply_bbox({"x": 10, "y": 10, "w": 20, "h": 20}) == {
        "x": 10, "y": 10, "w": 20, "h": 20,
    }


def test_pure_crop_offsets_points_and_boxes():
    t = Transform.build(src_w=1000, src_h=800, angle_deg=0.0, crop=(100, 50, 400, 300))
    assert (t.out_w, t.out_h) == (400, 300)
    x, y = t.apply_point(150, 100)
    assert (round(x), round(y)) == (50, 50)

    box = {"x": 140, "y": 90, "w": 40, "h": 40}
    moved = t.apply_bbox(box)
    assert moved is not None
    assert (round(moved["x"]), round(moved["y"])) == (40, 40)
    assert (round(moved["w"]), round(moved["h"])) == (40, 40)


def test_rotation_expands_the_canvas_and_keeps_the_centre_centred():
    t = Transform.build(src_w=400, src_h=300, angle_deg=10.0)
    # The canvas grows to hold the rotated source.
    assert t.out_w > 400 and t.out_h > 300
    cx, cy = t.apply_point(200, 150)
    assert cx == pytest.approx(t.out_w / 2, abs=1.0)
    assert cy == pytest.approx(t.out_h / 2, abs=1.0)


@pytest.mark.parametrize("angle", [-12.0, -3.0, -0.4, 0.0, 0.4, 3.0, 12.0])
def test_bbox_round_trips_through_the_inverse_within_a_pixel(angle):
    """Rotate + crop, then apply the inverse: the box comes back."""
    rect = Rect(cx=520.0, cy=400.0, w=900.0, h=700.0, angle=angle)
    t = transform_for(rect, src_w=1100, src_h=850, deskew=True, crop=True,
                      inset_px=4.0)
    inv = t.invert()
    for box in (
        {"x": 300.0, "y": 240.0, "w": 90.0, "h": 90.0},
        {"x": 520.0, "y": 380.0, "w": 40.0, "h": 55.0},
        {"x": 700.0, "y": 500.0, "w": 120.0, "h": 120.0},
    ):
        moved = t.apply_bbox(box)
        assert moved is not None, f"box vanished at {angle}°"
        back = inv.apply_bbox(moved)
        assert back is not None
        for key in ("x", "y", "w", "h"):
            assert back[key] == pytest.approx(box[key], abs=1.0), (
                f"{key} drifted at {angle}°: {box[key]} -> {back[key]}"
            )


def test_point_round_trip_is_exact():
    t = Transform.build(src_w=800, src_h=600, angle_deg=7.5, crop=(30, 40, 500, 400))
    inv = t.invert()
    for x, y in ((0, 0), (123.5, 456.25), (799, 599)):
        px, py = t.apply_point(x, y)
        bx, by = inv.apply_point(px, py)
        assert bx == pytest.approx(x, abs=1e-6)
        assert by == pytest.approx(y, abs=1e-6)


def test_box_outside_the_new_frame_is_rejected_not_clamped_to_nothing():
    t = Transform.build(src_w=1000, src_h=800, angle_deg=0.0, crop=(400, 400, 200, 200))
    # Entirely in the discarded top-left of the scan.
    assert t.apply_bbox({"x": 10, "y": 10, "w": 50, "h": 50}) is None
    # Mostly outside: less than MIN_BOX_OVERLAP survives.
    assert t.apply_bbox({"x": 380, "y": 380, "w": 30, "h": 30}) is None
    # Comfortably inside: kept.
    kept = t.apply_bbox({"x": 450, "y": 450, "w": 40, "h": 40})
    assert kept == {"x": 50, "y": 50, "w": 40, "h": 40}


def test_partially_out_of_frame_box_is_clamped_when_most_of_it_survives():
    t = Transform.build(src_w=1000, src_h=800, angle_deg=0.0, crop=(100, 100, 400, 400))
    box = {"x": 90, "y": 200, "w": 100, "h": 100}  # 10 px over the left edge
    moved = t.apply_bbox(box)
    assert moved is not None
    assert moved["x"] == 0
    assert moved["w"] == pytest.approx(90.0)
    assert (100 * 90) / (100 * 100) > MIN_BOX_OVERLAP


def test_crop_only_uses_the_axis_aligned_bounds():
    rect = Rect(cx=500.0, cy=400.0, w=600.0, h=400.0, angle=5.0)
    t = transform_for(rect, src_w=1000, src_h=800, deskew=False, crop=True,
                      inset_px=0.0)
    assert t.angle_deg == 0.0
    x, y, w, h = rect.axis_aligned_bounds()
    assert t.out_w == pytest.approx(round(w), abs=1)
    assert t.out_h == pytest.approx(round(h), abs=1)


def test_deskew_only_keeps_the_whole_rotated_canvas():
    rect = Rect(cx=500.0, cy=400.0, w=600.0, h=400.0, angle=5.0)
    t = transform_for(rect, src_w=1000, src_h=800, deskew=True, crop=False)
    assert t.out_w >= 1000 and t.out_h >= 800
    assert t.angle_deg == pytest.approx(5.0)


def test_neither_op_is_the_identity():
    rect = Rect(cx=500.0, cy=400.0, w=600.0, h=400.0, angle=5.0)
    t = transform_for(rect, src_w=1000, src_h=800, deskew=False, crop=False)
    assert t.is_identity


def test_normalise_angle_swaps_w_and_h_past_45_degrees():
    a, w, h = normalise_angle(80.0, 100.0, 200.0)
    assert a == pytest.approx(-10.0)
    assert (w, h) == (200.0, 100.0)
    a, w, h = normalise_angle(3.0, 100.0, 200.0)
    assert a == pytest.approx(3.0)
    assert (w, h) == (100.0, 200.0)


def test_inset_scales_with_dpi():
    assert inset_px_for_dpi(300, 4.0) == pytest.approx(4.0)
    assert inset_px_for_dpi(1200, 4.0) == pytest.approx(16.0)
    assert inset_px_for_dpi(600, 4.0) == pytest.approx(8.0)
    assert inset_px_for_dpi(None, 4.0) == pytest.approx(4.0)
    assert inset_px_for_dpi(0, 4.0) == pytest.approx(4.0)


def test_rect_corners_and_bounds_agree_for_an_axis_aligned_rect():
    r = Rect(cx=50.0, cy=40.0, w=20.0, h=10.0, angle=0.0)
    x, y, w, h = r.axis_aligned_bounds()
    assert (x, y, w, h) == pytest.approx((40.0, 35.0, 20.0, 10.0))


def test_bbox_overlap_frac():
    r = Rect(cx=100.0, cy=100.0, w=100.0, h=100.0, angle=0.0)  # 50..150
    assert bbox_overlap_frac({"x": 60, "y": 60, "w": 20, "h": 20}, r) == 1.0
    assert bbox_overlap_frac({"x": 0, "y": 0, "w": 10, "h": 10}, r) == 0.0
    half = bbox_overlap_frac({"x": 40, "y": 60, "w": 20, "h": 20}, r)
    assert half == pytest.approx(0.5)


def test_transform_json_round_trip():
    t = transform_for(Rect(cx=1.0, cy=2.0, w=30.0, h=40.0, angle=2.5),
                      src_w=100, src_h=120, deskew=True, crop=True, inset_px=2.0)
    again = Transform.from_json(t.to_json())
    assert again.m == pytest.approx(t.m)
    assert (again.out_w, again.out_h) == (t.out_w, t.out_h)
    assert again.angle_deg == pytest.approx(t.angle_deg)


def test_rect_scaled_matches_an_analysis_downscale():
    small = Rect(cx=100.0, cy=80.0, w=180.0, h=140.0, angle=2.0)
    full = small.scaled(4.0)
    assert (full.cx, full.cy, full.w, full.h) == (400.0, 320.0, 720.0, 560.0)
    assert full.angle == small.angle
    assert full.aspect == pytest.approx(small.aspect)


def test_non_invertible_transform_raises():
    t = Transform(m=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0), src_w=10, src_h=10,
                  out_w=10, out_h=10)
    with pytest.raises(ValueError):
        t.invert()
