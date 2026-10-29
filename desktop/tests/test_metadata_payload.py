"""Phase 13 — the payload, without a database or a file.

What is decided here is what the archive will say about a photo forever, so
the wording rules get their own tests: never a guessed date, common names in
keywords and every known name in the description, the AI text marked and
truncated first, an unverified back labelled as one, and face boxes mapped
into the stored frame for every orientation.
"""
from __future__ import annotations

from datetime import date

import pytest

from photoarchive.modes.metadata import exiftool as et_mod
from photoarchive.modes.metadata import payload as pl
from photoarchive.modes.metadata import regions as rg


def _peggy(**kw) -> pl.PersonFacts:
    base = dict(id=1, given_name="Margaret", middle_name="Jane", surname="Clay",
                maiden_name="Schmidt", nickname="Peggy",
                display_name='Margaret "Peggy" Clay (née Schmidt)',
                variants=("Maggie", "Meg"))
    base.update(kw)
    return pl.PersonFacts(**base)


def _facts(**kw) -> pl.PhotoFacts:
    base = dict(photo_id=7, master_sha256="ab" * 32, width=1000, height=800)
    base.update(kw)
    return pl.PhotoFacts(**base)


# ---- names -----------------------------------------------------------------

def test_keyword_is_the_common_name_and_the_description_has_every_name():
    p = _peggy()
    assert pl.keyword_name(p) == "Peggy Clay"
    assert pl.formal_name(p) == "Margaret Jane Clay (née Schmidt)"
    assert pl.full_name_line(p) == \
        "Margaret Jane 'Peggy' Clay (née Schmidt; also Maggie, Meg)"


def test_suffix_and_fallbacks():
    george = pl.PersonFacts(id=2, given_name="George", surname="Clay", suffix="Jr.")
    assert pl.keyword_name(george) == "George Clay Jr."
    assert pl.full_name_line(george) == "George Clay Jr."
    only_display = pl.PersonFacts(id=3, display_name="Uncle Bob")
    assert pl.keyword_name(only_display) == "Uncle Bob"
    assert pl.formal_name(only_display) == "Uncle Bob"


def test_a_variant_that_repeats_a_name_already_shown_is_not_listed_twice():
    p = _peggy(variants=("peggy", "Margaret", "Peg"))
    assert pl.full_name_line(p).endswith("(née Schmidt; also Peg)")


# ---- dates -----------------------------------------------------------------

def test_day_precision_without_a_time_never_offers_exif():
    d = pl.date_fields(_facts(capture_date=date(2001, 6, 12),
                              capture_date_precision="exact",
                              capture_date_confirmed=True))
    assert d["xmp"] == "2001-06-12" and d["iptc"] == "2001:06:12"
    assert d["exif_datetime"] is None, "never fabricate a time of day"
    assert d["note"] is None


def test_day_precision_with_the_cameras_time_for_that_day():
    d = pl.date_fields(_facts(capture_date=date(2001, 6, 12),
                              capture_date_precision="exact",
                              capture_date_confirmed=True,
                              exif_taken_at="2001:06:12 14:03:09"))
    assert d["exif_datetime"] == "2001:06:12 14:03:09"
    assert d["xmp"] == "2001-06-12T14:03:09"
    other_day = pl.date_fields(_facts(capture_date=date(2001, 6, 13),
                                      capture_date_precision="exact",
                                      capture_date_confirmed=True,
                                      exif_taken_at="2001:06:12 14:03:09"))
    assert other_day["exif_datetime"] is None


@pytest.mark.parametrize("prec, xmp, note", [
    ("month", "1962-03", "Date: March 1962"),
    ("year", "1962", "Date: about 1962"),
    ("decade", None, "Date: 1960s"),
])
def test_partial_dates_are_xmp_only_and_never_a_january_first(prec, xmp, note):
    d = pl.date_fields(_facts(capture_date=date(1962, 3, 1),
                              capture_date_precision=prec,
                              capture_date_confirmed=True))
    assert d["xmp"] == xmp
    assert d["iptc"] is None and d["exif_datetime"] is None
    assert d["note"] == note


def test_unknown_and_unconfirmed_dates_write_nothing():
    assert pl.date_fields(_facts(capture_date=date(1962, 1, 1),
                                 capture_date_precision="unknown",
                                 capture_date_confirmed=True)) is None
    assert pl.date_fields(_facts(capture_date=date(1962, 3, 4),
                                 capture_date_precision="exact",
                                 capture_date_confirmed=False)) is None
    p = pl.compose(_facts(capture_date=date(1962, 1, 1),
                          capture_date_precision="unknown",
                          capture_date_confirmed=True))
    assert p["date"] is None and p["description"] is None


# ---- description -----------------------------------------------------------

def test_description_order_and_markers():
    f = _facts(albums=("Summer 1992 - Canada",), description_ai="A picnic.",
               capture_date=date(1962, 3, 1), capture_date_precision="year",
               capture_date_confirmed=True,
               people={1: _peggy()},
               faces=(pl.FaceFacts(10, 1, {"x": 1, "y": 1, "w": 10, "h": 10}),),
               backs=(pl.BackFacts(1, "Lake  Louise\n1962", False),
                      pl.BackFacts(2, "Peggy", True)))
    lines = pl.compose(f)["description"].split("\n")
    assert lines == [
        "Albums: Summer 1992 - Canada",
        "People: Margaret Jane 'Peggy' Clay (née Schmidt; also Maggie, Meg)",
        "Date: about 1962",
        "AI-generated description: A picnic.",
        "Back of print (unverified transcription): Lake Louise 1962",
        "Back of print: Peggy",
    ]


def test_over_the_limit_the_ai_text_is_cut_first():
    text = pl.compose_description(
        albums=["A"], people_lines=["Peggy"], date_note=None,
        ai_text="x" * 5000, backs=[("the back", True)], limit=300)
    assert len(text) <= 300
    assert text.endswith("Back of print: the back"), "the back survives intact"
    ai = [ln for ln in text.split("\n") if ln.startswith(pl.AI_PREFIX)][0]
    assert ai.endswith(pl.ELLIPSIS)


def test_people_read_left_to_right():
    a = pl.PersonFacts(id=1, given_name="Ann", surname="Z")
    b = pl.PersonFacts(id=2, given_name="Bob", surname="Y")
    f = _facts(people={1: a, 2: b}, faces=(
        pl.FaceFacts(1, 1, {"x": 600, "y": 0, "w": 10, "h": 10}),
        pl.FaceFacts(2, 2, {"x": 100, "y": 0, "w": 10, "h": 10}),
    ))
    assert pl.compose(f)["keywords"] == ["Bob Y", "Ann Z"]


# ---- locator, places, hash -------------------------------------------------

def test_scan_locator_for_a_split_child():
    f = _facts(scan_batch="Batch 00012", scan_sequence=17, split_index=2, split_total=3)
    assert pl.scan_locator(f) == "Batch 00012 #017, print 2 of 3"
    assert pl.scan_locator(_facts()) is None


def test_place_parsing_is_conservative():
    assert pl.parse_place("Toronto, Ontario, Canada") == \
        {"city": "Toronto", "state": "Ontario", "country": "Canada"}
    assert pl.parse_place("Paris, France")["country"] == "France"
    assert pl.parse_place("Springfield, Ohio") == \
        {"city": None, "state": None, "country": None}


def test_camera_gps_suppresses_ours():
    place = pl.PlaceFacts(1, "Lake Louise", 51.4, -116.2)
    assert pl.compose(_facts(places=(place,)))["gps"] == {"lat": 51.4, "lon": -116.2}
    assert pl.compose(_facts(places=(place,), has_exif_gps=True))["gps"] is None


def test_hash_is_stable_and_moves_with_one_fact():
    f = _facts(people={1: _peggy()},
               faces=(pl.FaceFacts(10, 1, {"x": 1, "y": 1, "w": 10, "h": 10}),))
    h1 = pl.payload_hash(pl.compose(f))
    assert h1 == pl.payload_hash(pl.compose(f))
    f.albums = ("Christmas 1970",)
    assert pl.payload_hash(pl.compose(f)) != h1


# ---- regions ---------------------------------------------------------------

@pytest.mark.parametrize("orientation, expect", [
    (1, (0.1, 0.2, 0.3, 0.1)),
    # 6: display = stored rotated 90° CW, so display x is stored (1 - y).
    (6, (0.2, 0.6, 0.1, 0.3)),
    # 8: display = stored rotated 90° CCW, so display y is stored x.
    (8, (0.7, 0.1, 0.1, 0.3)),
    (3, (0.6, 0.7, 0.3, 0.1)),
])
def test_regions_are_mapped_into_the_stored_frame(orientation, expect):
    [r] = rg.to_stored([{"name": "P", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.1}], orientation)
    assert (r.x, r.y, r.w, r.h) == pytest.approx(expect)


def test_dims_match_swaps_for_rotated_files():
    assert rg.dims_match(800, 1000, 1000, 800, 6)
    assert not rg.dims_match(800, 1000, 1000, 800, 1)
    assert not rg.dims_match(None, 1000, 1000, 800, 1)


def test_mwg_areas_are_centre_based():
    s = rg.mwg_struct([rg.StoredRegion("P", 0.1, 0.2, 0.3, 0.1)], 400, 300)
    area = s["RegionList"][0]["Area"]
    assert (area["X"], area["Y"]) == pytest.approx((0.25, 0.25))
    assert s["AppliedToDimensions"] == {"W": 400, "H": 300, "Unit": "pixel"}


# ---- tag plumbing ----------------------------------------------------------

def test_struct_values_are_escaped():
    assert et_mod.struct_string({"Name": "Clay, Peggy [x]"}) == "{Name=Clay|, Peggy |[x|]}"


def test_an_empty_value_clears_the_tag_and_text_goes_through_a_file(tmp_path):
    args = et_mod.write_args({"XMP-dc:Subject": None,
                              "XMP-dc:Description": "a\nb",
                              "IPTC:Keywords": ["A", "B"]},
                             text_file=tmp_path / "t.txt")
    assert "-XMP-dc:Subject=" in args
    assert f"-XMP-dc:Description<={tmp_path / 't.txt'}" in args
    assert args[:3] == ["-charset", "iptc=UTF8", "-IPTC:CodedCharacterSet=UTF8"]
    assert "-IPTC:Keywords=A" in args and "-IPTC:Keywords=B" in args


def test_camera_exif_is_never_claimed():
    payload = pl.compose(_facts(capture_date=date(2001, 6, 12),
                                capture_date_precision="exact",
                                capture_date_confirmed=True,
                                exif_taken_at="2001:06:12 14:03:09"))
    camera = {et_mod.EXIF_DATE_TAG: "2001:06:11 09:00:00"}
    tags, notes = et_mod.desired_tags(payload, payload_hash="h", current=camera,
                                      stored_w=10, stored_h=10, orientation=1,
                                      dims_ok=True)
    assert et_mod.EXIF_DATE_TAG not in tags, "the camera's own date is left alone"
    assert any("differs" in n for n in notes)
    ours = {et_mod.EXIF_DATE_TAG: "2001:06:11 09:00:00",
            et_mod.WROTE_EXIF_TAG: ["DateTimeOriginal"]}
    tags, _ = et_mod.desired_tags(payload, payload_hash="h", current=ours,
                                  stored_w=10, stored_h=10, orientation=1, dims_ok=True)
    assert tags[et_mod.EXIF_DATE_TAG] == "2001:06:12 14:03:09"


def test_a_scan_date_counts_only_when_a_person_set_it():
    scan = dict(is_scan=True, capture_date=date(2025, 11, 7), capture_date_precision="exact",
                capture_date_confirmed=True, exif_taken_at="2025:11:07 12:07:00")
    assert pl.date_fields(_facts(**scan)) is None
    d = pl.date_fields(_facts(**scan, date_set_by_human=True))
    assert d["xmp"].startswith("2025-11-07") and d["exif_datetime"] is None


def test_two_people_with_the_same_names_are_both_listed():
    """Batch 00033 #2608: father and son both 'George Frederick Clay'."""
    a = pl.PersonFacts(id=1, given_name="George", middle_name="Frederick", surname="Clay")
    b = pl.PersonFacts(id=2, given_name="George", middle_name="Frederick", surname="Clay")
    f = _facts(people={1: a, 2: b}, faces=(
        pl.FaceFacts(1, 1, {"x": 10, "y": 0, "w": 10, "h": 10}),
        pl.FaceFacts(2, 2, {"x": 90, "y": 0, "w": 10, "h": 10}),
    ))
    out = pl.compose(f)
    assert out["persons_in_image"] == ["George Frederick Clay"] * 2
    assert out["keywords"] == ["George Clay"] * 2
    assert len(out["regions"]) == 2
