"""Keeper scoring: the five rules, their ordering, and the reason chain."""
from __future__ import annotations

from datetime import datetime

from photoarchive.modes.dedupe.keeper import MemberFacts, pick_keeper


def _m(**overrides) -> MemberFacts:
    base = dict(
        photo_id=1, is_scan=False, mime="image/jpeg",
        width=1000, height=1000, file_size=1_000_000,
        exif_taken_at=None, exif_camera=None,
    )
    base.update(overrides)
    return MemberFacts(**base)


def test_exif_original_beats_more_pixels():
    """Rule 1 (EXIF) trumps rule 3 (more pixels)."""
    exif = _m(
        photo_id=1, exif_taken_at=datetime(2010, 6, 1), exif_camera="Canon",
        width=1000, height=1000,
    )
    huge_scan = _m(
        photo_id=2, is_scan=True, mime="image/tiff",
        width=8000, height=8000, file_size=100_000_000,
    )
    result = pick_keeper([exif, huge_scan])
    assert result.keeper_id == 1
    assert "EXIF" in result.reason


def test_tiff_beats_jpg_when_no_exif():
    tiff = _m(photo_id=1, mime="image/tiff", file_size=5_000_000)
    jpg = _m(photo_id=2, mime="image/jpeg", file_size=5_000_000)
    result = pick_keeper([tiff, jpg])
    assert result.keeper_id == 1
    assert "TIFF" in result.reason


def test_more_pixels_beats_larger_file_when_neither_tiff_nor_exif():
    small = _m(photo_id=1, width=1000, height=1000, file_size=10_000_000)
    big = _m(photo_id=2, width=4000, height=4000, file_size=1_000_000)
    result = pick_keeper([small, big])
    assert result.keeper_id == 2
    assert "pixels" in result.reason


def test_larger_file_wins_when_pixels_equal():
    a = _m(photo_id=1, file_size=1_000_000)
    b = _m(photo_id=2, file_size=5_000_000)
    result = pick_keeper([a, b])
    assert result.keeper_id == 2
    assert "file size" in result.reason


def test_scan_beats_digital_when_no_exif_and_all_else_equal():
    """Rule 5: scans carry a physical ref; use as final tiebreaker."""
    scan = _m(photo_id=1, is_scan=True)
    digital = _m(photo_id=2, is_scan=False)
    result = pick_keeper([scan, digital])
    assert result.keeper_id == 1
    assert "scan" in result.reason


def test_deterministic_tiebreak_by_lower_id_when_all_equal():
    a = _m(photo_id=5)
    b = _m(photo_id=2)
    c = _m(photo_id=9)
    result = pick_keeper([a, b, c])
    assert result.keeper_id == 2
    assert result.reason == "lower id"


def test_reason_chain_has_multiple_factors():
    winner = _m(
        photo_id=1, mime="image/tiff",
        exif_taken_at=datetime(2010, 1, 1), exif_camera="Nikon",
        width=4000, height=3000,
    )
    loser = _m(
        photo_id=2, mime="image/jpeg", width=2000, height=1500,
    )
    result = pick_keeper([winner, loser])
    assert result.keeper_id == 1
    assert "EXIF" in result.reason
    assert "TIFF" in result.reason
    assert ">" in result.reason


def test_ordering_lower_beats_higher_only_at_terminal_tie():
    """If two members are equally-ranked on rule 5, lower id wins."""
    a = _m(photo_id=7, is_scan=True)
    b = _m(photo_id=3, is_scan=True)
    result = pick_keeper([a, b])
    assert result.keeper_id == 3
