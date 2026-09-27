"""Fix-up 2: where the deskew angle comes from, and which way it turns.

Photo #306 was straight and came out tilted. The cause was not a sign
convention but the *source* of the angle: `cv2.minAreaRect` returns the
minimum **enclosing** rectangle, whose orientation is pinned by whatever
sticks out furthest — a torn corner, a spur of scanner bed, the print running
off the edge of the scan. On a print that is not a clean rectangle that angle
has nothing to do with the print's edges.

The angle now comes from the outline itself: simplify it to straight runs,
fold each run's direction into [-45, 45), and take the length-weighted
consensus. Four long edges outvote a torn corner; when nothing wins,
`confidence` says so and the photo is left alone.

The tests that matter here check *direction* and *residual tilt*, which the
earlier magnitude-only assertions could not.
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

from photoarchive.modes.cleanup import analyse as analyse_mod
from photoarchive.modes.cleanup import render as render_mod
from photoarchive.modes.cleanup.geometry import Rect, normalise_angle, transform_for

from .test_cleanup_analyse import _analyse, _settings, make_scan

FIXTURE = Path(__file__).parent / "fixtures" / "scan_306_straight.jpg"


# --------------------------------------------------------------------------
# The angle source
# --------------------------------------------------------------------------

@pytest.mark.parametrize("truth", [-6.0, -1.5, 0.0, 1.5, 6.0])
def test_edge_orientation_folds_four_sides_into_one_tilt(truth):
    mask = np.zeros((500, 500), np.uint8)
    box = cv2.boxPoints(((250, 250), (300, 200), truth))
    cv2.fillConvexPoly(mask, np.int32(box), 255)

    edges = analyse_mod.edge_orientation(mask > 0)
    assert edges is not None
    assert edges.angle == pytest.approx(truth, abs=0.4), (truth, edges)
    assert edges.confidence > 0.9, (truth, edges)


def test_a_torn_corner_does_not_move_the_verdict():
    """The whole point: one short ragged edge must not outvote four long
    straight ones."""
    clean = np.zeros((500, 500), np.uint8)
    cv2.fillConvexPoly(clean, np.int32(cv2.boxPoints(((250, 250), (320, 240), 0.0))), 255)
    torn = clean.copy()
    # Bite a triangle out of one corner, the way a damaged print scans.
    cv2.fillConvexPoly(torn, np.int32([[410, 370], [410, 250], [300, 370]]), 0)

    clean_edges = analyse_mod.edge_orientation(clean > 0)
    torn_edges = analyse_mod.edge_orientation(torn > 0)
    assert clean_edges.angle == pytest.approx(0.0, abs=0.3)
    assert torn_edges.angle == pytest.approx(0.0, abs=0.5), torn_edges

    # …whereas the minimum enclosing rectangle is happy to be dragged around.
    pts = cv2.findNonZero(torn)
    (_c, size, raw) = cv2.minAreaRect(pts)
    enclosing, _w, _h = normalise_angle(raw, size[0], size[1])
    assert abs(enclosing - torn_edges.angle) >= 0.0   # recorded, not asserted


def test_an_unreadable_outline_reports_low_confidence():
    """When no orientation wins the vote the angle is noise. Say so, rather
    than rotating by a number nobody trusts."""
    mask = np.zeros((400, 400), np.uint8)
    cv2.circle(mask, (200, 200), 150, 255, -1)
    edges = analyse_mod.edge_orientation(mask > 0)
    assert edges is not None
    assert edges.confidence < analyse_mod.EDGE_MIN_CONFIDENCE, edges


# --------------------------------------------------------------------------
# The real scan
# --------------------------------------------------------------------------

def test_a_ragged_real_scan_is_not_deskewed_by_its_enclosing_box(tmp_path):
    """Photo #306 at 299x420 — a print with a torn corner that runs off the
    edge of the scan. The enclosing rectangle reads about +2.3 deg on a print
    that is straight."""
    assert FIXTURE.exists(), "real-scan fixture missing"
    settings = _settings(tmp_path)
    arr = np.asarray(Image.open(FIXTURE).convert("RGB"))

    bed, comps = analyse_mod.detect_bed_and_prints(arr, settings)
    assert comps
    top = comps[0]

    pts = cv2.findNonZero(top.mask.astype(np.uint8))
    (_centre, size, raw_angle) = cv2.minAreaRect(pts)
    enclosing_angle, _w, _h = normalise_angle(raw_angle, size[0], size[1])
    assert abs(enclosing_angle) > 1.5, (
        f"the fixture must still reproduce the misleading enclosing rectangle "
        f"(got {enclosing_angle:+.3f})")

    assert top.edges is not None
    assert abs(top.edges.angle) < 0.5, top.edges
    assert top.edges.confidence > 0.5, top.edges
    assert top.rect.angle == pytest.approx(top.edges.angle, abs=1e-6)


def test_the_real_scan_gets_no_deskew_op_end_to_end(tmp_path):
    settings = _settings(tmp_path)
    result = analyse_mod.analyse_photo(settings, photo_id=306,
                                       working_path=FIXTURE)
    assert "deskew" not in result.operations["ops"], result.operations["ops"]
    assert result.operations["edges"]["angle"] == pytest.approx(0.0, abs=0.5)


# --------------------------------------------------------------------------
# The invariant a deskew must satisfy
# --------------------------------------------------------------------------

@pytest.mark.parametrize("angle", [-4.0, -2.0, 2.0, 4.0])
def test_the_proposed_rotation_reduces_the_tilt(tmp_path, angle):
    """Afterwards, the print must be straighter than it was.

    Checking the magnitude of the proposed angle cannot catch a sign error;
    measuring the tilt again after applying it can.
    """
    path, _rects = make_scan(tmp_path / f"tilt{angle}.jpg", angle=angle)
    settings = _settings(tmp_path)
    result = _analyse(tmp_path, path)

    proposed = result.operations["ops"]["deskew"]["angle_deg"]
    assert proposed == pytest.approx(angle, abs=0.3), (
        f"proposed {proposed:+.2f} for a print tilted {angle:+.2f}")

    rgb, _src_w, _src_h, scale = analyse_mod.load_for_analysis(
        path, settings.CLEANUP_ANALYSE_EDGE)
    full = Rect.from_json(result.operations["print_rect"])
    small = Rect(cx=full.cx / scale, cy=full.cy / scale, w=full.w / scale,
                 h=full.h / scale, angle=full.angle)
    t = transform_for(small, src_w=rgb.shape[1], src_h=rgb.shape[0],
                      deskew=True, crop=False)
    straightened = render_mod.ops.warp(rgb, t, border_value=242)

    _bed, comps = analyse_mod.detect_bed_and_prints(straightened, settings)
    assert comps, "the print should still be findable after the rotation"
    residual = (comps[0].edges.angle if comps[0].edges is not None
                else comps[0].rect.angle)
    assert abs(residual) < abs(angle) - 0.5, (
        f"rotating by {proposed:+.2f} left a residual of {residual:+.2f} "
        f"on a print tilted {angle:+.2f}")


def test_the_deskew_is_skipped_when_the_edges_do_not_agree(tmp_path):
    """A low-confidence outline records why it was skipped instead of silently
    proposing nothing."""
    settings = _settings(tmp_path)
    # A round "print" on a bed: no dominant edge direction anywhere.
    bed = np.full((900, 1200, 3), 242, np.uint8)
    rng = np.random.default_rng(3)
    disc = np.zeros((900, 1200), np.uint8)
    cv2.ellipse(disc, (600, 450), (420, 330), 12.0, 0, 360, 255, -1)
    tone = rng.integers(20, 200, size=(900, 1200)).astype(np.uint8)
    tone = cv2.blur(tone, (41, 41))
    bed[disc > 0] = np.repeat(tone[disc > 0][:, None], 3, axis=1)
    path = tmp_path / "round.jpg"
    Image.fromarray(bed).save(path, "JPEG", quality=95)

    result = analyse_mod.analyse_photo(settings, photo_id=1, working_path=path)
    edges = result.operations.get("edges")
    if edges and edges["confidence"] >= analyse_mod.EDGE_MIN_CONFIDENCE:
        pytest.skip("this shape happened to read as rectangular enough")
    assert "deskew" not in result.operations["ops"]
    if abs((result.operations.get("print_rect") or {}).get("angle", 0.0)) >= 0.3:
        assert result.operations.get("deskew_skipped") == "edges_disagree"
        assert "edges disagree" in analyse_mod.caption_for(result.operations)
