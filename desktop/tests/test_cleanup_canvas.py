"""Fix-up 4: a deskew straightens the picture, it does not grow it.

Rotating an image needs a canvas big enough to hold the corners, so
`Transform.build` expands one. With *crop* unticked that expanded canvas was
what got kept, and the accepted file came out larger than the scan it was made
from — up to +47 % in area on a 13.5 deg print, bed-filled around the edges.
109 live proposals would have rendered that way.

The rule: for anything that is not a split region, the output must fit inside
the source in both axes. A skipped crop now clips back to a source-sized
window centred on the rotated canvas — same framing, straightened, bed only
where the rotation pulled it in.

The clipping must not quietly undo the rotation, so the straightening is
measured again afterwards rather than assumed.
"""
from __future__ import annotations

import numpy as np
import pytest

from photoarchive.modes.cleanup import analyse as analyse_mod
from photoarchive.modes.cleanup import render as render_mod
from photoarchive.modes.cleanup.geometry import Rect, transform_for

from .test_cleanup_analyse import _settings, make_scan


# --------------------------------------------------------------------------
# The rule, stated over the whole space
# --------------------------------------------------------------------------

@pytest.mark.parametrize("angle", [-14.0, -13.5, -6.0, -0.4, 0.0, 0.4, 6.0,
                                   13.5, 14.0])
@pytest.mark.parametrize("src", [(1920, 1458), (1502, 1940), (3510, 2357),
                                 (746, 1038)])
@pytest.mark.parametrize("deskew,crop", [(True, False), (True, True),
                                         (False, True)])
def test_no_ticked_combination_renders_larger_than_the_source(
        angle, src, deskew, crop):
    src_w, src_h = src
    rect = Rect(cx=src_w / 2.0, cy=src_h / 2.0,
                w=src_w * 0.92, h=src_h * 0.92, angle=angle)
    t = transform_for(rect, src_w=src_w, src_h=src_h,
                      deskew=deskew, crop=crop, inset_px=2.0)
    assert t.out_w <= src_w, (t.out_w, src_w, angle, deskew, crop)
    assert t.out_h <= src_h, (t.out_h, src_h, angle, deskew, crop)
    assert t.out_w >= 1 and t.out_h >= 1


def test_a_deskew_without_a_crop_keeps_the_source_size(tmp_path):
    """Not merely "no bigger" — the whole frame is still there, straightened.
    Clipping to something smaller would silently crop a photo the reviewer
    explicitly asked not to crop."""
    src_w, src_h = 1600, 1200
    rect = Rect(cx=800.0, cy=600.0, w=1400.0, h=1000.0, angle=5.0)
    t = transform_for(rect, src_w=src_w, src_h=src_h, deskew=True, crop=False)
    assert (t.out_w, t.out_h) == (src_w, src_h)


def test_a_guarded_rect_that_reaches_the_scan_edge_still_fits():
    """Fix-up 3's guard can push an edge out to the scan boundary. Rotating
    that rect must not turn it into a larger file."""
    src_w, src_h = 1200, 900
    rect = Rect(cx=600.0, cy=450.0, w=1200.0, h=900.0, angle=4.0)
    t = transform_for(rect, src_w=src_w, src_h=src_h, deskew=True, crop=True,
                      inset_px=0.0)
    assert t.out_w <= src_w and t.out_h <= src_h, (t.out_w, t.out_h)


# --------------------------------------------------------------------------
# Through the plan the review pane actually builds
# --------------------------------------------------------------------------

def _operations(tmp_path, **overrides):
    path, _rects = make_scan(tmp_path / "canvas.jpg", angle=6.0, print_frac=0.6)
    settings = _settings(tmp_path)
    result = analyse_mod.analyse_photo(settings, photo_id=1, working_path=path)
    return path, settings, result


def test_plan_from_never_grows_for_any_tick_set(tmp_path):
    path, settings, result = _operations(tmp_path)
    ops = result.operations
    src_w, src_h = result.src_w, result.src_h
    available = sorted((ops.get("ops") or {}).keys())
    assert "deskew" in available, available

    # Every subset of what was proposed, including the one that caused this.
    subsets = [(), ("deskew",), ("crop",), ("deskew", "crop")]
    for ticked in subsets:
        if any(t not in available for t in ticked):
            continue
        plan = render_mod.plan_from(ops, ticked, settings=settings,
                                    src_w=src_w, src_h=src_h)
        assert plan.transform.out_w <= src_w, (ticked, plan.transform.out_w)
        assert plan.transform.out_h <= src_h, (ticked, plan.transform.out_h)


def test_the_rendered_file_is_no_larger_than_the_scan(tmp_path):
    """The invariant where it counts: on disk, in pixels."""
    path, settings, result = _operations(tmp_path)
    plan = render_mod.plan_from(result.operations, ("deskew",),
                                settings=settings,
                                src_w=result.src_w, src_h=result.src_h)
    out = render_mod.render_full(path, plan, tmp_path / "out.jpg",
                                 operations=result.operations)
    assert out.width <= result.src_w
    assert out.height <= result.src_h


def test_clipping_the_canvas_does_not_undo_the_straightening(tmp_path):
    """The fix moves the output window; it must not cancel the rotation.
    Measured, not assumed — a sign slip here would look exactly like a pass."""
    angle = 6.0
    path, _rects = make_scan(tmp_path / "tilt.jpg", angle=angle, print_frac=0.6)
    settings = _settings(tmp_path)
    result = analyse_mod.analyse_photo(settings, photo_id=1, working_path=path)
    plan = render_mod.plan_from(result.operations, ("deskew",),
                                settings=settings,
                                src_w=result.src_w, src_h=result.src_h)
    out = render_mod.render_full(path, plan, tmp_path / "straight.jpg",
                                 operations=result.operations)
    assert out.width <= result.src_w and out.height <= result.src_h

    from PIL import Image
    arr = np.asarray(Image.open(out.path).convert("RGB"))
    _bed, comps = analyse_mod.detect_bed_and_prints(arr, settings)
    assert comps, "the print should still be findable after the deskew"
    residual = (comps[0].edges.angle if comps[0].edges is not None
                else comps[0].rect.angle)
    assert abs(residual) < abs(angle) - 1.0, (
        f"residual tilt {residual:+.2f} on a print that was {angle:+.2f}")


def test_a_split_region_is_the_one_exemption(tmp_path):
    """A split child is cut from a region of the scan, so it is smaller than
    the source by construction — the rule is about whole-scan renders, and
    the region path must keep working."""
    settings = _settings(tmp_path)
    path, _rects = make_scan(tmp_path / "two.jpg", prints=2, angle=2.0,
                             print_frac=0.18)
    result = analyse_mod.analyse_photo(settings, photo_id=1, working_path=path)
    regions = result.split_regions or []
    assert len(regions) == 2, result.operations.get("ops")

    for region in regions:
        # The transform the analyser stored on the region…
        stored = region["transform"]
        assert stored["out_w"] <= result.src_w
        assert stored["out_h"] <= result.src_h
        # …and the one the review pane rebuilds from the region rect.
        plan = render_mod.plan_from(result.operations, ("deskew", "crop"),
                                    settings=settings,
                                    src_w=result.src_w, src_h=result.src_h,
                                    region_rect=region["rect"])
        assert plan.transform.out_w <= result.src_w
        assert plan.transform.out_h <= result.src_h
        # A child really is a piece of the scan, not the whole of it.
        assert plan.transform.out_w < result.src_w
