"""Burst grouping over synthetic EXIF timestamps + pHashes."""
from __future__ import annotations

from datetime import datetime, timedelta

from photoarchive.modes.triage.burst import (
    PhotoTime, burst_extras, group_bursts,
)


def _h(bits: str) -> str:
    """Turn a 64-bit pattern into a 64-hex-char pHash string (padded)."""
    n = int(bits, 2)
    return f"{n:064x}"


def test_three_within_two_seconds_and_close_hashes_are_a_burst():
    t0 = datetime(2024, 6, 1, 12, 0, 0)
    photos = [
        PhotoTime(1, t0, _h("0" * 64)),
        PhotoTime(2, t0 + timedelta(seconds=1), _h("0" * 63 + "1")),
        PhotoTime(3, t0 + timedelta(seconds=2), _h("0" * 62 + "11")),
    ]
    groups = group_bursts(photos)
    assert len(groups) == 1
    assert [p.photo_id for p in groups[0]] == [1, 2, 3]


def test_time_gap_too_large_splits_group():
    t0 = datetime(2024, 6, 1, 12, 0, 0)
    photos = [
        PhotoTime(1, t0, _h("0" * 64)),
        PhotoTime(2, t0 + timedelta(seconds=1), _h("0" * 64)),
        PhotoTime(3, t0 + timedelta(seconds=10), _h("0" * 64)),
    ]
    assert group_bursts(photos) == []


def test_distant_hashes_do_not_form_burst():
    t0 = datetime(2024, 6, 1, 12, 0, 0)
    photos = [
        PhotoTime(1, t0, _h("0" * 64)),
        PhotoTime(2, t0 + timedelta(seconds=1), _h("1" * 64)),
        PhotoTime(3, t0 + timedelta(seconds=2), _h("0" * 32 + "1" * 32)),
    ]
    assert group_bursts(photos) == []


def test_missing_timestamps_are_dropped():
    photos = [
        PhotoTime(1, None, _h("0" * 64)),
        PhotoTime(2, None, _h("0" * 64)),
        PhotoTime(3, None, _h("0" * 64)),
    ]
    assert group_bursts(photos) == []


def test_burst_extras_keeps_sharpest_by_laplacian():
    t0 = datetime(2024, 6, 1, 12, 0, 0)
    photos = [
        PhotoTime(1, t0, _h("0" * 64)),
        PhotoTime(2, t0 + timedelta(seconds=1), _h("0" * 63 + "1")),
        PhotoTime(3, t0 + timedelta(seconds=2), _h("0" * 62 + "11")),
    ]
    groups = group_bursts(photos)
    sharpness = {1: 10.0, 2: 40.0, 3: 20.0}  # 2 is sharpest
    extras = burst_extras(groups, sharpness)
    assert {e.photo_id for e in extras} == {1, 3}
    for e in extras:
        assert e.sharpest_photo_id == 2
        assert e.group_size == 3


def test_burst_extras_tiebreaks_on_photo_id_when_sharpness_missing():
    t0 = datetime(2024, 6, 1, 12, 0, 0)
    photos = [
        PhotoTime(5, t0, _h("0" * 64)),
        PhotoTime(2, t0 + timedelta(seconds=1), _h("0" * 63 + "1")),
        PhotoTime(8, t0 + timedelta(seconds=2), _h("0" * 62 + "11")),
    ]
    groups = group_bursts(photos)
    extras = burst_extras(groups, {})  # no sharpness → tiebreak on id
    # Lowest photo_id (2) wins the tiebreak, so 5 and 8 are extras.
    assert {e.photo_id for e in extras} == {5, 8}
    for e in extras:
        assert e.sharpest_photo_id == 2
