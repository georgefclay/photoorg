from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QWidget,
)

from .. import db
from ..config import Settings
from ..modes.ingest.guard import GuardResult, run_masters_guard
from .log_dock import LogDock
from .mode_registry import default_modes
from .settings_dialog import SettingsDialog

log = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings, env_path: Path) -> None:
        super().__init__()
        self.settings = settings
        self.env_path = env_path
        self.setWindowTitle("Photo Archive")
        self.resize(1400, 900)

        splitter = QSplitter(Qt.Horizontal, self)
        self.sidebar = QListWidget()
        self.sidebar.setFixedWidth(140)
        self.stack = QStackedWidget()
        splitter.addWidget(self.sidebar)
        splitter.addWidget(self.stack)
        splitter.setStretchFactor(1, 1)
        self.setCentralWidget(splitter)

        self._modes = default_modes()
        for m in self._modes:
            item = QListWidgetItem(m.label)
            self.sidebar.addItem(item)
            widget = m.factory()
            self.stack.addWidget(widget)
        self.sidebar.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.sidebar.setCurrentRow(0)

        # Log dock
        self.log_dock = LogDock(self)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.log_dock)

        # Menu: Settings
        settings_menu = self.menuBar().addMenu("&Settings")
        edit = QAction("Edit .env…", self)
        edit.triggered.connect(self._open_settings)
        settings_menu.addAction(edit)

        # Status bar
        self.setStatusBar(QStatusBar())
        self.status_db = QLabel("DB: ?")
        self.status_guard = QLabel("Guard: ?")
        self.status_counts = QLabel("photos 0 | untriaged 0 | backs 0")
        self.status_pending = QLabel("proposals: 0")
        for w in (self.status_db, self.status_guard, self.status_counts, self.status_pending):
            self.statusBar().addPermanentWidget(w)

        # First status refresh after the event loop starts (so the window paints).
        QTimer.singleShot(0, self._refresh_status)

        self._guard_state: GuardResult | None = None

    def _open_settings(self) -> None:
        photos_exist = False
        try:
            with db.connection() as conn:
                row = conn.execute("select 1 from photos limit 1").fetchone()
                photos_exist = row is not None
        except Exception:
            log.debug("Could not check photos table for settings dialog", exc_info=True)
        dlg = SettingsDialog(self.settings, self.env_path, photos_exist, self)
        dlg.exec()

    def _refresh_status(self) -> None:
        # DB
        try:
            with db.connection() as conn:
                photos = conn.execute("select count(*) from photos where not is_deleted").fetchone()[0]
                untriaged = conn.execute(
                    "select count(*) from photos where not is_deleted and triage_status = 'untriaged'"
                ).fetchone()[0]
                backs = conn.execute("select count(*) from photo_backs").fetchone()[0]
                pending = conn.execute(
                    """
                    select
                      (select count(*) from ingest_pairings where status = 'pending')
                    + (select count(*) from ingest_rescans where status = 'pending')
                    """
                ).fetchone()[0]
            self.status_db.setText("DB: ok")
            self.status_counts.setText(
                f"photos {photos} | untriaged {untriaged} | backs {backs}"
            )
            self.status_pending.setText(f"proposals: {pending}")
        except Exception as e:
            self.status_db.setText(f"DB: down ({e.__class__.__name__})")

        # Guard: run cheap non-destructive check on every refresh (probe-and-remove)
        try:
            self._guard_state = run_masters_guard(self.settings.master_roots)
            if self._guard_state.all_read_only:
                self.status_guard.setText("Guard: read-only")
                self.status_guard.setStyleSheet("color: green")
            elif self._guard_state.missing_labels:
                missing = ", ".join(self._guard_state.missing_labels)
                writable = ", ".join(self._guard_state.writable_labels)
                extra = f"; writable: {writable}" if writable else ""
                self.status_guard.setText(f"Guard: missing ({missing}){extra}")
                self.status_guard.setStyleSheet("color: orange; font-weight: bold")
            else:
                writable = ", ".join(self._guard_state.writable_labels)
                self.status_guard.setText(f"Guard: WRITABLE ({writable})")
                self.status_guard.setStyleSheet("color: red; font-weight: bold")
        except Exception as e:
            self.status_guard.setText(f"Guard: err ({e.__class__.__name__})")

    def closeEvent(self, event):
        try:
            with db.connection() as conn:
                pending = conn.execute(
                    """
                    select
                      (select count(*) from ingest_pairings where status = 'pending')
                    + (select count(*) from ingest_rescans where status = 'pending')
                    """
                ).fetchone()[0]
        except Exception:
            pending = 0
        if pending > 0:
            from PySide6.QtWidgets import QMessageBox
            reply = QMessageBox.question(
                self,
                "Pending proposals",
                f"{pending} ingest proposals still need review. Quit anyway?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                event.ignore()
                return
        self.log_dock.detach()
        super().closeEvent(event)
