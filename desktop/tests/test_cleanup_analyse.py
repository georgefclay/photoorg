"""Phase 7 analysis on synthetic scans.

Each case builds a scan the way a flatbed would produce it — a print on a
white or black bed — and checks the analyser measures what it should. No
database; `analyse_photo` only needs a file.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from photoarchive.config import Settings
from photoarchive.modes.cleanup import analyse as analyse_mod
from photoarchive.modes.cleanup import ops
from photoarchive.modes.cleanup.geometry import Rect


def _settings(tmp_path: Path, **overrides) -> Settings:
    kwargs = dict(
        MASTER_ROOTS="dummy=Z:\\",
        WORKING_DIR=tmp_path / "working",
        QUARANTINE_DIR=tmp_path / "quarantine",
        MANUAL_FIX_DIR=tmp_path / "manual-fix",
        THUMBS_DIR=tmp_path / "thumbs",
        CLEANUP_DIR=tmp_path / "cleanup",
        DATABASE_URL="postgresql://x/y",
        INFERENCE_URL="http://x", INFERENCE_TOKEN="x",
        WEB_API_URL="http://x", WEB_API_TOKEN="x",
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


# --------------------------------------------------------------------------
# Synthetic scans
# --------------------------------------------------------------------------

def _print_content(w: int, h: int, *, base=(150, 130, 110), seed=7) -> np.ndarray:
    """A print with enough structure to look like a photograph: a gradient, a
    dark band, a light band, and patches of genuinely different hue.

    The hue spread matters — the analyser treats a single-hue image as sepia (a
    deliberate look) and refuses to "correct" it, so a colour-cast fixture has
    to start from something actually colourful.
    """
    rng = np.random.default_rng(seed)
    arr = np.zeros((h, w, 3), dtype=np.float32)
    grad = np.linspace(0.55, 1.35, w, dtype=np.float32)[None, :, None]
    arr += np.array(base, dtype=np.float32)[None, None, :] * grad
    # A dark band and a light band so the histogram has real ends.
    arr[int(h * 0.15):int(h * 0.3), int(w * 0.1):int(w * 0.6)] *= 0.35
    arr[int(h * 0.6):int(h * 0.8), int(w * 0.3):int(w * 0.9)] *= 1.55
    # Sky, foliage and a red coat: three distinct hues.
    arr[0:int(h * 0.30), :] = np.array([95, 135, 190], np.float32)
    arr[int(h * 0.80):, 0:int(w * 0.55)] = np.array([80, 140, 70], np.float32)
    arr[int(h * 0.40):int(h * 0.60), int(w * 0.65):int(w * 0.90)] =         np.array([185, 70, 60], np.float32)
    arr += rng.normal(0.0, 4.0, size=arr.shape).astype(np.float32)
    return np.clip(arr, 0, 255).astype(np.uint8)


def _with_full_range(arr: np.ndarray) -> np.ndarray:
    """Give the print a real black and a near-white, in bands wide enough to
    survive the 0.5 / 99.5 percentiles and inside the interior the tone
    statistics are measured over (8 % is trimmed off each edge).

    The light band stops at 215, not 253: a bed-white band would be masked
    *out* as bed and cut the print into two components, which the analyser
    would then read as a two-print scan.
    """
    h = arr.shape[0]
    arr = arr.copy()
    arr[int(h * 0.44):int(h * 0.48), :] = 2
    arr[int(h * 0.50):int(h * 0.54), :] = 215
    return arr


def _neutral_print(w: int, h: int, seed: int = 11, block: int = 40) -> np.ndarray:
    """A print with nothing to fix: grey (so no cast), a full tonal range (so
    no levels), one solid component (so no split).

    The tones come in blocks, not per-pixel noise — the analyser works on a
    2000 px downscale, and per-pixel noise averages away to mid-grey there,
    which would read as a faded print.
    """
    rng = np.random.default_rng(seed)
    gh, gw = max(1, h // block + 1), max(1, w // block + 1)
    grid = rng.integers(2, 216, size=(gh, gw), dtype=np.int16).astype(np.uint8)
    grey = np.kron(grid, np.ones((block, block), np.uint8))[:h, :w]
    return np.repeat(grey[:, :, None], 3, axis=2)


def make_scan(
    path: Path,
    *,
    bed: str = "white",
    angle: float = 0.0,
    print_frac: float = 0.55,
    scan_size: tuple[int, int] = (1600, 1200),
    content: np.ndarray | None = None,
    prints: int = 1,
    print_size: tuple[int, int] | None = None,
) -> tuple[Path, list[Rect]]:
    """Paste one or more rotated prints onto a scanner bed.

    Returns the path and the ground-truth rects (centre, size, angle) in the
    scan's own pixels.
    """
    import cv2

    W, H = scan_size
    bed_value = 242 if bed == "white" else 12
    canvas = np.full((H, W, 3), bed_value, dtype=np.uint8)
    # A little bed noise, so thresholding is not trivially exact.
    canvas = np.clip(
        canvas.astype(np.int16)
        + np.random.default_rng(3).integers(-3, 4, size=canvas.shape),
        0, 255,
    ).astype(np.uint8)

    rects: list[Rect] = []
    if print_size is not None:
        base = (float(print_size[0]), float(print_size[1]))
    else:
        side = math.sqrt(print_frac * W * H / 1.25)
        base = (side * 1.25, side)
    if prints == 1:
        centres = [(W / 2, H / 2)]
    else:
        centres = [(W * 0.27, H / 2), (W * 0.73, H / 2)]
    sizes = [base] * len(centres)

    for (cx, cy), (pw, ph) in zip(centres, sizes):
        pw, ph = int(round(pw)), int(round(ph))
        tile = content if content is not None else _print_content(pw, ph)
        if tile.shape[:2] != (ph, pw):
            tile = cv2.resize(tile, (pw, ph), interpolation=cv2.INTER_AREA)
        # Rotate the tile on a transparent-ish canvas and paste it.
        m = cv2.getRotationMatrix2D((pw / 2, ph / 2), -angle, 1.0)
        rot_w = int(math.ceil(abs(pw * math.cos(math.radians(angle)))
                              + abs(ph * math.sin(math.radians(angle)))))
        rot_h = int(math.ceil(abs(pw * math.sin(math.radians(angle)))
                              + abs(ph * math.cos(math.radians(angle)))))
        m[0, 2] += rot_w / 2 - pw / 2
        m[1, 2] += rot_h / 2 - ph / 2
        rotated = cv2.warpAffine(tile, m, (rot_w, rot_h),
                                 borderValue=(bed_value,) * 3)
        mask = cv2.warpAffine(np.full((ph, pw), 255, np.uint8), m,
                              (rot_w, rot_h), borderValue=0)
        x0 = int(round(cx - rot_w / 2))
        y0 = int(round(cy - rot_h / 2))
        # Clip both ways: a big or rotated print may overhang the bed.
        sx0, sy0 = max(0, -x0), max(0, -y0)
        dx0, dy0 = max(0, x0), max(0, y0)
        cw = min(rot_w - sx0, W - dx0)
        ch = min(rot_h - sy0, H - dy0)
        region = canvas[dy0:dy0 + ch, dx0:dx0 + cw]
        sub_mask = mask[sy0:sy0 + ch, sx0:sx0 + cw]
        sub_rot = rotated[sy0:sy0 + ch, sx0:sx0 + cw]
        region[sub_mask > 127] = sub_rot[sub_mask > 127]
        rects.append(Rect(cx=x0 + rot_w / 2, cy=y0 + rot_h / 2,
                          w=float(pw), h=float(ph), angle=float(angle)))

    Image.fromarray(canvas).save(path, "JPEG", quality=96, subsampling=0)
    return path, rects


def _analyse(tmp_path: Path, path: Path, **setting_overrides):
    settings = _settings(tmp_path, **setting_overrides)
    return analyse_mod.analyse_photo(settings, photo_id=1, working_path=path)


# --------------------------------------------------------------------------
# Deskew and crop
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bed", ["white", "black"])
def test_three_degree_skew_is_measured_within_a_third_of_a_degree(tmp_path, bed):
    path, rects = make_scan(tmp_path / f"scan_{bed}.jpg", bed=bed, angle=3.0)
    result = _analyse(tmp_path, path)

    assert result.status == "pending"
    assert not result.needs_manual, result.manual_reason
    assert result.operations["bed"]["kind"] == bed
    deskew = result.operations["ops"]["deskew"]
    assert abs(abs(deskew["angle_deg"]) - 3.0) <= 0.3, deskew


@pytest.mark.parametrize("bed", ["white", "black"])
def test_crop_lands_within_three_pixels_of_the_print(tmp_path, bed):
    path, rects = make_scan(tmp_path / f"crop_{bed}.jpg", bed=bed, angle=3.0)
    result = _analyse(tmp_path, path)

    crop = result.operations["ops"]["crop"]
    truth = rects[0]
    # Minus the 4 px inset on each side (300 DPI base, unknown DPI here).
    assert crop["out_w"] == pytest.approx(truth.w - 8, abs=3)
    assert crop["out_h"] == pytest.approx(truth.h - 8, abs=3)


def test_skew_under_the_threshold_produces_no_deskew_op(tmp_path):
    path, _ = make_scan(tmp_path / "straight.jpg", angle=0.0)
    result = _analyse(tmp_path, path)
    assert "deskew" not in result.operations["ops"]


def test_a_print_filling_the_scan_produces_no_crop_op(tmp_path):
    # A real scan is thousands of pixels wide, so the 4 px inset is well
    # under the 1 % the crop op needs to be worth a new file version.
    path, _ = make_scan(tmp_path / "full.jpg", print_size=(3992, 2992),
                        angle=0.0, scan_size=(4000, 3000))
    result = _analyse(tmp_path, path)
    assert "crop" not in result.operations["ops"], result.operations["ops"]


# --------------------------------------------------------------------------
# Multi-print split
# --------------------------------------------------------------------------

def test_two_prints_on_one_bed_become_two_regions(tmp_path):
    path, rects = make_scan(tmp_path / "two.jpg", prints=2, print_frac=0.20,
                            angle=0.0, scan_size=(2000, 1200))
    result = _analyse(tmp_path, path)

    assert result.split_regions is not None, result.operations
    assert len(result.split_regions) == 2
    assert result.operations["ops"]["split"]["regions"] == 2
    got = sorted(r["rect"]["cx"] for r in result.split_regions)
    want = sorted(r.cx for r in rects)
    for g, w in zip(got, want):
        assert g == pytest.approx(w, abs=30)


def test_a_multi_print_scan_is_not_called_print_too_small(tmp_path):
    """Three prints on one bed means the largest covers only a third of it.
    That is the analyser understanding the scan, not failing to find a print,
    so the whole-scan size gate must not fire — the per-region gates already
    judged every print."""
    path, rects = make_scan(tmp_path / "three.jpg", prints=2, print_frac=0.16,
                            angle=0.0, scan_size=(2400, 1200))
    result = _analyse(tmp_path, path)

    assert result.split_regions is not None, result.operations
    assert not result.needs_manual, result.manual_reason
    assert result.manual_reason is None
    # Every region is well under CLEANUP_MIN_PRINT_FRAC on its own.
    assert all(r["area_frac"] < 0.40 for r in result.split_regions)
    # The combined figure is recorded so the report can show it.
    assert result.operations["print_frac_all"] == pytest.approx(
        sum(r["area_frac"] for r in result.split_regions), abs=0.01)


def test_a_multi_print_scan_still_fails_on_a_wildly_skewed_region(tmp_path):
    """The per-region aspect and skew checks survive the exemption."""
    path, _ = make_scan(tmp_path / "three_wonky.jpg", prints=2, print_frac=0.16,
                        angle=30.0, scan_size=(2400, 1200))
    result = _analyse(tmp_path, path, CLEANUP_MAX_DESKEW_DEG=15.0)
    if result.split_regions:
        assert result.needs_manual
        assert result.manual_reason in ("skew_too_large", "implausible_aspect")


def test_a_scan_with_a_back_is_never_auto_split(tmp_path):
    path, _ = make_scan(tmp_path / "two_back.jpg", prints=2, print_frac=0.20,
                        angle=0.0, scan_size=(2000, 1200))
    settings = _settings(tmp_path)
    result = analyse_mod.analyse_photo(
        settings, photo_id=1, working_path=path, has_back=True,
    )
    assert result.needs_manual
    assert result.manual_reason == "has_back"
    assert result.split_regions is None


# --------------------------------------------------------------------------
# needs_manual
# --------------------------------------------------------------------------

def test_a_tiny_print_is_needs_manual_not_a_guess(tmp_path):
    path, _ = make_scan(tmp_path / "tiny.jpg", print_frac=0.08,
                        scan_size=(1600, 1200), angle=2.0)
    result = _analyse(tmp_path, path)
    assert result.needs_manual
    assert result.manual_reason in ("print_too_small", "no_print_found")
    assert "deskew" not in result.operations["ops"]
    assert "crop" not in result.operations["ops"]


def test_a_wildly_skewed_print_is_needs_manual(tmp_path):
    path, _ = make_scan(tmp_path / "wonky.jpg", angle=30.0, print_frac=0.35)
    result = _analyse(tmp_path, path, CLEANUP_MAX_DESKEW_DEG=15.0)
    assert result.needs_manual
    assert result.manual_reason in ("skew_too_large", "implausible_aspect",
                                   "print_too_small")


def test_a_blank_bed_finds_no_print(tmp_path):
    arr = np.full((600, 800, 3), 243, dtype=np.uint8)
    path = tmp_path / "empty.jpg"
    Image.fromarray(arr).save(path, "JPEG", quality=95)
    result = _analyse(tmp_path, path)
    assert result.needs_manual
    assert result.manual_reason == "no_print_found"


# --------------------------------------------------------------------------
# Colour cast and levels
# --------------------------------------------------------------------------

def test_an_orange_cast_is_measured_and_corrected_towards_neutral(tmp_path):
    base = _print_content(900, 700)
    cast = base.astype(np.float32) * np.array([1.30, 1.0, 0.68], np.float32)
    cast = np.clip(cast, 0, 255).astype(np.uint8)
    path, _ = make_scan(tmp_path / "cast.jpg", content=cast, angle=0.0,
                        print_frac=0.55)
    result = _analyse(tmp_path, path)

    colour = result.operations["ops"].get("colour")
    assert colour is not None, result.operations
    assert colour["magnitude"] >= 6.0
    shift = colour["rgb_shift"]
    assert shift["r"] < 0 and shift["b"] > 0, shift

    # Applying the gains really does move the mid-tones towards neutral.
    before = ops.measure_cast(cast)
    after = ops.measure_cast(ops.apply_cast_gains(cast, colour["gains"]))
    # The gains are clamped to [0.74, 1.35] so a heavy cast cannot blow a
    # channel, which leaves a little residue on a cast this strong.
    assert after["magnitude"] < before["magnitude"] * 0.35, (before, after)
    assert after["magnitude"] < 8.0


def test_a_colourful_scene_with_no_cast_is_left_alone(tmp_path):
    """The regression this estimator exists for.

    Plain grey-world assumes the average scene is grey, so a photo that is
    genuinely mostly one colour — a lawn, a winter field, a warm indoor shot —
    reads as a cast and gets pushed into the opposite one. Measuring on the
    print's *near-neutral* mid-tones keeps a real cast (the paper shifts, greys
    included) and drops the scene.
    """
    # A green-dominated scene, neutral paper: grass with a grey path and a
    # white-ish sky, no cast at all.
    base = _print_content(900, 700)
    green = base.astype(np.float32)
    green[:, :] = np.array([70, 135, 60], np.float32)          # grass
    green[int(700 * 0.42):int(700 * 0.58), :] = np.array([140, 140, 140], np.float32)
    green[0:int(700 * 0.18), :] = np.array([225, 225, 228], np.float32)
    rng = np.random.default_rng(12)
    green += rng.normal(0.0, 5.0, size=green.shape).astype(np.float32)
    green = np.clip(green, 0, 255).astype(np.uint8)
    path, _ = make_scan(tmp_path / "green.jpg", content=green, angle=0.0)

    grey_world = _analyse(tmp_path, path, CLEANUP_CAST_NEUTRAL_PCT=100.0)
    neutral = _analyse(tmp_path, path, CLEANUP_CAST_NEUTRAL_PCT=40.0)

    gw = (grey_world.operations.get("tone") or {})["cast"]["magnitude"]
    ne = (neutral.operations.get("tone") or {})["cast"]["magnitude"]
    assert ne < gw, (
        f"the neutral estimator must not exceed grey-world here: {ne} vs {gw}")
    assert "colour" not in neutral.operations["ops"], (
        f"a neutral-paper green scene must not be 'corrected' "
        f"(magnitude {ne}); {neutral.operations['ops']}")


def test_the_grey_world_figure_is_kept_for_comparison(tmp_path):
    """Every proposal records what plain grey-world would have said, so the
    gap between the two estimators stays visible in the report."""
    base = _print_content(900, 700)
    cast = np.clip(base.astype(np.float32) * np.array([1.30, 1.0, 0.68], np.float32),
                   0, 255).astype(np.uint8)
    path, _ = make_scan(tmp_path / "cast_cmp.jpg", content=cast, angle=0.0)
    result = _analyse(tmp_path, path)
    tone = result.operations["tone"]["cast"]
    assert "grey_world_magnitude" in tone
    assert tone["neutral_pct"] == 40.0
    colour = result.operations["ops"].get("colour")
    if colour:
        assert colour["neutral_pct"] == 40.0
        assert colour["grey_world_magnitude"] >= colour["magnitude"] - 0.01
        assert colour["sample_px"] > 0


def test_the_gains_are_measured_on_the_same_pixels_as_the_cast(tmp_path):
    """If the gains came from a wider set than the measurement, the correction
    would not be the one the caption promised."""
    from photoarchive.modes.cleanup import ops as ops_mod
    base = _print_content(600, 500)
    cast = np.clip(base.astype(np.float32) * np.array([1.25, 1.0, 0.75], np.float32),
                   0, 255).astype(np.uint8)
    sel40 = ops_mod.neutral_midtones(cast, neutral_pct=40.0)
    sel100 = ops_mod.neutral_midtones(cast, neutral_pct=100.0)
    assert int(sel40.sum()) < int(sel100.sum())
    g40 = ops_mod.cast_gains({}, cast, neutral_pct=40.0)
    g100 = ops_mod.cast_gains({}, cast, neutral_pct=100.0)
    assert g40 != g100, "the gains must follow the selection, not ignore it"


def test_a_black_and_white_print_gets_no_colour_op(tmp_path):
    grey = _print_content(900, 700)
    grey = np.repeat(grey.mean(axis=2, keepdims=True), 3, axis=2).astype(np.uint8)
    path, _ = make_scan(tmp_path / "bw.jpg", content=grey, angle=0.0)
    result = _analyse(tmp_path, path)
    assert "colour" not in result.operations["ops"]
    assert result.operations["colour_skipped"] == "mono"
    assert result.operations["tone"]["chroma"]["is_mono"] is True


def test_a_sepia_print_gets_no_colour_op(tmp_path):
    grey = _print_content(900, 700).mean(axis=2)
    sepia = np.stack([grey * 1.20, grey * 0.98, grey * 0.62], axis=2)
    sepia = np.clip(sepia, 0, 255).astype(np.uint8)
    path, _ = make_scan(tmp_path / "sepia.jpg", content=sepia, angle=0.0)
    result = _analyse(tmp_path, path)
    assert "colour" not in result.operations["ops"], result.operations["ops"]
    assert result.operations["colour_skipped"] == "sepia"
    assert result.operations["tone"]["chroma"]["is_sepia"] is True


def test_a_low_contrast_print_gets_a_levels_op_and_a_normal_one_does_not(tmp_path):
    faded = (_print_content(900, 700).astype(np.float32) * 0.35 + 110.0)
    faded = np.clip(faded, 0, 255).astype(np.uint8)
    path, _ = make_scan(tmp_path / "faded.jpg", content=faded, angle=0.0)
    result = _analyse(tmp_path, path)
    levels = result.operations["ops"].get("levels")
    assert levels is not None, result.operations
    assert levels["contrast_before"] < 0.72
    assert levels["contrast_after"] > levels["contrast_before"]

    normal = _with_full_range(_print_content(900, 700))
    path2, _ = make_scan(tmp_path / "normal.jpg", content=normal, angle=0.0)
    result2 = _analyse(tmp_path, path2)
    assert "levels" not in result2.operations["ops"], result2.operations["ops"]


# --------------------------------------------------------------------------
# "clean" photos and the caption
# --------------------------------------------------------------------------

def test_a_photo_that_needs_nothing_is_clean_and_never_reaches_the_queue(tmp_path):
    content = _neutral_print(4000, 3000)
    path, _ = make_scan(tmp_path / "clean.jpg", content=content, angle=0.0,
                        print_size=(3992, 2992), scan_size=(4000, 3000))
    result = _analyse(tmp_path, path)
    assert result.operations["ops"] == {}
    assert result.status == "clean"
    assert result.transform is None


def test_caption_reports_the_measured_numbers():
    caption = analyse_mod.caption_for({
        "ops": {
            "deskew": {"angle_deg": 2.1},
            "crop": {"removed_frac": 0.062},
            "colour": {"rgb_shift": {"r": 9, "g": 0, "b": -7}},
            "levels": {"contrast_before": 0.58, "contrast_after": 0.96},
        },
    })
    assert "skew +2.1" in caption
    assert "crop 6%" in caption
    assert "R+9" in caption and "B-7" in caption
    assert "0.58" in caption and "0.96" in caption


def test_caption_says_why_the_colour_op_was_skipped():
    assert "mono" in analyse_mod.caption_for(
        {"ops": {"levels": {"contrast_before": 0.5, "contrast_after": 0.9}},
         "colour_skipped": "mono"})


def test_ops_are_dtype_preserving_for_16_bit(tmp_path):
    arr16 = (_print_content(200, 150).astype(np.uint16) * 257)
    stretched = ops.apply_levels(arr16, lo=40, hi=200, s_curve=0.1)
    assert stretched.dtype == np.uint16
    assert stretched.max() > 60000
    gained = ops.apply_cast_gains(arr16, {"r": 1.1, "g": 1.0, "b": 0.9})
    assert gained.dtype == np.uint16
