"""Phase 13 fix-up 1, item 3: the desktop's Clear confirmed date button,
offscreen — shown only on a confirmed date, and its click clears the date
through `dates.repo.unconfirm` and leaves the old value re-acceptable."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton

from photoarchive import db as dbmod
from photoarchive.modes.faces import repo as faces_repo
from photoarchive.modes.faces.photo_preview import SuggestionsPanel

from .phase6_fixtures import phase6  # noqa: F401
from .test_date_hygiene import _photo, _photo_row, _suggestions

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


def _buttons(panel) -> dict[str, QPushButton]:
    # set_context drops the previous photo's buttons with deleteLater.
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    return {b.text(): b for b in panel._buttons_holder.findChildren(QPushButton)}


def test_the_button_shows_only_on_a_confirmed_date_and_says_which(phase6):
    _app()
    confirmed = _photo(cd="2004-01-01", taken="2004-01-01 00:00:11", folder="_2004-01")
    guess = _photo(cd=None, folder="Holiday")
    panel = SuggestionsPanel()
    with dbmod.connection() as conn:
        panel.set_context(faces_repo.load_photo_context(conn, confirmed))
    assert "Clear confirmed date" in _buttons(panel)
    assert "Confirmed date: 2004-01-01 (exact)" in panel.body.text()
    seen = []
    panel.clear_date_requested.connect(seen.append)
    _buttons(panel)["Clear confirmed date"].click()
    assert seen == [confirmed]

    with dbmod.connection() as conn:
        panel.set_context(faces_repo.load_photo_context(conn, guess))
    assert "Clear confirmed date" not in _buttons(panel)


def test_the_faces_handler_clears_and_offers_the_old_date_back(phase6, monkeypatch):
    _app()
    from photoarchive.modes.faces import ui as faces_ui

    pid = _photo(cd="2004-01-01", taken="2004-01-01 00:00:11", folder="_2004-01")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    panel = faces_ui.FacesPanel()
    monkeypatch.setattr(panel, "_refresh_preview_context", lambda: None)
    panel._on_clear_date(pid)
    assert _photo_row(pid) == (None, "unknown", False)
    assert panel.status_label.text() == "Date 2004-01-01 cleared; it is now a suggestion."
    with dbmod.connection() as conn:
        ctx = faces_repo.load_photo_context(conn, pid)
    assert not ctx.has_confirmed_date
    assert any(d.date == "2004-01-01" and d.precision == "exact" for d in ctx.date_suggestions)
    assert len(_suggestions(pid)) == 2      # the old value + the folder month
