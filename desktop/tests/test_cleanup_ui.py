"""GUI-level checks for the Cleanup panel, offscreen.

The panel loads the real queue from the real database, so these catch the
wiring mistakes unit tests can't: the per-op checkboxes reflecting the
proposal, geometry being unavailable on a `needs_manual` scan, splits being
all-or-nothing, the E key's disabled tooltip, and the status bar staying put
rather than flashing.

Worker threads never touch a widget (the Phase 3 lesson): the panel's
background steps return plain dicts and the panel paints on the GUI thread.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEventLoop
from PySide6.QtWidgets import QApplication

from photoarchive import db
from photoarchive.modes.cleanup import job as job_mod
from photoarchive.modes.cleanup import repo

from .conftest import DB_AVAILABLE, TEST_DATABASE_URL
from .test_cleanup_accept import (
    _FakeGuard, _init_pool, _mk_scan_photo, _photo_row, _reset, _test_settings,
)

pytestmark = pytest.mark.skipif(not DB_AVAILABLE, reason="no TEST_DATABASE_URL")


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app  # type: ignore[return-value]


def _pump(ms: int = 400) -> None:
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        QCoreApplication.processEvents(QEventLoop.AllEvents, 20)


@pytest.fixture
def panel(monkeypatch, tmp_path):
    """A CleanupPanel pointed at the test database, with the guard stubbed."""
    settings = _test_settings(tmp_path, TEST_DATABASE_URL)
    _init_pool(settings)
    _reset(TEST_DATABASE_URL)
    monkeypatch.setattr(job_mod, "run_masters_guard",
                        lambda roots: _FakeGuard(roots))
    # The panel and its worker targets each call load_config(); give them the
    # test settings rather than the real .env.
    import photoarchive.config as config_mod
    import photoarchive.modes.cleanup.ui as ui_mod
    monkeypatch.setattr(ui_mod, "load_config", lambda: settings)
    monkeypatch.setattr(config_mod, "load", lambda: settings)
    _app()
    yield settings, ui_mod


def _open_panel(ui_mod):
    p = ui_mod.CleanupPanel()
    p.resize(1200, 800)
    p.show()
    _pump()
    return p


def test_the_panel_opens_empty_and_says_so(panel):
    settings, ui_mod = panel
    p = _open_panel(ui_mod)
    assert "No pending proposals" in p._status_bar.text()
    assert not p._accept_btn.isEnabled()
    assert p._op_boxes == {}
    p.close()


def test_a_pending_proposal_shows_its_ops_as_checkboxes(panel):
    settings, ui_mod = panel
    pid = _mk_scan_photo(settings, sha="a" * 64, angle=3.0)
    job_mod.run_cleanup_analyse(settings, write_previews=False)

    p = _open_panel(ui_mod)
    assert p._proposal is not None and p._proposal.photo_id == pid
    assert set(p._op_boxes) >= {"deskew", "crop"}
    assert all(p._op_boxes[n].isChecked() for n in ("deskew", "crop"))
    assert p._ticked >= {"deskew", "crop"}
    assert p._accept_btn.isEnabled()
    # The caption carries the measured numbers, not adjectives.
    assert "skew" in p._facts.text() and "crop" in p._facts.text()
    assert f"#{pid}" in p._header.text()
    p.close()


def test_unticking_an_op_drops_it_from_the_plan(panel):
    settings, ui_mod = panel
    _mk_scan_photo(settings, sha="b" * 64, angle=3.0)
    job_mod.run_cleanup_analyse(settings, write_previews=False)

    p = _open_panel(ui_mod)
    assert "deskew" in p._ticked
    p._op_boxes["deskew"].setChecked(False)
    _pump(150)
    assert "deskew" not in p._ticked
    assert "crop" in p._ticked
    p.close()


def test_geometry_is_unavailable_on_a_needs_manual_scan(panel):
    settings, ui_mod = panel
    import numpy as np
    from PIL import Image
    pid = _mk_scan_photo(settings, sha="c" * 64, angle=3.0)
    path = Path(_photo_row(TEST_DATABASE_URL, pid)["working_path"])
    with Image.open(path) as im:
        arr = np.asarray(im).copy()
    arr[:, :] = 242
    arr[380:520, 500:700] = 40
    Image.fromarray(arr).save(path, "JPEG", quality=96, subsampling=0)
    job_mod.run_cleanup_analyse(settings, reanalyse=True, write_previews=False)

    p = _open_panel(ui_mod)
    assert p._proposal is not None and p._proposal.needs_manual
    for name in ("deskew", "crop"):
        if name in p._op_boxes:
            assert not p._op_boxes[name].isEnabled()
    assert "deskew" not in p._ticked and "crop" not in p._ticked
    assert "needs manual" in p._facts.text()
    p.close()


def test_the_remote_key_is_disabled_with_a_tooltip_when_no_provider(panel):
    settings, ui_mod = panel
    _mk_scan_photo(settings, sha="d" * 64, angle=3.0)
    job_mod.run_cleanup_analyse(settings, write_previews=False)

    p = _open_panel(ui_mod)
    assert not p._remote_btn.isEnabled()
    assert "CLEANUP_REMOTE_PROVIDER" in p._remote_btn.toolTip()
    # Pressing it anyway says why, in the status bar, and changes nothing.
    p._remote()
    _pump(100)
    assert "Remote enhance is off" in p._status_bar.text()
    p.close()


def test_skip_moves_the_proposal_to_the_back_of_the_pass(panel):
    settings, ui_mod = panel
    first = _mk_scan_photo(settings, sha="e" * 64, angle=3.0,
                           filename="IMG040.jpg", scan_sequence=40)
    second = _mk_scan_photo(settings, sha="f" * 64, angle=4.0,
                            filename="IMG041.jpg", scan_sequence=41)
    job_mod.run_cleanup_analyse(settings, write_previews=False)

    p = _open_panel(ui_mod)
    assert p._proposal.photo_id == first
    p._skip()
    _pump(200)
    assert p._proposal.photo_id == second
    assert "Skipped" in p._last_action
    # Nothing was decided.
    with db.connection() as conn:
        conn.autocommit = True
        assert len(repo.pending_ids(conn)) == 2
    p.close()


def test_accept_advances_the_queue_and_reports_in_the_status_bar(panel):
    settings, ui_mod = panel
    pid = _mk_scan_photo(settings, sha="1" * 64, angle=3.0)
    job_mod.run_cleanup_analyse(settings, write_previews=False)

    p = _open_panel(ui_mod)
    p._accept()
    _pump(400)
    # The decision half persists even though the queue then emptied.
    assert f"photo {pid}" in p._last_action
    assert "v1 → v2" in p._last_action
    assert f"photo {pid}" in p._status_bar.text()
    assert _photo_row(TEST_DATABASE_URL, pid)["file_version"] == 2
    assert "No pending proposals" in p._status_bar.text()
    assert p._proposal is None
    p.close()


def test_the_status_bar_carries_the_remote_spend_and_never_clears(panel):
    settings, ui_mod = panel
    p = _open_panel(ui_mod)
    assert "remote spend: session $0.00" in p._status_bar.text()
    p._say("hello")
    _pump(50)
    assert "hello" in p._status_bar.text()
    # Still there after an event-loop spin — it is a status bar, not a toast.
    _pump(300)
    assert "hello" in p._status_bar.text()
    p.close()


def test_the_last_decision_survives_a_preview_finishing(panel):
    """The Phase 3 lesson: no flash messages. A background render landing must
    not wipe out what George just did."""
    settings, ui_mod = panel
    first = _mk_scan_photo(settings, sha="3" * 64, angle=3.0,
                           filename="IMG050.jpg", scan_sequence=50)
    _mk_scan_photo(settings, sha="4" * 64, angle=4.0,
                   filename="IMG051.jpg", scan_sequence=51)
    job_mod.run_cleanup_analyse(settings, write_previews=False)

    p = _open_panel(ui_mod)
    p._skip()
    _pump(600)   # long enough for the next photo's preview to render and land
    assert "Skipped" in p._status_bar.text(), (
        "the decision must persist through the next preview")
    # …and the context half updated alongside it.
    assert "skew" in p._status_bar.text() or "photo" in p._status_bar.text()
    p.close()


def test_the_manual_queue_toggle_disables_the_decision_buttons(panel):
    settings, ui_mod = panel
    _mk_scan_photo(settings, sha="2" * 64, angle=3.0)
    job_mod.run_cleanup_analyse(settings, write_previews=False)

    p = _open_panel(ui_mod)
    p._manual_btn.setChecked(True)
    _pump(200)
    assert p._manual_mode
    for b in (p._accept_btn, p._reject_btn, p._bulk_batch_btn, p._bulk_all_btn):
        assert not b.isEnabled()
    assert "No manual proposals" in p._status_bar.text()
    p.close()


# --------------------------------------------------------------------------
# Fix-up 4: the Show: filter
# --------------------------------------------------------------------------

def _mixed_queue(settings):
    """One split, one needs-manual, one plain geometric proposal."""
    from .test_cleanup_split import _mk_two_print_photo

    split_id, _rects = _mk_two_print_photo(settings, sha="e" * 64)
    geo_id = _mk_scan_photo(settings, sha="f" * 64, angle=3.0,
                            scan_sequence=8, filename="IMG_geo.jpg")
    # A print that fills almost the whole bed reads as implausible/too small
    # depending on the gate; either way it lands in needs_manual.
    manual_id = _mk_scan_photo(settings, sha="0" * 64, w=1200, h=140,
                               scan_sequence=9, filename="IMG_manual.jpg")
    job_mod.run_cleanup_analyse(settings, write_previews=False)
    return split_id, geo_id, manual_id


def test_the_filter_narrows_the_queue_to_its_kind(panel):
    settings, ui_mod = panel
    split_id, geo_id, manual_id = _mixed_queue(settings)

    _init_pool(settings)
    with db.connection() as conn:
        conn.autocommit = True
        everything = repo.pending_ids(conn, queue_filter="all")
        splits = repo.pending_ids(conn, queue_filter="splits")
        manual = repo.pending_ids(conn, queue_filter="manual")
        geometric = repo.pending_ids(conn, queue_filter="geometric")
        by_filter = repo.pending_counts_by_filter(conn)

        loaded = {i: repo.load_proposal(conn, i) for i in everything}

    assert len(everything) >= 3
    assert {loaded[i].photo_id for i in splits} == {split_id}
    assert all(loaded[i].is_split for i in splits)
    assert all(loaded[i].needs_manual for i in manual)
    assert manual_id in {loaded[i].photo_id for i in manual}

    # Geometric-only is exactly the bulk-acceptable set, so the SQL and the
    # dataclass property must agree — two definitions of one rule is how the
    # bulk button and the filter drift apart.
    assert geo_id in {loaded[i].photo_id for i in geometric}
    for i in everything:
        p = loaded[i]
        expected = (p.is_geometric_only and not p.is_split and not p.needs_manual)
        assert (i in geometric) == expected, (p.photo_id, p.op_names,
                                              p.needs_manual, p.is_split)

    assert by_filter == {"all": len(everything), "splits": len(splits),
                         "manual": len(manual), "geometric": len(geometric)}


def test_choosing_a_filter_reloads_the_queue_and_the_header(panel):
    settings, ui_mod = panel
    split_id, _geo_id, _manual_id = _mixed_queue(settings)

    p = _open_panel(ui_mod)
    all_ids = list(p._proposal_ids)
    assert len(all_ids) >= 3
    assert "showing" in p._header.text()

    idx = p._filter_box.findData("splits")
    assert idx >= 0
    p._filter_box.setCurrentIndex(idx)
    _pump(400)

    assert p._proposal_ids and len(p._proposal_ids) < len(all_ids)
    assert p._proposal is not None and p._proposal.photo_id == split_id
    assert p._cursor == 0, "a new queue starts at its top"
    assert "showing 1 / 1 splits" in p._header.text().lower(), p._header.text()
    # The filmstrip is the queue, so it has to narrow with it.
    assert p._filmstrip.count() == len(p._proposal_ids)
    p.close()


def test_each_filter_carries_its_size_in_the_label(panel):
    settings, ui_mod = panel
    _mixed_queue(settings)
    p = _open_panel(ui_mod)
    labels = [p._filter_box.itemText(i) for i in range(p._filter_box.count())]
    assert any(t.startswith("Splits (1)") for t in labels), labels
    assert any(t.startswith("All pending (") for t in labels), labels
    p.close()


def test_the_choice_survives_a_restart(panel):
    settings, ui_mod = panel
    _mixed_queue(settings)

    p = _open_panel(ui_mod)
    p._filter_box.setCurrentIndex(p._filter_box.findData("splits"))
    _pump(300)
    p.close()

    again = _open_panel(ui_mod)
    assert again._filter_box.currentData() == "splits"
    again.close()

    # Reset, so the stored value doesn't leak into the next test.
    ui_mod.CleanupPanel._qsettings().setValue(ui_mod.QUEUE_FILTER_KEY, "all")


def test_an_unknown_stored_filter_falls_back_to_all(panel):
    """A key dropped in a later version must not leave George looking at an
    empty queue with no way to tell why."""
    settings, ui_mod = panel
    ui_mod.CleanupPanel._qsettings().setValue(ui_mod.QUEUE_FILTER_KEY,
                                              "no-such-filter")
    _mixed_queue(settings)
    p = _open_panel(ui_mod)
    assert p._filter_box.currentData() == "all"
    assert p._proposal_ids
    p.close()
    ui_mod.CleanupPanel._qsettings().setValue(ui_mod.QUEUE_FILTER_KEY, "all")
