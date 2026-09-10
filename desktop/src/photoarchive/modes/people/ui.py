"""Fix-up 9 People sidebar mode.

Searchable list on the left (name, face count, birth/death years),
editor on the right. Editor covers every name field (given, middle,
surname, maiden, nickname, suffix), birth/death year, notes, and name
variants (add / remove). Buttons: New, Save, Merge into…, Show all
photos.

Every edit writes an audit row via `repo`; the panel refreshes the
list on each save so counts stay correct.
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ... import db as dbmod
from ..faces import repo
from ..faces.merge import merge_people
from ..faces.person_dialog import MergePeopleDialog

log = logging.getLogger(__name__)


class PeoplePanel(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._current_person_id: int | None = None
        self._people_cache: list[repo.PersonRow] = []
        self._face_counts: dict[int, int] = {}

        outer = QVBoxLayout(self)

        toolbar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search people…")
        self.search.textChanged.connect(self._refresh_list)
        toolbar.addWidget(self.search, 1)

        self.new_btn = QPushButton("New person")
        self.new_btn.clicked.connect(self._new_person)
        toolbar.addWidget(self.new_btn)

        self.reload_btn = QPushButton("Reload")
        self.reload_btn.clicked.connect(self._reload)
        toolbar.addWidget(self.reload_btn)

        outer.addLayout(toolbar)

        splitter = QSplitter(Qt.Horizontal)

        # Left — list.
        self.list = QListWidget()
        self.list.currentItemChanged.connect(self._on_selection_changed)
        splitter.addWidget(self.list)

        # Right — editor.
        editor = QWidget()
        editor_layout = QVBoxLayout(editor)
        form = QFormLayout()
        self.f_given = QLineEdit()
        self.f_middle = QLineEdit()
        self.f_surname = QLineEdit()
        self.f_suffix = QLineEdit()
        self.f_suffix.setPlaceholderText("Jr., II, III…")
        self.f_maiden = QLineEdit()
        self.f_nickname = QLineEdit()
        self.f_birth = QSpinBox(); self.f_birth.setRange(0, 3000); self.f_birth.setSpecialValueText("—")
        self.f_death = QSpinBox(); self.f_death.setRange(0, 3000); self.f_death.setSpecialValueText("—")
        self.f_notes = QTextEdit(); self.f_notes.setFixedHeight(70)
        form.addRow("Given", self.f_given)
        form.addRow("Middle", self.f_middle)
        form.addRow("Surname", self.f_surname)
        form.addRow("Suffix", self.f_suffix)
        form.addRow("Maiden", self.f_maiden)
        form.addRow("Nickname", self.f_nickname)
        form.addRow("Birth year", self.f_birth)
        form.addRow("Death year", self.f_death)
        form.addRow("Notes", self.f_notes)
        editor_layout.addLayout(form)

        # Variants sub-panel
        editor_layout.addWidget(QLabel("Name variants"))
        self.variants_list = QListWidget()
        self.variants_list.setFixedHeight(80)
        editor_layout.addWidget(self.variants_list)
        variants_row = QHBoxLayout()
        add_variant_btn = QPushButton("Add variant…")
        add_variant_btn.clicked.connect(self._add_variant)
        remove_variant_btn = QPushButton("Remove selected variant")
        remove_variant_btn.clicked.connect(self._remove_variant)
        variants_row.addWidget(add_variant_btn)
        variants_row.addWidget(remove_variant_btn)
        variants_row.addStretch(1)
        editor_layout.addLayout(variants_row)

        actions = QHBoxLayout()
        self.save_btn = QPushButton("Save changes")
        self.save_btn.clicked.connect(self._save)
        actions.addWidget(self.save_btn)

        self.merge_btn = QPushButton("Merge into…")
        self.merge_btn.clicked.connect(self._merge)
        actions.addWidget(self.merge_btn)

        self.photos_btn = QPushButton("Show all photos")
        self.photos_btn.clicked.connect(self._show_photos)
        actions.addWidget(self.photos_btn)

        actions.addStretch(1)
        editor_layout.addLayout(actions)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        editor_layout.addWidget(self.status)

        splitter.addWidget(editor)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        outer.addWidget(splitter, 1)

        QTimer.singleShot(0, self._reload)

    # --- list / selection -------------------------------------------------

    def _reload(self) -> None:
        with dbmod.connection() as conn:
            conn.autocommit = True
            self._people_cache = repo.list_people(conn)
            self._face_counts = {
                p.id: repo.face_count_for_person(conn, p.id)
                for p in self._people_cache
            }
        self._refresh_list(self.search.text())

    def _refresh_list(self, query: str) -> None:
        self.list.clear()
        q = (query or "").strip().lower()
        for p in self._people_cache:
            if q and q not in (p.display_name or "").lower():
                continue
            count = self._face_counts.get(p.id, 0)
            years = ""
            if p.birth_year or p.death_year:
                years = f"  ({p.birth_year or '?'} – {p.death_year or ''})"
            item = QListWidgetItem(f"{p.display_name}  · {count} face(s){years}")
            item.setData(Qt.UserRole, p.id)
            self.list.addItem(item)
        if self.list.count() > 0:
            self.list.setCurrentRow(0)
        else:
            self._current_person_id = None
            self._paint_editor(None)

    def _on_selection_changed(self, current, _prev) -> None:
        if current is None:
            self._current_person_id = None
            self._paint_editor(None)
            return
        pid = int(current.data(Qt.UserRole))
        self._current_person_id = pid
        with dbmod.connection() as conn:
            conn.autocommit = True
            person = repo.get_person(conn, pid)
        self._paint_editor(person)

    def _paint_editor(self, person: repo.PersonRow | None) -> None:
        self.f_given.setText(person.given_name if person else "")
        self.f_middle.setText(person.middle_name if (person and person.middle_name) else "")
        self.f_surname.setText(person.surname if person else "")
        self.f_suffix.setText(person.suffix if (person and person.suffix) else "")
        self.f_maiden.setText(person.maiden_name if (person and person.maiden_name) else "")
        self.f_nickname.setText(person.nickname if (person and person.nickname) else "")
        self.f_birth.setValue(person.birth_year if (person and person.birth_year) else 0)
        self.f_death.setValue(person.death_year if (person and person.death_year) else 0)
        self.f_notes.setPlainText(person.notes if (person and person.notes) else "")
        self.variants_list.clear()
        if person is not None:
            with dbmod.connection() as conn:
                conn.autocommit = True
                variants = repo.list_name_variants(conn, person.id)
            for vid, variant, kind in variants:
                item = QListWidgetItem(f"{variant}  ({kind})")
                item.setData(Qt.UserRole, vid)
                self.variants_list.addItem(item)
        for w in (self.save_btn, self.merge_btn, self.photos_btn):
            w.setEnabled(person is not None)
        self.status.setText("")

    # --- actions ---------------------------------------------------------

    def _values(self) -> dict[str, Any]:
        def _txt(w: QLineEdit) -> str | None:
            v = w.text().strip()
            return v or None
        def _year(w: QSpinBox) -> int | None:
            v = w.value()
            return v if v > 0 else None
        return {
            "given_name": _txt(self.f_given),
            "middle_name": _txt(self.f_middle),
            "surname": _txt(self.f_surname),
            "suffix": _txt(self.f_suffix),
            "maiden_name": _txt(self.f_maiden),
            "nickname": _txt(self.f_nickname),
            "birth_year": _year(self.f_birth),
            "death_year": _year(self.f_death),
            "notes": self.f_notes.toPlainText().strip() or None,
        }

    def _new_person(self) -> None:
        from ..faces.person_dialog import PersonDialog
        dlg = PersonDialog(parent=self)
        if dlg.exec() != QDialog.Accepted:
            return
        with dbmod.connection() as conn:
            conn.autocommit = False
            try:
                pid = repo.create_person(conn, **dlg.values())
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        self._reload()
        # Select the freshly created person.
        for i in range(self.list.count()):
            it = self.list.item(i)
            if int(it.data(Qt.UserRole)) == pid:
                self.list.setCurrentRow(i)
                break

    def _save(self) -> None:
        if self._current_person_id is None:
            return
        vals = self._values()
        if not (vals["given_name"] or vals["surname"]):
            QMessageBox.warning(self, "People",
                                "Enter at least a given name or surname.")
            return
        with dbmod.connection() as conn:
            conn.autocommit = False
            try:
                repo.update_person(conn, self._current_person_id, **vals)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        self._reload()
        self.status.setText("Saved.")

    def _merge(self) -> None:
        if self._current_person_id is None:
            return
        with dbmod.connection() as conn:
            conn.autocommit = True
            people = repo.list_people(conn)
        if len(people) < 2:
            QMessageBox.information(self, "Merge", "Need at least two people to merge.")
            return
        dlg = MergePeopleDialog(
            people, default_winner_id=self._current_person_id, parent=self,
        )
        if dlg.exec() != QDialog.Accepted:
            return
        values = dlg.values()
        with dbmod.connection() as conn:
            conn.autocommit = False
            try:
                out = merge_people(conn, **values)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        QMessageBox.information(
            self, "Merge",
            f"Moved {out['faces_moved']} face(s) and {out['variants_moved']} variant(s).",
        )
        self._reload()

    def _show_photos(self) -> None:
        if self._current_person_id is None:
            return
        with dbmod.connection() as conn:
            conn.autocommit = True
            ids = repo.photos_for_person(conn, self._current_person_id)
        head = ", ".join(str(i) for i in ids[:20])
        more = f" · and {len(ids) - 20} more" if len(ids) > 20 else ""
        self.status.setText(f"{len(ids)} photo(s): {head}{more}")
        log.info("people.show_photos: person=%s ids=%s", self._current_person_id, ids)

    def _add_variant(self) -> None:
        if self._current_person_id is None:
            return
        text, ok = QInputDialog.getText(self, "Add name variant",
                                        "Variant (e.g. 'Peggy'):")
        if not ok or not text.strip():
            return
        with dbmod.connection() as conn:
            conn.autocommit = False
            try:
                new_id = repo.add_name_variant(
                    conn, person_id=self._current_person_id,
                    variant=text.strip(), kind="nickname",
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        if new_id is None:
            self.status.setText("(variant already exists on this person)")
        else:
            self.status.setText(f"Added variant '{text.strip()}'.")
        # Repaint the current person to refresh variants.
        current = self.list.currentItem()
        if current is not None:
            self._on_selection_changed(current, None)

    def _remove_variant(self) -> None:
        if self._current_person_id is None:
            return
        item = self.variants_list.currentItem()
        if item is None:
            return
        vid = int(item.data(Qt.UserRole))
        with dbmod.connection() as conn:
            conn.autocommit = False
            try:
                repo.remove_name_variant(conn, vid)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        self.status.setText("Removed variant.")
        current = self.list.currentItem()
        if current is not None:
            self._on_selection_changed(current, None)
