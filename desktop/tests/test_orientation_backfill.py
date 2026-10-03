"""Fix-up 7: `photos.orientation` comes from the image, not from a string.

`photos.width/height` are display dims and `faces.bbox` lives in that frame
(Phase 6 fix-up 6). Ingest took the orientation from exifread's human-readable
tag and matched it against a lookup table written in *exiftool's* vocabulary —
exifread says "Rotated 90 CW", the table expected "Rotate 90 CW" — so
`_parse_orientation` returned None for every photo ever ingested. All 12,686
rows had `orientation` NULL, 235 of them held raw dims where display dims
belong, and 280 face boxes sat in the wrong frame with nothing to notice.

Two defences: ingest reads the orientation from `probe_image`, the same probe
that produces the dimensions, so the two cannot disagree; and the parser knows
exifread's actual words.
"""
from __future__ import annotations

import pytest
from PIL import Image

from photoarchive.modes.ingest.exif import _parse_orientation
from photoarchive.modes.ingest.image_io import probe_image


@pytest.mark.parametrize("text,expected", [
    # What exifread actually emits — the spellings that were missing.
    ("Rotated 90 CW", 6),
    ("Rotated 90 CCW", 8),
    ("Rotated 180", 3),
    ("Mirrored vertical", 4),
    ("Mirrored horizontal", 2),
    ("Horizontal (normal)", 1),
    # exiftool's spellings, kept so nothing that used to work stops.
    ("Rotate 90 CW", 6),
    ("Rotate 270 CW", 8),
    ("Rotate 180", 3),
])
def test_the_parser_knows_exifreads_words(text, expected):
    assert _parse_orientation(text) == expected


def test_a_bare_number_still_wins():
    assert _parse_orientation("6") == 6
    assert _parse_orientation("Orientation 8") == 8


@pytest.mark.parametrize("text", ["", None, "nonsense", "Rotated sideways"])
def test_nothing_is_invented_from_an_unparseable_string(text):
    assert _parse_orientation(text) is None


def test_probe_image_is_the_authority_on_orientation(tmp_path):
    """The path ingest now uses: one probe gives the dimensions *and* the
    orientation, so a row cannot end up with display dims and a NULL
    orientation, or raw dims and a set one."""
    path = tmp_path / "rotated.jpg"
    im = Image.new("RGB", (400, 300), (120, 140, 160))
    exif = im.getexif()
    exif[0x0112] = 6                       # displays rotated 90 CW
    im.save(path, "JPEG", exif=exif)

    with Image.open(path) as opened:
        dims = probe_image(opened)
    assert dims.orientation == 6
    assert (dims.master_width, dims.master_height) == (400, 300)
    assert (dims.display_width, dims.display_height) == (300, 400), (
        "display dims are what photos.width/height must hold")


def test_an_unrotated_file_reports_one_not_none(tmp_path):
    """A NULL orientation must mean "never established", not "upright" —
    that conflation is what let 235 photos sit unnoticed."""
    path = tmp_path / "plain.jpg"
    im = Image.new("RGB", (400, 300), (10, 20, 30))
    exif = im.getexif()
    exif[0x0112] = 1
    im.save(path, "JPEG", exif=exif)
    with Image.open(path) as opened:
        dims = probe_image(opened)
    assert dims.orientation == 1
    assert (dims.display_width, dims.display_height) == (400, 300)


def test_a_file_with_no_exif_at_all_has_no_orientation(tmp_path):
    path = tmp_path / "bare.jpg"
    Image.new("RGB", (400, 300), (10, 20, 30)).save(path, "JPEG")
    with Image.open(path) as opened:
        dims = probe_image(opened)
    assert dims.orientation is None
    assert (dims.display_width, dims.display_height) == (400, 300)


def test_ingest_takes_orientation_from_the_probe_not_the_string():
    """Pinning the wiring: the insert must be handed `dims.orientation`.
    `exif.orientation` comes from a text match and was NULL for 12,686
    photos without anything failing."""
    import inspect

    from photoarchive.modes.ingest import service

    src = inspect.getsource(service)
    assert "orientation=dims.orientation" in src
    assert "orientation=exif.orientation" not in src
