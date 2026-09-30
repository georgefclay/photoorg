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
                                              "components_after": 3}


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
