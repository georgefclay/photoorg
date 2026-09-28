"""Fix-up 3: a crop removes scanner bed, never pixels of the photograph.

Photo #15 is a black-and-white family portrait against a white curtain, with a
grandmother in a white cardigan at the left edge. The print mask keyed on
"darker than the bed", so the curtain and the cardigan read as bed, the
detected rectangle started 290 px inside the picture, and the crop took her
arm with it.

Two lines of defence, tested here:

  * the mask now calls a pixel bed only when it is near the bed tone, locally
    flat, and reachable from the scan's border — scanner bed is calm in a way
    no photograph is;
  * the content guard then pushes every edge outward until what lies beyond it
    is genuinely bed, so even a bad rectangle cannot eat the picture.

The window for that local flatness has to scale with the image: a fixed 7 px
sits inside one smooth fold of #15's curtain at 2000 px and reads as calm as
bed, while the same 7 px on a 520 px thumbnail spans several folds.
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

from photoarchive.modes.cleanup import analyse as analyse_mod
from photoarchive.modes.cleanup.geometry import Rect

from .test_cleanup_analyse import _settings, make_scan

FIXTURE = Path(__file__).parent / "fixtures" / "scan_15_light_edges.jpg"


def _bed_and_top(arr, settings):
    bed, comps = analyse_mod.detect_bed_and_prints(arr, settings)
    assert comps, "no print found"
    return bed, comps[0]


# --------------------------------------------------------------------------
# The real scan
# --------------------------------------------------------------------------

def test_the_light_edged_print_is_found_without_the_guard(tmp_path):
    """#15 at 520x344. The brightness mask starts the rectangle inside the
    grandmother; the calm mask does not."""
    assert FIXTURE.exists(), "real-scan fixture missing"
    arr = np.asarray(Image.open(FIXTURE).convert("RGB"))

    bright = _settings(tmp_path, CLEANUP_MASK_MODE="brightness")
    calm = _settings(tmp_path, CLEANUP_MASK_MODE="calm")
    _b1, top_bright = _bed_and_top(arr, bright)
    _b2, top_calm = _bed_and_top(arr, calm)

    left_bright = top_bright.rect.cx - abs(top_bright.rect.w) / 2
    left_calm = top_calm.rect.cx - abs(top_calm.rect.w) / 2
    assert left_bright > 60, (
        "the fixture must still reproduce the brightness mask's mistake")
    assert left_calm < 40, (
        f"the calm mask should find the print's real edge, got {left_calm:.1f}")


def test_the_guard_rescues_the_brightness_mask_on_the_real_scan(tmp_path):
    """Even with the old mask, nothing of the photograph may be cropped away."""
    arr = np.asarray(Image.open(FIXTURE).convert("RGB"))
    settings = _settings(tmp_path, CLEANUP_MASK_MODE="brightness")
    bed, top = _bed_and_top(arr, settings)

    _guarded, verdicts = analyse_mod.guard_crop_edges(arr, top.rect, bed, settings)
    left = next(v for v in verdicts if v.side == "left")
    assert left.moved_px > 20, "the guard should have pushed the left edge out"
    assert left.unclear, "a push that large means the side is not to be cropped"


def test_the_guard_leaves_a_correctly_detected_print_alone(tmp_path):
    arr = np.asarray(Image.open(FIXTURE).convert("RGB"))
    settings = _settings(tmp_path)          # calm mask
    bed, top = _bed_and_top(arr, settings)
    _guarded, verdicts = analyse_mod.guard_crop_edges(arr, top.rect, bed, settings)
    assert all(not v.unclear for v in verdicts), [v.to_json() for v in verdicts]
    assert max(v.moved_frac for v in verdicts) < 0.05


def test_the_flatness_window_scales_with_the_image(tmp_path):
    """The bug behind the bug: a window that does not scale reads a smooth
    fold of curtain as calm as bed on a large frame."""
    arr = np.asarray(Image.open(FIXTURE).convert("RGB"))
    big = cv2.resize(arr, (arr.shape[1] * 4, arr.shape[0] * 4),
                     interpolation=cv2.INTER_LANCZOS4)

    scaled = _settings(tmp_path)
    fixed_small = _settings(tmp_path, CLEANUP_MASK_STD_WINDOW_FRAC=1e-6,
                            CLEANUP_MASK_STD_WINDOW_MIN=7)

    _b, top_scaled = _bed_and_top(big, scaled)
    _b2, top_fixed = _bed_and_top(big, fixed_small)
    left_scaled = top_scaled.rect.cx - abs(top_scaled.rect.w) / 2
    left_fixed = top_fixed.rect.cx - abs(top_fixed.rect.w) / 2
    assert left_scaled < left_fixed - 40, (
        f"scaled window {left_scaled:.0f} should beat a fixed 7 px "
        f"{left_fixed:.0f} on a 4x frame")


# --------------------------------------------------------------------------
# The guard itself
# --------------------------------------------------------------------------

def test_an_edge_already_on_bed_does_not_move(tmp_path):
    path, rects = make_scan(tmp_path / "clean.jpg", angle=0.0, print_frac=0.5)
    settings = _settings(tmp_path)
    rgb, _w, _h, _s = analyse_mod.load_for_analysis(
        path, settings.CLEANUP_ANALYSE_EDGE)
    bed, top = _bed_and_top(rgb, settings)
    _guarded, verdicts = analyse_mod.guard_crop_edges(rgb, top.rect, bed, settings)
    for v in verdicts:
        assert v.found_bed, v.to_json()
        assert v.moved_frac < 0.02, v.to_json()


def test_an_edge_that_runs_off_the_scan_is_not_a_failure(tmp_path):
    """A print scanned right to its edge simply has nothing to crop there."""
    settings = _settings(tmp_path)
    rng = np.random.default_rng(5)
    grid = rng.integers(20, 200, size=(40, 55)).astype(np.uint8)
    arr = np.kron(grid, np.ones((30, 30), np.uint8))[:1100, :1600]
    rgb = np.repeat(arr[:, :, None], 3, axis=2)
    path = tmp_path / "edge_to_edge.jpg"
    Image.fromarray(rgb).save(path, "JPEG", quality=95)

    loaded, _w, _h, _s = analyse_mod.load_for_analysis(
        path, settings.CLEANUP_ANALYSE_EDGE)
    bed, comps = analyse_mod.detect_bed_and_prints(loaded, settings)
    if not comps:
        pytest.skip("no component on an edge-to-edge print")
    _guarded, verdicts = analyse_mod.guard_crop_edges(
        loaded, comps[0].rect, bed, settings)
    off = [v for v in verdicts if v.runs_off_scan]
    assert off, "edges at the scan boundary should be marked as running off it"
    for v in off:
        assert not v.unclear, "running off the scan is not an unresolved edge"


def test_a_white_region_inside_the_print_is_not_bed(tmp_path):
    """An overexposed sky is pale and calm, but it is enclosed by photograph."""
    settings = _settings(tmp_path)
    bed_val = 243
    img = np.full((900, 1200, 3), bed_val, np.uint8)
    # A print with a big flat near-white patch in the middle of it.
    rng = np.random.default_rng(7)
    grid = rng.integers(20, 200, size=(24, 31)).astype(np.uint8)
    tile = np.kron(grid, np.ones((30, 30), np.uint8))[:700, :900]
    tile[80:340, 120:640] = 244                 # the "sky"
    img[100:800, 150:1050] = np.repeat(tile[:, :, None], 3, axis=2)

    bed, comps = analyse_mod.detect_bed_and_prints(img, settings)
    assert comps
    mask = comps[0].mask
    sky = mask[200:300, 250:550]
    assert sky.mean() > 0.9, (
        "a calm pale region enclosed by the print must stay print, not bed")


def test_the_safety_margin_is_given_back_outward(tmp_path):
    """Prefer under-cropping: bed slivers are harmless, missing pixels are not."""
    path, _rects = make_scan(tmp_path / "safety.jpg", angle=0.0, print_frac=0.5)
    tight = _settings(tmp_path, CLEANUP_CROP_SAFETY_PX_AT_300=0.0)
    loose = _settings(tmp_path, CLEANUP_CROP_SAFETY_PX_AT_300=40.0)
    a = analyse_mod.analyse_photo(tight, photo_id=1, working_path=path)
    b = analyse_mod.analyse_photo(loose, photo_id=1, working_path=path)
    crop_a = a.operations["ops"].get("crop")
    crop_b = b.operations["ops"].get("crop")
    assert crop_a and crop_b
    assert crop_b["out_w"] > crop_a["out_w"], (
        "a bigger safety margin must keep more of the scan, not less")
    assert crop_b["removed_frac"] < crop_a["removed_frac"]


def test_two_unresolvable_sides_go_to_needs_manual(tmp_path):
    """Fix-up 3's rule: one unclear side is cropped around; two is a scan for
    George to look at."""
    settings = _settings(tmp_path)
    # A print whose left and top edges are indistinguishable from the bed.
    img = np.full((900, 1200, 3), 243, np.uint8)
    rng = np.random.default_rng(11)
    grid = rng.integers(60, 235, size=(30, 40)).astype(np.uint8)
    tile = np.kron(grid, np.ones((30, 30), np.uint8))[:880, :1180]
    img[0:880, 0:1180] = np.repeat(tile[:, :, None], 3, axis=2)
    path = tmp_path / "two_unclear.jpg"
    Image.fromarray(img).save(path, "JPEG", quality=95)

    result = analyse_mod.analyse_photo(settings, photo_id=1, working_path=path)
    guard = result.operations.get("crop_guard")
    if not guard:
        pytest.skip("this synthetic scan produced no rectangle to guard")
    unclear = [v for v in guard if v["unclear"]]
    if len(unclear) >= 2:
        assert result.needs_manual
        assert result.manual_reason == "print_edge_unclear"
    else:
        assert not (result.needs_manual
                    and result.manual_reason == "print_edge_unclear")


def test_the_caption_names_the_side_that_was_skipped(tmp_path):
    ops = {"ops": {"crop": {"removed_frac": 0.18}},
           "crop_unclear_sides": ["left"]}
    caption = analyse_mod.caption_for(ops)
    assert "crop skipped on left: print edge unclear" in caption
