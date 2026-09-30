"""The low-memory JPEG loader: reduced decode must see what a full decode sees.

`load_for_analysis` used to decode every scan at full resolution and then throw
the pixels away — 281 MB for the archive's 93.7 MP scan, 114 MB for a 38 MP
JPEG, all to produce a 2000 px working copy. Pillow's `draft()` decodes a JPEG
straight out of the DCT at 1/2, 1/4 or 1/8, so the full array is never
allocated.

That is only safe if it changes nothing the analyser depends on, which is what
these pin: the same source dimensions, the same scale, the same print
rectangle, the same ops. TIFF has no reduced-decode path, so it must be
untouched.

Two ways these tests can lie, both of which they did before they were finished:

  * if `draft` never engages, every comparison is one path against itself.
    Pillow picks the scale as ``min(w // box_w, h // box_h)``, so a square
    (edge, edge) box is governed by the SHORT edge and a 4400x3000 scan asking
    for 2000x2000 got ``min(2, 1) = 1``;
  * `JpegImageFile` overrides `draft`, so patching `Image.Image.draft` to force
    the full-decode path silently patches nothing.
"""
from __future__ import annotations

import numpy as np
import pytest
from PIL import Image, JpegImagePlugin

from photoarchive.modes.cleanup import analyse as analyse_mod

from .test_cleanup_analyse import _settings, make_scan

#: The class the loader actually gets back for a JPEG. Patching `Image.Image`
#: here would be a no-op — the subclass shadows it.
JPEG = JpegImagePlugin.JpegImageFile


def _no_draft(monkeypatch):
    """Force the full-decode path, so both can be measured on one image."""
    monkeypatch.setattr(JPEG, "draft", lambda self, mode, size: None)


def _big_scan(tmp_path, name="big.jpg", w=4400, h=3000, angle=0.0):
    """Large enough that `draft` actually reduces: the analysis edge is 2000,
    so a 4400 px scan is decoded at 1/2."""
    return make_scan(tmp_path / name, scan_size=(w, h), angle=angle,
                     print_frac=0.55)


# --------------------------------------------------------------------------
# The reduction really happens
# --------------------------------------------------------------------------

def test_draft_actually_reduces_the_decode(tmp_path, monkeypatch):
    """Without this every equivalence test below passes vacuously."""
    settings = _settings(tmp_path)
    path, _rects = _big_scan(tmp_path)
    edge = settings.CLEANUP_ANALYSE_EDGE

    seen = []
    real_draft = JPEG.draft

    def spy(self, mode, size):
        before = self.size
        out = real_draft(self, mode, size)
        seen.append((before, size, self.size))
        return out

    monkeypatch.setattr(JPEG, "draft", spy)
    arr, src_w, src_h, scale = analyse_mod.load_for_analysis(path, edge)

    assert seen, "the loader never called draft"
    before, requested, after = seen[0]
    assert before == (4400, 3000)
    assert after == (2200, 1500), (
        f"draft did not reduce the decode: asked {requested}, got {after}")
    # It must never decode below the analysis edge — detail the resize cannot
    # put back.
    assert max(after) >= edge, after
    # …and the loader still reports the full-resolution frame.
    assert (src_w, src_h) == (4400, 3000)
    assert scale == pytest.approx(src_w / arr.shape[1], rel=1e-9)


@pytest.mark.parametrize("size,expect", [
    ((4400, 3000), (2200, 1500)),     # landscape 4:3, the archive's usual shape
    ((3000, 4400), (1500, 2200)),     # portrait
    ((9000, 6000), (2250, 1500)),     # 1/4
    ((2600, 1800), (2600, 1800)),     # long edge only 1.3x: no whole scale fits
])
def test_the_request_box_is_asked_for_in_proportion(tmp_path, monkeypatch,
                                                    size, expect):
    """The short-edge trap: a square box reduces only when BOTH dimensions
    clear it, so 4:3 scans were decoded in full however large they were."""
    settings = _settings(tmp_path)
    path, _rects = _big_scan(tmp_path, name=f"p{size[0]}x{size[1]}.jpg",
                             w=size[0], h=size[1])
    seen = []
    real_draft = JPEG.draft

    def spy(self, mode, sz):
        out = real_draft(self, mode, sz)
        seen.append(self.size)
        return out

    monkeypatch.setattr(JPEG, "draft", spy)
    analyse_mod.load_for_analysis(path, settings.CLEANUP_ANALYSE_EDGE)
    assert seen and seen[0] == expect, (size, seen)


def test_a_scan_already_below_the_edge_is_not_drafted(tmp_path, monkeypatch):
    """Nothing to gain, and a box bigger than the image is meaningless."""
    settings = _settings(tmp_path)
    path, _rects = make_scan(tmp_path / "small.jpg", scan_size=(900, 700))
    calls = []
    monkeypatch.setattr(JPEG, "draft",
                        lambda self, mode, size: calls.append(size))
    analyse_mod.load_for_analysis(path, settings.CLEANUP_ANALYSE_EDGE)
    assert calls == []


# --------------------------------------------------------------------------
# Draft vs. full decode
# --------------------------------------------------------------------------

def test_draft_gives_the_same_frame_as_a_full_decode(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    path, _rects = _big_scan(tmp_path)

    drafted, dw, dh, dscale = analyse_mod.load_for_analysis(
        path, settings.CLEANUP_ANALYSE_EDGE)

    _no_draft(monkeypatch)
    full, fw, fh, fscale = analyse_mod.load_for_analysis(
        path, settings.CLEANUP_ANALYSE_EDGE)

    assert (dw, dh) == (fw, fh) == (4400, 3000), "source dims must be the raw ones"
    assert dscale == pytest.approx(fscale, rel=1e-9)
    assert drafted.shape == full.shape
    # DCT-domain reduction is not bit-identical to a full decode plus Lanczos,
    # but it must be the same picture.
    diff = np.abs(drafted.astype(np.int16) - full.astype(np.int16))
    assert diff.mean() < 3.0, f"mean |diff| {diff.mean():.2f}"
    assert np.percentile(diff, 99) < 24, np.percentile(diff, 99)


def test_draft_and_full_decode_find_the_same_print(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    path, _rects = _big_scan(tmp_path, angle=3.0)

    def rect_for():
        rgb, _w, _h, _s = analyse_mod.load_for_analysis(
            path, settings.CLEANUP_ANALYSE_EDGE)
        _bed, comps = analyse_mod.detect_bed_and_prints(rgb, settings)
        assert comps
        return comps[0].rect

    drafted = rect_for()
    _no_draft(monkeypatch)
    full = rect_for()

    assert drafted.cx == pytest.approx(full.cx, abs=2)
    assert drafted.cy == pytest.approx(full.cy, abs=2)
    assert abs(drafted.w) == pytest.approx(abs(full.w), abs=2)
    assert abs(drafted.h) == pytest.approx(abs(full.h), abs=2)
    assert drafted.angle == pytest.approx(full.angle, abs=0.2)


def test_draft_and_full_decode_propose_the_same_ops(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    path, _rects = _big_scan(tmp_path, angle=3.0)

    drafted = analyse_mod.analyse_photo(settings, photo_id=1, working_path=path)
    _no_draft(monkeypatch)
    full = analyse_mod.analyse_photo(settings, photo_id=1, working_path=path)

    assert drafted.op_names == full.op_names
    assert drafted.status == full.status
    assert drafted.needs_manual == full.needs_manual
    assert drafted.manual_reason == full.manual_reason
    assert (drafted.src_w, drafted.src_h) == (full.src_w, full.src_h)

    if "deskew" in drafted.op_names:
        assert (drafted.operations["ops"]["deskew"]["angle_deg"]
                == pytest.approx(full.operations["ops"]["deskew"]["angle_deg"],
                                 abs=0.2))
    if "crop" in drafted.op_names:
        dc = drafted.operations["ops"]["crop"]
        fc = full.operations["ops"]["crop"]
        assert dc["out_w"] == pytest.approx(fc["out_w"], abs=4)
        assert dc["out_h"] == pytest.approx(fc["out_h"], abs=4)


def test_a_tiff_is_unaffected_because_draft_does_not_apply(tmp_path):
    """TIFF has no reduced-decode path in Pillow: `draft` is the base class's
    no-op. The loader must behave exactly as it did before, and calling `draft`
    on a format that ignores it must not corrupt anything."""
    settings = _settings(tmp_path)
    path, _rects = make_scan(tmp_path / "src.jpg", scan_size=(2600, 1800),
                             angle=2.0, print_frac=0.55)
    tif = tmp_path / "as_tiff.tif"
    with Image.open(path) as im:
        im.convert("RGB").save(tif, "TIFF", compression="tiff_lzw")

    with Image.open(tif) as probe:
        assert type(probe).draft is Image.Image.draft, (
            "TIFF must be using the base class's no-op draft")
        before = probe.size
        probe.draft("RGB", (1300, 900))
        assert probe.size == before, "draft must not have reduced a TIFF"

    arr, src_w, src_h, scale = analyse_mod.load_for_analysis(
        tif, settings.CLEANUP_ANALYSE_EDGE)
    assert (src_w, src_h) == (2600, 1800)
    assert arr.shape[:2] == (1385, 2000)
    assert scale == pytest.approx(src_w / arr.shape[1], rel=1e-9)


# --------------------------------------------------------------------------
# The frame the rest of the pipeline works in
# --------------------------------------------------------------------------

def test_the_loader_reports_display_dimensions_not_raw(tmp_path):
    """`draft` changes `im.size`, so the dimensions have to be captured before
    it — everything downstream is in the full-resolution display frame
    (CLAUDE.md, face coordinate frame)."""
    settings = _settings(tmp_path)
    path, _rects = _big_scan(tmp_path, name="portrait.jpg", w=3000, h=4400)
    arr, src_w, src_h, scale = analyse_mod.load_for_analysis(
        path, settings.CLEANUP_ANALYSE_EDGE)
    assert (src_w, src_h) == (3000, 4400)
    assert arr.shape[1] / arr.shape[0] == pytest.approx(3000 / 4400, rel=0.01)
    assert scale == pytest.approx(src_w / arr.shape[1], rel=1e-9)


def test_a_small_scan_is_not_upscaled(tmp_path):
    settings = _settings(tmp_path)
    path, _rects = make_scan(tmp_path / "small.jpg", scan_size=(900, 700),
                             angle=0.0)
    arr, src_w, src_h, scale = analyse_mod.load_for_analysis(
        path, settings.CLEANUP_ANALYSE_EDGE)
    assert (src_w, src_h) == (900, 700)
    assert arr.shape[:2] == (700, 900)
    assert scale == pytest.approx(1.0)


# --------------------------------------------------------------------------
# The two copies the loader no longer makes
# --------------------------------------------------------------------------

def test_a_greyscale_scan_is_downscaled_before_it_becomes_rgb(tmp_path):
    """Twelve of the archive's scans are greyscale TIFFs, the largest 93.7 MP:
    94 MB decoded, another 281 MB the moment it becomes RGB — for a picture
    thrown away at 2000 px. Replicating one channel into three commutes with a
    linear resample, so the order is free to change, and the result must be
    the same picture either way."""
    settings = _settings(tmp_path)
    edge = settings.CLEANUP_ANALYSE_EDGE
    path, _rects = make_scan(tmp_path / "grey_src.jpg", scan_size=(4400, 3000),
                             angle=2.0)
    grey = tmp_path / "grey.tif"
    with Image.open(path) as im:
        im.convert("L").save(grey, "TIFF", compression="tiff_lzw")

    arr, src_w, src_h, scale = analyse_mod.load_for_analysis(grey, edge)

    assert arr.ndim == 3 and arr.shape[2] == 3, "must come back as RGB"
    assert np.array_equal(arr[:, :, 0], arr[:, :, 1])
    assert np.array_equal(arr[:, :, 1], arr[:, :, 2])
    assert (src_w, src_h) == (4400, 3000)

    # The old order, done by hand: convert first, then resize.
    with Image.open(grey) as im:
        rgb = im.convert("RGB")
        f = edge / float(max(rgb.size))
        old = np.asarray(rgb.resize(
            (round(rgb.width * f), round(rgb.height * f)), Image.LANCZOS))
    assert old.shape == arr.shape
    assert np.abs(old.astype(np.int16) - arr.astype(np.int16)).max() <= 1


def test_a_palette_image_keeps_the_old_order(tmp_path):
    """Palette and 16-bit modes do not resample faithfully, so they must still
    be converted before they are downscaled."""
    settings = _settings(tmp_path)
    path, _rects = make_scan(tmp_path / "pal_src.jpg", scan_size=(3000, 2200))
    pal = tmp_path / "pal.png"
    with Image.open(path) as im:
        im.convert("P", palette=Image.ADAPTIVE, colors=128).save(pal)

    arr, src_w, src_h, _scale = analyse_mod.load_for_analysis(
        pal, settings.CLEANUP_ANALYSE_EDGE)
    assert arr.ndim == 3 and arr.shape[2] == 3
    assert (src_w, src_h) == (3000, 2200)
    # A nearest-neighbour resample of palette indices would leave the frame
    # full of hard colour steps; a faithful one keeps the bed smooth.
    corner = arr[:40, :40].reshape(-1, 3)
    assert corner.std(axis=0).max() < 12, corner.std(axis=0)


def test_an_exif_rotated_scan_is_still_transposed(tmp_path):
    """`exif_transpose` now runs in place — the transposition itself must
    still happen, and the reported dimensions must be the display ones."""
    settings = _settings(tmp_path)
    path, _rects = make_scan(tmp_path / "rot_src.jpg", scan_size=(2600, 1800))
    rotated = tmp_path / "rot.jpg"
    with Image.open(path) as im:
        exif = im.getexif()
        exif[0x0112] = 6                      # rotate 90 CW on display
        im.save(rotated, "JPEG", quality=95, exif=exif)

    arr, src_w, src_h, scale = analyse_mod.load_for_analysis(
        rotated, settings.CLEANUP_ANALYSE_EDGE)
    # Stored 2600x1800; orientation 6 means it displays portrait.
    assert (src_w, src_h) == (1800, 2600)
    assert arr.shape[0] > arr.shape[1], "the array must be in the display frame"
    assert scale == pytest.approx(src_w / arr.shape[1], rel=1e-9)
