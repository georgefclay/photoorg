from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QDockWidget, QPlainTextEdit


class _QtLogHandler(logging.Handler, QObject):
    log_line = Signal(str)

    def __init__(self) -> None:
        logging.Handler.__init__(self)
        QObject.__init__(self)
        self.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-5s %(name)s: %(message)s",
                              datefmt="%H:%M:%S")
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.log_line.emit(self.format(record))
        except Exception:
            self.handleError(record)


class LogDock(QDockWidget):
    """Read-only pane mirroring the root logger at INFO."""

    def __init__(self, parent=None) -> None:
        super().__init__("Log", parent)
        self.setAllowedAreas(Qt.BottomDockWidgetArea | Qt.RightDockWidgetArea)
        self._view = QPlainTextEdit(readOnly=True)
        self._view.setMaximumBlockCount(5000)
        self.setWidget(self._view)

        self._handler = _QtLogHandler()
        self._handler.setLevel(logging.INFO)
        self._handler.log_line.connect(self._view.appendPlainText)
        logging.getLogger().addHandler(self._handler)

    def detach(self) -> None:
        logging.getLogger().removeHandler(self._handler)
