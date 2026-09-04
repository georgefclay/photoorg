"""GUI-level reproduction of Phase 3 fix-up 1: the grid must not blank
after the first decision. Runs with the offscreen Qt platform against
TEST_DATABASE_URL.

Tests:
  - After K on the first untriaged photo, the model loses that row and
    keeps the rest; the current index remains valid.
  - The grid view paints (viewport() has non-zero grab).
  - Under the 'keep' filter, decisions leave items in place with a new
    status badge instead of removing them.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import psycopg
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, QCoreApplication, QEventLoop
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication

from photoarchive import db
from photoarchive.config import Settings

from .conftest import DB_AVAILABLE, TEST_DATABASE_URL


pytestmark = pytest.mark.skipif(not DB_AVAILABLE, reason="no TEST_DATABASE_URL")


def _app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app  # type: ignore[return-value]


def _pump(ms: int = 200) -> None:
    """Spin the event loop for a fixed time so worker signals land."""
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        QCoreApplication.processEvents(QEventLoop.AllEvents, 20)


def _test_settings(tmp_path: Path, url: str) -> Settings:
    s = Settings(
        MASTER_ROOTS="dummy=Z:\\",
        WORKING_DIR=tmp_path / "working",
        QUARANTINE_DIR=tmp_path / "quarantine",
        MANUAL_FIX_DIR=tmp_path / "manual-fix",
        THUMBS_DIR=tmp_path / "thumbs",
        DATABASE_URL=url,
        INFERENCE_URL="http://x", INFERENCE_TOKEN="x",
        WEB_API_URL="http://x", WEB_API_TOKEN="x",
    )
    for d in (s.WORKING_DIR, s.QUARANTINE_DIR, s.MANUAL_FIX_DIR, s.THUMBS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    return s


def _reset_and_seed(url: str, settings: Settings, n: int = 5) -> list[int]:
    from photoarchive.modes.ingest.paths import working_path
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("truncate audit_log, triage_hints, photos "
                     "restart identity cascade")
    pids: list[int] = []
    for i in range(n):
        sha = f"{i:064x}"
        with psycopg.connect(url, autocommit=True) as conn:
            pid = conn.execute(
                """
                insert into photos
                  (sha256, source_root, source_folder, source_filename,
                   mime, width, height, working_path, triage_status, is_deleted)
                values (%s, 'tst', '', %s, 'image/jpeg', 100, 100, %s,
                        'untriaged', false)
                returning id
                """,
                (sha, f"stub-{i}.jpg", None),
            ).fetchone()[0]
        wp = working_path(settings, pid, sha, "jpg")
        wp.parent.mkdir(parents=True, exist_ok=True)
        wp.write_bytes(b"stub")
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("update photos set working_path=%s where id=%s",
                         (str(wp), pid))
        pids.append(pid)
    return pids


def _init_pool(url: str, settings: Settings) -> None:
    if getattr(db, "_pool", None) is not None:
        db.close_pool()
    db.init_pool(settings)


def _make_panel(monkeypatch, settings: Settings):
    # Bypass the panel's own load_config() so it uses the temp settings.
    from photoarchive.modes.triage import ui as triage_ui
    monkeypatch.setattr(triage_ui, "load_config", lambda: settings)
    panel = triage_ui.TriagePanel()
    panel.resize(900, 600)
    panel.show()
    _pump(50)
    return panel


def test_first_decision_leaves_grid_alive(monkeypatch, tmp_path):
    app = _app()
    settings = _test_settings(tmp_path, TEST_DATABASE_URL)
    _init_pool(TEST_DATABASE_URL, settings)
    _reset_and_seed(TEST_DATABASE_URL, settings, n=5)

    panel = _make_panel(monkeypatch, settings)
    _pump(100)
    assert panel._grid_model.rowCount() == 5

    # Press K on the current (first) row via the panel's key handler.
    ev = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key_K, Qt.NoModifier)
    panel._handle_key(Qt.Key_K, ev)
    # Wait for the worker to finish and the GUI slot to run.
    _pump(500)

    # Under 'untriaged' filter the decided item must leave the list.
    assert panel._grid_model.rowCount() == 4, (
        "expected the decided row to be removed under the untriaged filter, "
        f"got {panel._grid_model.rowCount()} rows"
    )
    # Current index must remain valid so the viewport paints.
    cur = panel._grid.currentIndex()
    assert cur.isValid(), "current index went invalid after decision"
    assert 0 <= cur.row() < panel._grid_model.rowCount()

    # The viewport must have painted a non-null pixmap grab.
    pix = panel._grid.viewport().grab()
    assert not pix.isNull()
    assert pix.width() > 0 and pix.height() > 0

    db.close_pool()


def test_decision_under_non_untriaged_filter_leaves_item_in_place(
    monkeypatch, tmp_path,
):
    app = _app()
    settings = _test_settings(tmp_path, TEST_DATABASE_URL)
    _init_pool(TEST_DATABASE_URL, settings)
    pids = _reset_and_seed(TEST_DATABASE_URL, settings, n=3)

    # Pre-set all to 'keep' so we can view them under the 'keep' filter.
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("update photos set triage_status='keep' where id = any(%s)",
                     (pids,))

    panel = _make_panel(monkeypatch, settings)

    # Switch to the 'keep' filter.
    from photoarchive.modes.triage.ui import _select_by_data
    _select_by_data(panel._f_status, "keep")
    _pump(100)
    assert panel._grid_model.rowCount() == 3

    # Press J on the first one — it should stay in the list (status filter is
    # 'keep' but the item's new status is 'junk' which doesn't match; per the
    # fix-up spec: "under any other filter, the item stays and shows its new
    # status badge". "any other filter" here means "not untriaged".
    ev = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key_J, Qt.NoModifier)
    panel._handle_key(Qt.Key_J, ev)
    _pump(500)

    assert panel._grid_model.rowCount() == 3
    assert panel._grid_model.rows()[0].triage_status == "junk"

    db.close_pool()
