"""Fix-up 9 assign-existing-person dialog.

Opens with an empty text field focused. Typing filters live: prefix on
any name field (given, surname, maiden, nickname, suffix) at the top,
then trigram similarity on `display_name` and name variants below. The
AI-suggested person, if provided, is pinned to the top as the first
row so it can be accepted with Enter — but the field itself never
pre-fills with the suggestion (typing replaces the pinned row
completely).

`Enter` on the highlighted row accepts; `Esc` cancels.
"""

from __future__ import annotations

import logging
from typing import Iterable

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
)

from ... import db as dbmod
from . import repo

log = logging.getLogger(__name__)

_SUGGESTED_ROLE = Qt.UserRole + 1


class AssignExistingPersonDialog(QDialog):
    def __init__(
        self,
        *,
        suggested_person: repo.PersonRow | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Assign to person")
        self._suggested = suggested_person
        self._selected_id: int | None = None

        layout = QVBoxLayout(self)
        header = QLabel(
            "Type to filter. Suggested match is pinned at the top; "
            "Enter accepts the highlighted row, Esc cancels."
        )
        header.setWordWrap(True)
        layout.addWidget(header)

        self.field = QLineEdit()
        self.field.setPlaceholderText("Search names, nicknames, variants…")
        self.field.textChanged.connect(self._refresh_list)
        self.field.returnPressed.connect(self._accept_current)
        layout.addWidget(self.field)

        self.list = QListWidget()
        self.list.itemActivated.connect(self._accept_current)
        self.list.installEventFilter(self)
        layout.addWidget(self.list, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept_current)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        # Populate once and focus the field for immediate typing.
        self._refresh_list("")
        QTimer.singleShot(0, self.field.setFocus)

    def selected_person_id(self) -> int | None:
        return self._selected_id

    # --- internals --------------------------------------------------------

    def _refresh_list(self, query: str) -> None:
        self.list.clear()
        # Always keep the suggestion at the top when present. Typing filters
        # the rest but doesn't hide the suggestion — that way George can
        # Enter through when the AI got it right.
        if self._suggested is not None:
            item = QListWidgetItem(f"★ {self._suggested.display_name}")
            item.setData(Qt.UserRole, self._suggested.id)
            item.setData(_SUGGESTED_ROLE, True)
            item.setToolTip("AI-suggested match — Enter accepts")
            self.list.addItem(item)
        with dbmod.connection() as conn:
            conn.autocommit = True
            matches = repo.search_people(conn, query, limit=25)
        seen = {self._suggested.id} if self._suggested else set()
        for person in matches:
            if person.id in seen:
                continue
            item = QListWidgetItem(person.display_name or f"person {person.id}")
            item.setData(Qt.UserRole, person.id)
            self.list.addItem(item)
        if self.list.count() > 0:
            self.list.setCurrentRow(0)

    def _accept_current(self) -> None:
        item = self.list.currentItem()
        if item is None:
            return
        self._selected_id = int(item.data(Qt.UserRole))
        self.accept()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 — Qt override
        # Down/Up on the list widget already works; keep Enter routing
        # to _accept_current.
        if obj is self.list and event.type() == event.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key_Return, Qt.Key_Enter):
                self._accept_current()
                return True
        return super().eventFilter(obj, event)
