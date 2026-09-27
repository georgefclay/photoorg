"""Rendering: the plan, the formats, and the 1:1 detail window.

The detail window is answer 5's mechanism — the review pane shows a
viewport-sized downscale and swaps in a true full-resolution crop once the
zoom reaches 1:1. If the window and the full render disagree, the reviewer is
looking at a lie, so this pins them together.

No database: `render` only needs a file and an `operations` dict.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from photoarchive.config import Settings
from photoarchive.modes.cleanup import render as render_mod
from photoarchive.modes.cleanup.geometry import Rect, Transform, transform_for


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


def _source(tmp_path: Path, *, w=1200, h=900, fmt="JPEG", dtype=np.uint8) -> Path:
    """A blocky image: distinct enough that a misaligned crop is obvious, and
    coarse enough to survive JPEG."""
    rng = np.random.default_rng(4)
    grid = rng.integers(10, 240, size=(h // 20 + 1, w // 20 + 1)).astype(np.uint8)
    arr = np.kron(grid, np.ones((20, 20), np.uint8))[:h, :w]
    rgb = np.repeat(arr[:, :, None], 3, axis=2)
    if dtype == np.uint16:
        rgb = (rgb.astype(np.uint16) * 257)
    path = tmp_path / ("src.tif" if fmt == "TIFF" else "src.jpg")
    if fmt == "TIFF":
        Image.fromarray(rgb).save(path, "TIFF", compression="tiff_lzw")
    else:
        Image.fromarray(rgb).save(path, "JPEG", quality=100, subsampling=0)
    return path


def _operations(w: int, h: int, *, angle=0.0, with_tone=True) -> dict:
    ops: dict = {
        "bed": {"kind": "white", "grey": 242.0},
        "analysis": {"src_w": w, "src_h": h, "dpi": 300, "inset_px": 4.0},
        "print_rect": Rect(cx=w / 2, cy=h / 2, w=w * 0.8, h=h * 0.8,
                           angle=angle).to_json(),
        "ops": {
            "crop": {"removed_frac": 0.36},
        },
    }
    if angle:
        ops["ops"]["deskew"] = {"angle_deg": angle}
    if with_tone:
        ops["ops"]["colour"] = {"gains": {"r": 1.05, "g": 1.0, "b": 0.95},
                                "rgb_shift": {"r": 6, "g": 0, "b": -6}}
        ops["ops"]["levels"] = {"lo": 10.0, "hi": 240.0, "s_curve": 0.12,
                                "contrast_before": 0.60, "contrast_after": 0.95}
    return ops


# --------------------------------------------------------------------------
# The plan
# --------------------------------------------------------------------------

def test_default_ticked_is_everything_the_analyser_proposed(tmp_path):
    ops = _operations(1200, 900, angle=3.0)
    # Without settings, every proposed op is ticked; fix-up 2's switches
    # are applied only when settings are passed (see
    # test_an_old_proposal_opens_with_its_tonal_ops_unticked).
    assert render_mod.default_ticked(ops) == ("deskew", "crop", "colour", "levels")
    assert render_mod.default_ticked({"ops": {}}) == ()


def test_the_plan_drops_what_is_unticked(tmp_path):
    # The tonal ops are opt-in since fix-up 2; this test is about the ticks,
    # so switch them on to exercise that path.
    settings = _settings(tmp_path, CLEANUP_COLOUR_ENABLED=True,
                         CLEANUP_LEVELS_ENABLED=True)
    ops = _operations(1200, 900, angle=3.0)

    full = render_mod.plan_from(ops, ("deskew", "crop", "colour", "levels"),
                                settings=settings)
    assert full.colour_gains and full.levels
    assert full.transform.angle_deg == pytest.approx(3.0)

    geo = render_mod.plan_from(ops, ("deskew", "crop"), settings=settings)
    assert geo.colour_gains is None and geo.levels is None
    assert geo.transform.out_w == full.transform.out_w

    tonal = render_mod.plan_from(ops, ("colour",), settings=settings)
    assert tonal.transform.is_identity
    assert tonal.colour_gains and tonal.levels is None

    nothing = render_mod.plan_from(ops, (), settings=settings)
    assert nothing.is_noop


def test_an_op_the_analyser_did_not_propose_cannot_be_ticked_on(tmp_path):
    settings = _settings(tmp_path, CLEANUP_COLOUR_ENABLED=True,
                         CLEANUP_LEVELS_ENABLED=True)
    ops = _operations(1200, 900, angle=0.0, with_tone=False)  # crop only
    plan = render_mod.plan_from(ops, ("deskew", "crop", "colour", "levels"),
                                settings=settings)
    assert plan.transform.angle_deg == 0.0
    assert plan.colour_gains is None and plan.levels is None


# --------------------------------------------------------------------------
# Format follows the source (invariant 3)
# --------------------------------------------------------------------------

def test_jpeg_in_jpeg_out(tmp_path):
    settings = _settings(tmp_path, CLEANUP_COLOUR_ENABLED=True,
                         CLEANUP_LEVELS_ENABLED=True)
    src = _source(tmp_path)
    ops = _operations(1200, 900)
    plan = render_mod.plan_from(ops, ("crop",), settings=settings)
    out = render_mod.render_full(src, plan, tmp_path / "out.jpg",
                                mime="image/jpeg", operations=ops)
    with Image.open(out.path) as im:
        assert im.format == "JPEG"
        assert im.size == (out.width, out.height)
    assert out.phash and out.dhash
    assert out.thumb_bytes and out.thumb_bytes[:2] == b"\xff\xd8"


def test_tiff_in_tiff_out(tmp_path):
    settings = _settings(tmp_path, CLEANUP_LEVELS_ENABLED=True)
    src = _source(tmp_path, fmt="TIFF")
    with Image.open(src) as im:
        w, h = im.size
    ops = _operations(w, h)
    plan = render_mod.plan_from(ops, ("crop", "levels"), settings=settings)
    out = render_mod.render_full(src, plan, tmp_path / "out.tif",
                                mime="image/tiff", operations=ops)
    with Image.open(out.path) as im:
        assert im.format == "TIFF"
        assert im.size == (out.width, out.height)


def test_a_sixteen_bit_greyscale_tiff_stays_sixteen_bit(tmp_path):
    """Invariant 3 keeps the bit depth as well as the format — for greyscale,
    which is the mode Pillow can actually carry at 16 bits."""
    settings = _settings(tmp_path, CLEANUP_LEVELS_ENABLED=True)
    h, w = 300, 400
    rng = np.random.default_rng(6)
    grid = rng.integers(500, 64000, size=(h // 20 + 1, w // 20 + 1)).astype(np.uint16)
    grey16 = np.kron(grid, np.ones((20, 20), np.uint16))[:h, :w]
    src = tmp_path / "grey16.tif"
    Image.fromarray(grey16).save(src, "TIFF", compression="tiff_lzw")
    with Image.open(src) as im:
        assert im.mode == "I;16"

    ops = _operations(w, h)
    plan = render_mod.plan_from(ops, ("crop", "levels"), settings=settings)
    out = render_mod.render_full(src, plan, tmp_path / "out16.tif",
                                mime="image/tiff", operations=ops)
    with Image.open(out.path) as im:
        assert im.format == "TIFF"
        assert im.mode == "I;16"
        arr = np.asarray(im)
    assert arr.dtype == np.uint16, "a 16-bit greyscale scan must stay 16-bit"
    assert arr.max() > 30000


def test_sixteen_bit_colour_is_reduced_on_load_not_crashed_on_save(tmp_path):
    """Pillow has no 16-bit RGB mode — `Image.fromarray` on a 3-channel uint16
    array raises. `load_display_array` reduces it once, on the way in, so a
    render never blows up at save time."""
    from photoarchive.modes.cleanup import ops as ops_mod
    h, w = 40, 60
    rgb16 = ((np.arange(h * w * 3, dtype=np.uint16).reshape(h, w, 3) * 700)
             % 65535)
    with pytest.raises(TypeError):
        Image.fromarray(rgb16)          # the thing we are guarding against
    reduced = ops_mod.as_uint8(rgb16)
    assert reduced.dtype == np.uint8 and reduced.shape == (h, w, 3)
    assert render_mod._pil_mode_for(reduced) == "RGB"


# --------------------------------------------------------------------------
# The 1:1 detail window (answer 5)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("angle", [0.0, 2.5])
@pytest.mark.parametrize("ticked", [("crop",), ("deskew", "crop"),
                                    ("deskew", "crop", "colour", "levels")])
def test_the_detail_window_matches_the_full_render(tmp_path, angle, ticked):
    """A 1:1 crop of the output must be the same pixels the full render puts
    there — otherwise zooming in shows something that will not be accepted."""
    settings = _settings(tmp_path, CLEANUP_COLOUR_ENABLED=True,
                         CLEANUP_LEVELS_ENABLED=True)
    src = _source(tmp_path, fmt="TIFF")     # lossless, so this can be exact-ish
    with Image.open(src) as im:
        w, h = im.size
    ops = _operations(w, h, angle=angle)
    plan = render_mod.plan_from(ops, ticked, settings=settings)

    full = render_mod.render_full(src, plan, tmp_path / "full.tif",
                                 mime="image/tiff", operations=ops,
                                 want_hashes=False)
    with Image.open(full.path) as im:
        full_arr = np.asarray(im.convert("RGB"))

    box = (60, 40, 200, 150)
    window = render_mod.crop_at_full_res(src, plan, box=box, operations=ops)
    win_arr = np.asarray(window)

    x, y, bw, bh = box
    expect = full_arr[y:y + bh, x:x + bw]
    assert win_arr.shape == expect.shape
    # Lanczos resampling at a shifted origin is not bit-identical, but it must
    # be the same picture.
    diff = np.abs(win_arr.astype(np.int16) - expect.astype(np.int16))
    assert diff.mean() < 2.0, f"mean |diff| {diff.mean():.2f}"
    assert np.percentile(diff, 99) < 16, f"p99 |diff| {np.percentile(diff, 99)}"


def test_the_detail_window_is_clamped_to_the_output_frame(tmp_path):
    settings = _settings(tmp_path)
    src = _source(tmp_path)
    ops = _operations(1200, 900)
    plan = render_mod.plan_from(ops, ("crop",), settings=settings)

    huge = render_mod.crop_at_full_res(
        src, plan, box=(0, 0, 99999, 99999), operations=ops)
    assert huge.size == (plan.transform.out_w, plan.transform.out_h)

    off = render_mod.crop_at_full_res(
        src, plan, box=(plan.transform.out_w + 500, 0, 50, 50), operations=ops)
    assert off.width >= 1 and off.height >= 1


def test_the_preview_is_bounded_and_always_jpeg(tmp_path):
    settings = _settings(tmp_path)
    src = _source(tmp_path, fmt="TIFF")
    with Image.open(src) as im:
        w, h = im.size
    ops = _operations(w, h)
    plan = render_mod.plan_from(ops, ("crop",), settings=settings)
    out = render_mod.render_preview(src, plan, tmp_path / "p.jpg", edge=320,
                                    operations=ops)
    with Image.open(out.path) as im:
        assert im.format == "JPEG"
        assert max(im.size) <= 320


def test_an_uncropped_deskew_fills_the_corners_with_the_bed_not_black(tmp_path):
    """Unticking crop while deskew stays on keeps the whole rotated canvas —
    it has to look deliberate, not like a mistake."""
    settings = _settings(tmp_path)
    src = _source(tmp_path)
    ops = _operations(1200, 900, angle=6.0)
    plan = render_mod.plan_from(ops, ("deskew",), settings=settings)
    out = render_mod.render_full(src, plan, tmp_path / "rot.jpg",
                                mime="image/jpeg", operations=ops,
                                want_hashes=False)
    with Image.open(out.path) as im:
        arr = np.asarray(im.convert("RGB"))
    # The extreme top-left corner is exposed canvas; it should carry the bed's
    # tone (242), not 0.
    corner = arr[2, 2]
    assert corner.mean() > 200, corner


# --------------------------------------------------------------------------
# Memory: the tonal ops go through a LUT, previews warp a reduced source
# --------------------------------------------------------------------------

@pytest.mark.parametrize("dtype", [np.uint8, np.uint16])
def test_the_lut_matches_the_direct_arithmetic(dtype):
    """`ops.tone_luts` composes both tonal ops so a 93 MP scan never needs a
    1.1 GB float32 copy. It must agree with the direct route — and it rounds
    once instead of twice, so it can only be closer, never further."""
    from photoarchive.modes.cleanup import ops as ops_mod
    mx = 255 if dtype == np.uint8 else 65535
    rng = np.random.default_rng(1)
    arr = rng.integers(0, mx + 1, size=(60, 80, 3)).astype(dtype)
    gains = {"r": 1.07, "g": 1.0, "b": 0.88}
    levels = {"lo": 14.0, "hi": 233.0, "s_curve": 0.12}

    direct = ops_mod.apply_levels(
        ops_mod.apply_cast_gains(arr, gains),
        lo=levels["lo"], hi=levels["hi"], s_curve=levels["s_curve"])
    luts = ops_mod.tone_luts(arr.dtype, gains=gains, levels=levels, channels=3)
    via_lut = ops_mod.apply_tone_luts(arr, luts)

    assert via_lut.dtype == dtype
    diff = np.abs(direct.astype(np.int64) - via_lut.astype(np.int64))
    assert diff.max() <= 1, "one level of rounding is the only allowed gap"
    assert diff.mean() < 0.5


def test_the_lut_in_place_matches_the_copy():
    from photoarchive.modes.cleanup import ops as ops_mod
    rng = np.random.default_rng(2)
    arr = rng.integers(0, 256, size=(40, 50, 3)).astype(np.uint8)
    luts = ops_mod.tone_luts(arr.dtype, gains={"r": 1.1, "g": 1.0, "b": 0.9},
                             levels={"lo": 12.0, "hi": 240.0, "s_curve": 0.12},
                             channels=3)
    copied = ops_mod.apply_tone_luts(arr.copy(), luts)
    target = arr.copy()
    in_place = ops_mod.apply_tone_luts(target, luts, in_place=True)
    assert in_place is target
    assert np.array_equal(copied, in_place)


def test_no_tonal_ops_means_no_lut_and_no_copy():
    from photoarchive.modes.cleanup import ops as ops_mod
    arr = np.zeros((4, 5, 3), np.uint8)
    assert ops_mod.tone_luts(arr.dtype) is None
    assert ops_mod.apply_tone_luts(arr, None) is arr


def test_a_scaled_transform_maps_the_reduced_frame_the_same_way():
    """`render_preview` warps a downscaled source and scales the transform to
    match, so the preview must land where the full render would, to within the
    reduction factor."""
    t = transform_for(Rect(cx=600.0, cy=450.0, w=900.0, h=700.0, angle=3.0),
                      src_w=1200, src_h=900, deskew=True, crop=True, inset_px=4.0)
    k = 0.25
    small = t.scaled_by(k)
    assert small.out_w == pytest.approx(round(t.out_w * k), abs=1)
    assert small.src_w == pytest.approx(round(t.src_w * k), abs=1)
    for x, y in ((0.0, 0.0), (300.0, 200.0), (1199.0, 899.0)):
        fx, fy = t.apply_point(x, y)
        sx, sy = small.apply_point(x * k, y * k)
        assert sx == pytest.approx(fx * k, abs=1e-6)
        assert sy == pytest.approx(fy * k, abs=1e-6)


def test_the_preview_of_a_large_scan_does_not_load_it_whole(tmp_path):
    """The point of the reduction: a preview must not allocate the full image.
    Tracemalloc only sees Python/numpy allocations, which is exactly the part
    that used to blow up."""
    import tracemalloc
    settings = _settings(tmp_path, CLEANUP_COLOUR_ENABLED=True,
                         CLEANUP_LEVELS_ENABLED=True)
    w, h = 4000, 3000                     # 12 MP: 36 MB as uint8 RGB
    src = _source(tmp_path, w=w, h=h)
    ops = _operations(w, h, angle=2.0)
    plan = render_mod.plan_from(ops, ("deskew", "crop", "colour", "levels"),
                                settings=settings)

    tracemalloc.start()
    render_mod.render_preview(src, plan, tmp_path / "prev.jpg", edge=2000,
                              operations=ops)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert peak < w * h * 3 * 0.75, (
        f"peak {peak/1e6:.0f} MB is too close to the full {w*h*3/1e6:.0f} MB image")


def test_the_preview_still_shows_the_right_crop(tmp_path):
    """Reducing first must not move the crop: the preview and a downscale of
    the full render should be the same picture."""
    settings = _settings(tmp_path)
    src = _source(tmp_path, w=1600, h=1200, fmt="TIFF")
    ops = _operations(1600, 1200, angle=0.0, with_tone=False)
    plan = render_mod.plan_from(ops, ("crop",), settings=settings)

    full = render_mod.render_full(src, plan, tmp_path / "f.tif",
                                  mime="image/tiff", operations=ops,
                                  want_hashes=False)
    prev = render_mod.render_preview(src, plan, tmp_path / "p.jpg", edge=400,
                                     operations=ops)
    # Same aspect, and the preview is a reduction of the same frame.
    assert (prev.width / prev.height) == pytest.approx(
        full.width / full.height, rel=0.02)

    with Image.open(full.path) as im:
        ref = np.asarray(im.convert("RGB").resize((prev.width, prev.height),
                                                  Image.LANCZOS)).astype(np.int16)
    with Image.open(prev.path) as im:
        got = np.asarray(im.convert("RGB")).astype(np.int16)
    assert np.abs(got - ref).mean() < 6.0
