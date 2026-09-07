"""Small modal dialog to create or edit a person, plus a merge dialog."""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
)

from .repo import PersonRow


class PersonDialog(QDialog):
    def __init__(self, existing: Optional[PersonRow] = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit person" if existing else "New person")
        form = QFormLayout()

        self.given = QLineEdit(existing.given_name if existing else "")
        self.middle = QLineEdit(existing.middle_name if existing else "")
        self.surname = QLineEdit(existing.surname if existing else "")
        self.maiden = QLineEdit(existing.maiden_name if existing else "")
        self.nickname = QLineEdit(existing.nickname if existing else "")
        self.birth = QSpinBox()
        self.birth.setRange(0, 3000)
        self.birth.setSpecialValueText("—")
        self.birth.setValue(existing.birth_year if (existing and existing.birth_year) else 0)
        self.death = QSpinBox()
        self.death.setRange(0, 3000)
        self.death.setSpecialValueText("—")
        self.death.setValue(existing.death_year if (existing and existing.death_year) else 0)
        self.notes = QTextEdit(existing.notes if existing else "")
        self.notes.setFixedHeight(80)

        form.addRow("Given", self.given)
        form.addRow("Middle", self.middle)
        form.addRow("Surname", self.surname)
        form.addRow("Maiden", self.maiden)
        form.addRow("Nickname", self.nickname)
        form.addRow("Birth year", self.birth)
        form.addRow("Death year", self.death)
        form.addRow("Notes", self.notes)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _on_ok(self) -> None:
        # Given + surname minimum (per phase 6 spec).
        if not (self.given.text().strip() or self.surname.text().strip()):
            QMessageBox.warning(self, "Person",
                                "Enter at least a given name or surname.")
            return
        self.accept()

    def values(self) -> dict:
        def _txt(w) -> str | None:
            v = w.text().strip()
            return v or None
        def _year(w) -> int | None:
            v = w.value()
            return v if v > 0 else None
        return {
            "given_name": _txt(self.given),
            "middle_name": _txt(self.middle),
            "surname": _txt(self.surname),
            "maiden_name": _txt(self.maiden),
            "nickname": _txt(self.nickname),
            "birth_year": _year(self.birth),
            "death_year": _year(self.death),
            "notes": self.notes.toPlainText().strip() or None,
        }


class MergePeopleDialog(QDialog):
    def __init__(self, people: list[PersonRow], default_winner_id: int | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Merge two people")
        form = QFormLayout()
        self.winner = QComboBox()
        self.loser = QComboBox()
        for p in people:
            label = f"{p.display_name} (id {p.id})"
            self.winner.addItem(label, p.id)
            self.loser.addItem(label, p.id)
        if default_winner_id is not None:
            idx = self.winner.findData(default_winner_id)
            if idx >= 0:
                self.winner.setCurrentIndex(idx)
        form.addRow("Keep (winner)", self.winner)
        form.addRow("Merge into it (loser)", self.loser)
        self.reason = QLineEdit("duplicate_person")
        form.addRow("Reason", self.reason)

        note = QLabel("The loser is soft-deleted. Faces and name variants "
                      "move onto the winner. Audit rows record the merge.")
        note.setWordWrap(True)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(buttons)

    def _on_ok(self) -> None:
        if self.winner.currentData() == self.loser.currentData():
            QMessageBox.warning(self, "Merge",
                                "Winner and loser must be different people.")
            return
        self.accept()

    def values(self) -> dict:
        return {
            "winner_id": int(self.winner.currentData()),
            "loser_id": int(self.loser.currentData()),
            "reason": self.reason.text().strip() or "duplicate_person",
        }
