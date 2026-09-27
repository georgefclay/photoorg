"""The run report: counts, timings, op frequency, and before/after pairs.

This is what George reads before authorising the full-scope run, so it has to
actually contain the numbers and the images.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from photoarchive.modes.cleanup import job as job_mod
from photoarchive.modes.cleanup import paths as cpaths
from photoarchive.modes.cleanup import report as report_mod

from .conftest import DB_AVAILABLE, TEST_DATABASE_URL
from .test_cleanup_accept import (
    _FakeGuard, _init_pool, _mk_scan_photo, _reset, _test_settings,
)

pytestmark = pytest.mark.skipif(not DB_AVAILABLE, reason="no TEST_DATABASE_URL")


@pytest.fixture(autouse=True)
def _guard_passes(monkeypatch):
    monkeypatch.setattr(job_mod, "run_masters_guard",
                        lambda roots: _FakeGuard(roots))


def test_the_report_carries_the_counts_timings_and_pairs(tmp_path):
    settings = _test_settings(tmp_path, TEST_DATABASE_URL)
    _init_pool(settings)
    _reset(TEST_DATABASE_URL)
    _mk_scan_photo(settings, sha="a" * 64, angle=3.0,
                   filename="IMG060.jpg", scan_sequence=60)
    _mk_scan_photo(settings, sha="b" * 64, angle=4.0,
                   filename="IMG061.jpg", scan_sequence=61)
    stats = job_mod.run_cleanup_analyse(settings, write_previews=False)

    paths = report_mod.write_report(
        settings, stats=stats.to_dict(), samples=2,
        title="Cleanup analysis — Batch 00001",
    )

    assert paths.directory.is_relative_to(cpaths.report_dir(settings, ""). parent)
    md = paths.markdown.read_text(encoding="utf-8")
    assert "Cleanup analysis" in md
    assert "Counts by status" in md
    assert "Op frequency" in md
    assert "deskew" in md and "crop" in md
    assert "Timing" in md
    assert "median" in md

    summary = json.loads(paths.summary.read_text(encoding="utf-8"))
    assert summary["scope_total"] == 2
    assert summary["pending_in_queue"] == 2
    assert summary["op_frequency"].get("deskew") == 2
    assert summary["run_stats"]["analysed"] == 2
    assert summary["remote_spend_usd"] == 0.0
    assert len(summary["samples"]) == 2

    sheet = paths.contact_sheet.read_text(encoding="utf-8")
    assert "<title>" in sheet
    # The CSS braces survived the f-string.
    assert "grid-template-columns: 1fr 1fr" in sheet
    for sample in summary["samples"]:
        for name in (sample["before"], sample["after"]):
            img = paths.directory / name
            assert img.exists(), name
            with Image.open(img) as im:
                assert max(im.size) <= report_mod.PAIR_EDGE
            assert name in sheet


def test_samples_are_spread_across_the_op_kinds(tmp_path):
    """One boring deskew tells George nothing about the colour work."""
    settings = _test_settings(tmp_path, TEST_DATABASE_URL)
    _init_pool(settings)
    _reset(TEST_DATABASE_URL)

    import numpy as np
    geo_ids = []
    for i in range(3):
        pid = _mk_scan_photo(settings, sha=f"{i}" * 64, angle=3.0,
                             filename=f"IMG07{i}.jpg", scan_sequence=70 + i)
        geo_ids.append(pid)
    # One faded scan, so there is a second op-kind bucket.
    faded_id = _mk_scan_photo(settings, sha="f" * 64, angle=3.0,
                              filename="IMG079.jpg", scan_sequence=79)
    from .test_cleanup_accept import _photo_row
    path = Path(_photo_row(TEST_DATABASE_URL, faded_id)["working_path"])
    with Image.open(path) as im:
        arr = np.asarray(im).astype(np.float32)
    Image.fromarray(np.clip(arr * 0.3 + 120.0, 0, 255).astype(np.uint8)).save(
        path, "JPEG", quality=96, subsampling=0)

    job_mod.run_cleanup_analyse(settings, write_previews=False)
    paths = report_mod.write_report(settings, samples=2)
    summary = json.loads(paths.summary.read_text(encoding="utf-8"))

    kinds = {",".join(s["ops"]) for s in summary["samples"]}
    assert len(kinds) == 2, f"expected one sample per op-kind bucket, got {kinds}"


def test_a_report_with_an_empty_queue_still_writes(tmp_path):
    settings = _test_settings(tmp_path, TEST_DATABASE_URL)
    _init_pool(settings)
    _reset(TEST_DATABASE_URL)
    paths = report_mod.write_report(settings, samples=5)
    assert paths.markdown.exists()
    summary = json.loads(paths.summary.read_text(encoding="utf-8"))
    assert summary["pending_in_queue"] == 0
    assert summary["samples"] == []
    assert "No samples rendered." in paths.contact_sheet.read_text(encoding="utf-8")
