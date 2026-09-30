"""Fix-up 5: how many prints the analyser sees, and when it must not guess.

Three scans went wrong in three different ways, and only one of them is a
gate that can be retuned:

  * **#708** is three prints stacked on one bed. The lower two touch, so one
    connected component covered both and the analyser proposed a two-way
    split. Nothing was wrong with the gates — the prints were never separated
    in the first place. There is a 62 px band of scanner bed straight through
    that component, and cutting on it recovers the third print.
  * **#1398** is eight prints whose backgrounds are pale studio grey. The
    calm mask reads that background as bed, so the mask is holes and the
    components are neither prints nor halves of prints. A size gate cannot
    fix a mask; the region editor is the answer, and the relative gate here
    only stops eight ~10 % prints being dismissed for being small.
  * **#3839** is a newspaper cutting. Its columns of text, separated by white
    gutters, are exactly what a multi-print scan looks like from the outside.
    No image heuristic is needed: the classify job already labelled it
    `document` at 0.98 confidence, so the answer is to believe it.

A gutter cut is only allowed to fire on genuine scanner bed, which is why the
band's own pixels are tested rather than its absence from the mask — #1398 is
full of bands that are absent from the mask and are not bed.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from photoarchive.modes.cleanup import analyse as analyse_mod

from .test_cleanup_analyse import _settings, make_scan

FIXTURES = Path(__file__).parent / "fixtures"
THREE_PRINTS = FIXTURES / "scan_708_three_prints.jpg"
NEWSPAPER = FIXTURES / "scan_3839_newspaper.jpg"


def _regions(path, settings, **kw):
    r = analyse_mod.analyse_photo(settings, photo_id=1, working_path=path, **kw)
    return r, len(r.split_regions or [])


# --------------------------------------------------------------------------
# Prints that touch (#708)
# --------------------------------------------------------------------------

def test_the_real_scan_still_merges_two_prints_into_one_component(tmp_path):
    """The fixture has to keep reproducing the bug, or the test below proves
    nothing."""
    assert THREE_PRINTS.exists()
    settings = _settings(tmp_path, CLEANUP_SPLIT_GUTTERS=False)
    rgb, _w, _h, _s = analyse_mod.load_for_analysis(
        THREE_PRINTS, settings.CLEANUP_ANALYSE_EDGE)
    _bed, comps = analyse_mod.detect_bed_and_prints(rgb, settings)
    assert len(comps) == 2, [round(c.area_frac, 3) for c in comps]
    # …and the merged one is about twice as tall as it is wide, because it is
    # two portrait prints stacked.
    assert comps[0].rect.aspect > 2.0, comps[0].rect.aspect


def test_the_gutter_cut_finds_the_third_print(tmp_path):
    settings = _settings(tmp_path)
    without = _settings(tmp_path, CLEANUP_SPLIT_GUTTERS=False)
    _r, n_off = _regions(THREE_PRINTS, without)
    r_on, n_on = _regions(THREE_PRINTS, settings)
    assert n_off == 2, "the fixture must still split two ways without the cut"
    assert n_on == 3, f"expected three prints, got {n_on}"
    assert r_on.operations["gutter_cuts"] == {"components_before": 2,
                                              "components_after": 3,
                                              "used": True}


def test_the_three_regions_are_the_three_prints(tmp_path):
    settings = _settings(tmp_path)
    r, n = _regions(THREE_PRINTS, settings)
    assert n == 3
    areas = sorted(reg["area_frac"] for reg in r.split_regions)
    # Three prints on one bed: each about a third, none a sliver.
    assert min(areas) > 0.15, areas
    assert max(areas) < 0.45, areas
    tops = sorted(analyse_mod.Rect.from_json(reg["rect"]).cy
                  for reg in r.split_regions)
    assert tops[0] < tops[1] < tops[2], "the prints are stacked, so are the regions"


def test_a_gutter_is_only_cut_where_the_band_is_really_bed(tmp_path):
    """A pale calm band inside a photograph is not a gutter. Without this the
    cut fires all over #1398, whose prints have pale studio backgrounds."""
    settings = _settings(tmp_path)
    # One print with a broad pale band across its middle — light, calm, and
    # nothing to do with the scanner.
    img = np.full((900, 1200, 3), 242, np.uint8)
    rng = np.random.default_rng(5)
    grid = rng.integers(30, 210, size=(26, 34)).astype(np.uint8)
    tile = np.kron(grid, np.ones((30, 30), np.uint8))[:760, :1020]
    tile[330:430, :] = 250                      # the pale band
    img[70:830, 90:1110] = np.repeat(tile[:, :, None], 3, axis=2)
    path = tmp_path / "banded.jpg"
    Image.fromarray(img).save(path, "JPEG", quality=95)

    result = analyse_mod.analyse_photo(settings, photo_id=1, working_path=path)
    assert not result.split_regions, (
        "a pale band inside one print must not become a split")


def test_the_cut_is_refused_unless_both_halves_look_like_prints(tmp_path):
    """Cutting one real print off a sliver is the detector being wrong twice,
    not a multi-print scan."""
    settings = _settings(tmp_path)
    rgb, _w, _h, _s = analyse_mod.load_for_analysis(
        THREE_PRINTS, settings.CLEANUP_ANALYSE_EDGE)
    bed, comps = analyse_mod.detect_bed_and_prints(rgb, settings)
    gutter = analyse_mod.bed_by_tone(rgb, bed, settings)
    # The smallest component is a single print; there is nothing to cut.
    smallest = comps[-1]
    pieces = analyse_mod.split_touching_prints(rgb, smallest, bed, settings,
                                               gutter=gutter)
    assert pieces == [smallest]


# --------------------------------------------------------------------------
# Eight prints, none of them 12 % of the bed (#1398)
# --------------------------------------------------------------------------

def test_equal_prints_below_the_absolute_gate_still_count(tmp_path):
    """Eight prints on one bed are ~10 % of the scan each. What makes them
    prints is that they are all the same size as each other."""
    settings = _settings(tmp_path)
    # Eight prints in a 2x4 grid, 8.8 % of the scan each — every one of them
    # under the 12 % absolute gate, which is #1398's shape exactly.
    W, H = 2000, 1300
    pw, ph = 460, 500
    img = np.full((H, W, 3), 242, np.uint8)
    rng = np.random.default_rng(9)
    for r in range(2):
        for c in range(4):
            x0 = 32 + c * (pw + 32)
            y0 = 100 + r * (ph + 100)
            grid = rng.integers(25, 205, size=(ph // 20, pw // 20)).astype(np.uint8)
            tile = np.kron(grid, np.ones((20, 20), np.uint8))[:ph, :pw]
            img[y0:y0 + ph, x0:x0 + pw] = np.repeat(tile[:, :, None], 3, axis=2)
    path = tmp_path / "eight.jpg"
    Image.fromarray(img).save(path, "JPEG", quality=95)

    strict = _settings(tmp_path, CLEANUP_SPLIT_REL_MIN=1.0)
    r_rel, n_rel = _regions(path, settings)
    _r_abs, n_abs = _regions(path, strict)
    assert n_rel == 8, f"the relative gate should admit all eight, got {n_rel}"
    # Every one of them is under the absolute gate, so without the relative
    # one only the few that tie with the largest region survive.
    for reg in r_rel.split_regions:
        assert reg["area_frac"] < settings.CLEANUP_SPLIT_MIN_FRAC
    assert n_abs < n_rel, (
        f"the absolute gate alone should not admit all eight, got {n_abs}")


def test_the_relative_gate_does_not_admit_a_speck(tmp_path):
    """Relative to the largest is not enough on its own — a dust mote next to
    a dust mote is still two dust motes."""
    settings = _settings(tmp_path)
    path, _rects = make_scan(tmp_path / "one.jpg", angle=0.0, print_frac=0.55)
    result = analyse_mod.analyse_photo(settings, photo_id=1, working_path=path)
    assert not result.split_regions


# --------------------------------------------------------------------------
# A document is not a multi-print scan (#3839)
# --------------------------------------------------------------------------

def test_the_newspaper_splits_when_nothing_says_otherwise(tmp_path):
    """The fixture must still reproduce the wrong answer, or the veto below
    is untested."""
    settings = _settings(tmp_path)
    _r, n = _regions(NEWSPAPER, settings)
    assert n == 2, f"expected the fixture to split two ways, got {n}"


def test_a_document_is_never_split(tmp_path):
    settings = _settings(tmp_path)
    r, n = _regions(NEWSPAPER, settings, ai_label="document")
    assert n == 0
    assert r.operations["split_vetoed_by_label"] == "document"
    assert "split" not in (r.operations.get("ops") or {})


@pytest.mark.parametrize("label", ["document", "DOCUMENT", "screenshot",
                                   "back_of_print"])
def test_every_listed_label_vetoes_the_split(tmp_path, label):
    settings = _settings(tmp_path)
    _r, n = _regions(NEWSPAPER, settings, ai_label=label)
    assert n == 0


@pytest.mark.parametrize("label", ["photo", "no_people", "other", None])
def test_an_ordinary_label_changes_nothing(tmp_path, label):
    settings = _settings(tmp_path)
    _r, n = _regions(NEWSPAPER, settings, ai_label=label)
    assert n == 2


def test_the_veto_list_is_a_setting(tmp_path):
    settings = _settings(tmp_path, CLEANUP_SPLIT_SKIP_LABELS="")
    _r, n = _regions(NEWSPAPER, settings, ai_label="document")
    assert n == 2, "an empty list must veto nothing"


# --------------------------------------------------------------------------
# What the archive sweep caught: cutting must never make things worse
# --------------------------------------------------------------------------

def test_a_print_that_fills_the_scan_is_never_cut(tmp_path):
    """Photo #1033 is one photograph scanned edge to edge, covering 98 % of
    the frame. The cut found a "gutter" in the picture — a horizon, a painted
    line — and the crop that followed took a quarter of the photo away.

    Several prints on a bed always leave bed around them. A component that
    fills the scan has none, so there is nothing in it that can be a gutter.
    """
    settings = _settings(tmp_path)
    H, W = 1000, 1400
    # A print covering 95 % of the scan, with a band of bed tone across its
    # middle that a gutter map would happily believe in.
    rgb = np.full((H, W, 3), 242, np.uint8)
    rng = np.random.default_rng(21)
    grid = rng.integers(20, 210, size=(H // 20, W // 20)).astype(np.uint8)
    tile = np.kron(grid, np.ones((20, 20), np.uint8))[:H, :W]
    rgb[25:975, 35:1365] = np.repeat(tile[:950, :1330, None], 3, axis=2)

    mask = np.zeros((H, W), bool)
    mask[25:975, 35:1365] = True
    comp = analyse_mod._component_from_mask(mask, float(H * W))
    assert comp.area_frac > settings.CLEANUP_SPLIT_MAX_FILL, comp.area_frac

    bed = analyse_mod.BedInfo(kind="white", grey=242.0, score=1.0)
    gutter = np.zeros((H, W), bool)
    gutter[480:540, :] = True          # a band right across it, and beyond
    gutter[:, :35] = True
    gutter[:, 1365:] = True

    pieces = analyse_mod.split_touching_prints(rgb, comp, bed, settings,
                                               gutter=gutter)
    assert pieces == [comp], (
        "a print filling the scan has no bed in it to be a gutter")

    # Lift the ceiling and the very same band does cut it in two, which is
    # what used to happen and what took a quarter off photo #1033.
    loose = _settings(tmp_path, CLEANUP_SPLIT_MAX_FILL=1.0)
    assert len(analyse_mod.split_touching_prints(
        rgb, comp, bed, loose, gutter=gutter)) == 2


def test_a_cut_that_is_not_a_split_leaves_the_crop_alone(tmp_path):
    """The regression the sweep found on #1790 and #2251: the cut fragmented
    one print, the pieces failed the split gates, and the crop then followed
    a fragment — removing 30 % of a clean scan. Cutting decides the split and
    nothing else."""
    settings = _settings(tmp_path)
    off = _settings(tmp_path, CLEANUP_SPLIT_GUTTERS=False)
    path, _rects = make_scan(tmp_path / "one.jpg", angle=1.0, print_frac=0.55)

    with_cut = analyse_mod.analyse_photo(settings, photo_id=1, working_path=path)
    without = analyse_mod.analyse_photo(off, photo_id=1, working_path=path)

    assert not with_cut.split_regions
    a = (with_cut.operations.get("ops") or {}).get("crop") or {}
    b = (without.operations.get("ops") or {}).get("crop") or {}
    assert a.get("removed_frac", 0.0) == pytest.approx(
        b.get("removed_frac", 0.0), abs=0.01)
    ra = analyse_mod.Rect.from_json(with_cut.operations["print_rect"])
    rb = analyse_mod.Rect.from_json(without.operations["print_rect"])
    assert abs(ra.w) == pytest.approx(abs(rb.w), abs=2)
    assert abs(ra.h) == pytest.approx(abs(rb.h), abs=2)


def test_cutting_never_destroys_a_split_that_was_already_right(tmp_path):
    """Photo #3833 was a correct four-way split; the cut turned it into seven
    pieces that no longer looked like prints, and judging only the cut list
    lost the split entirely. The uncut components are reconsidered."""
    settings = _settings(tmp_path)
    off = _settings(tmp_path, CLEANUP_SPLIT_GUTTERS=False)
    path, _rects = make_scan(tmp_path / "two.jpg", prints=2, angle=1.0,
                             print_frac=0.18)

    with_cut = analyse_mod.analyse_photo(settings, photo_id=1, working_path=path)
    without = analyse_mod.analyse_photo(off, photo_id=1, working_path=path)

    assert len(without.split_regions or []) == 2
    assert len(with_cut.split_regions or []) >= 2, (
        "the cut must not lose a split the uncut components already found")
