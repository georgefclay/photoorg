"""GUI-level checks for the Metadata mode and the Sync tab's offer, offscreen.

The wiring the unit tests cannot see: the panel builds against the real
database, "What's embedded" reads a written file back and marks the
archive's own tags, the runner turns the CLI's output into progress, and
the Sync tab says how many files will re-push before George presses it.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from photoarchive.modes.metadata import ui as ui_mod

from .test_metadata_writer import EXIFTOOL, _photo, _write, mw  # noqa: F401
from .phase6_fixtures import phase6  # noqa: F401

pytestmark = [
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
    pytest.mark.skipif(not EXIFTOOL, reason="exiftool not installed"),
]


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


def _wait_jobs(panel) -> None:
    for job in list(panel._jobs):
        job.wait(10_000)
    _app().processEvents()


def test_whats_embedded_reads_the_file_and_marks_our_tags(mw, monkeypatch):
    _app()
    monkeypatch.setenv("EXIFTOOL_PATH", EXIFTOOL)
    pid = _photo(mw, description_ai="A porch.")
    _write(mw)
    panel = ui_mod.MetadataPanel()
    _wait_jobs(panel)
    assert "up to date" in panel._due.text()
    assert panel._last.text().startswith("Last run: never")

    panel._pid.setValue(pid)
    panel._read_embedded()
    rows = {panel._table.item(i, 0).text(): panel._table.item(i, 1).text()
            for i in range(panel._table.rowCount())}
    assert rows["XMP-photoarchive:PhotoId"] == str(pid)
    assert rows["XMP-dc:Description"] == "AI-generated description: A porch."
    first = panel._table.item(0, 0)
    assert first.font().bold(), "the archive's own tags are listed first, in bold"
    assert "metadata current" in panel._status_right.text()


def test_the_runner_turns_cli_output_into_progress():
    _app()
    r = ui_mod.MetadataRunner()
    seen = []
    r.progress.connect(lambda d, t: seen.append((d, t)))
    r._consume("12 photos in scope; 3 due (never written 3).\nChunk 1/1: 3 photos\n")
    r._consume("  [1/3] photo 5: written v2 (9 tags)\n  [2/3] photo 6: skip")
    r._consume("ped — missing file\nphoto 7 (never written): 4 tags\nprogress: 3/3\n")
    assert seen == [(0, 3), (1, 3), (2, 3), (3, 3), (3, 3)]


def test_the_sync_tab_offers_the_write_and_says_what_re_pushes(phase6):
    _app()
    from photoarchive.modes.sync import SyncPanel
    panel = SyncPanel()
    assert not panel._btn_meta.isVisibleTo(panel)
    panel._show_metadata_due({"due": 3, "in_group": 1})
    assert panel._btn_meta.isVisibleTo(panel)
    assert panel._btn_meta.text() == "Write metadata for 3 changed photos"
    assert "3 files will re-push" in panel._meta_due.text()
    assert "1 on the next push" in panel._meta_due.text()
    panel._show_metadata_due({"due": 0, "in_group": 0})
    assert not panel._btn_meta.isVisibleTo(panel)
