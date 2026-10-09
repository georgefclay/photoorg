"""Phase 15 Corrections mode — three tabs, one rule.

The rule: nothing in this archive is corrected with SQL again. A folder
named "Canaca" instead of "Canada" became an album name and 139 pending
description suggestions, and putting it right took an evening of
hand-written SQL that landed in the wrong database. So:

* **Find & replace** sweeps every piece of desktop-owned text at once
  (`targets.TARGETS`), previews the exact replacement per row, applies it
  under one batch id with an audit row each, and undoes the batch from
  the audit log — after a restart if need be.
* **Albums** and **Places** are the editors the direct-edit paths were
  missing.

Two UI details that are deliberate. The master-derived group
(`photos.source_folder`, `photos.scan_batch`,
`photo_masters.master_path`) is listed with its count and **no
checkbox**, with the reason on screen: those mirror the read-only master
disk and keep the original spelling on purpose, so the typo's full
extent is visible rather than looking like rows the tool missed. And the
status line has **two halves**, the Phase 7 rule: the left is the last
decision and persists until the next one, the right is context. A search
refresh writes only the right-hand half, so what George just applied or
undid stays on screen — the Phase 3 "no flash messages" lesson.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ... import db as dbmod
from . import albums as albums_repo
from . import places as places_repo
from . import repo
from .targets import READ_ONLY_REASON

log = logging.getLogger(__name__)

_ROW_ROLE = Qt.UserRole + 1


class CorrectionsPanel(QWidget):
    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        tabs.addTab(FindReplaceTab(), "Find && replace")
        tabs.addTab(AlbumsTab(), "Albums")
        tabs.addTab(PlacesTab(), "Places")
        layout.addWidget(tabs)


# ----------------------------------------------------------------------
# Find & replace
# ----------------------------------------------------------------------

class FindReplaceTab(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._groups: list[repo.GroupResult] = []

        outer = QVBoxLayout(self)

        form = QHBoxLayout()
        self.find_field = QLineEdit()
        self.find_field.setPlaceholderText("Find text…")
        self.find_field.returnPressed.connect(self._search)
        self.replace_field = QLineEdit()
        self.replace_field.setPlaceholderText("Replace with…")
        self.replace_field.textChanged.connect(self._repreview)
        self.match_case = QCheckBox("Match case")
        self.match_case.setChecked(True)
        self.search_btn = QPushButton("Search")
        self.search_btn.clicked.connect(self._search)
        form.addWidget(QLabel("Find"))
        form.addWidget(self.find_field, 2)
        form.addWidget(QLabel("Replace"))
        form.addWidget(self.replace_field, 2)
        form.addWidget(self.match_case)
        form.addWidget(self.search_btn)
        outer.addLayout(form)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Where", "Now", "After"])
        self.tree.setColumnWidth(0, 320)
        self.tree.setColumnWidth(1, 300)
        self.tree.itemChanged.connect(self._on_item_changed)
        outer.addWidget(self.tree, 1)

        actions = QHBoxLayout()
        self.apply_btn = QPushButton("Apply")
        self.apply_btn.clicked.connect(self._apply)
        self.apply_btn.setEnabled(False)
        actions.addWidget(self.apply_btn)
        actions.addStretch(1)
        actions.addWidget(QLabel("Undo"))
        self.batches = QComboBox()
        self.batches.setMinimumWidth(320)
        actions.addWidget(self.batches)
        self.undo_btn = QPushButton("Undo this correction")
        self.undo_btn.clicked.connect(self._undo)
        actions.addWidget(self.undo_btn)
        outer.addLayout(actions)

        # Two halves, the Phase 7 rule: the left is the last **decision**
        # and persists until the next one; the right is context (what the
        # search found). A search refresh must never wipe out what George
        # just applied or undid - which it did, until
        # `test_applying_then_undoing_from_the_panel_round_trips` caught
        # `_undo` writing a message and then calling `_search` over it.
        status_row = QHBoxLayout()
        self.decision = QLabel("")
        self.decision.setWordWrap(True)
        self.decision.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.context = QLabel("")
        self.context.setWordWrap(True)
        self.context.setAlignment(Qt.AlignRight | Qt.AlignTop)
        status_row.addWidget(self.decision, 3)
        status_row.addWidget(self.context, 2)
        outer.addLayout(status_row)

        QTimer.singleShot(0, self._reload_batches)

    # -- search / preview ---------------------------------------------

    def _search(self) -> None:
        needle = self.find_field.text()
        if not needle:
            self.context.setText("Type the text to find.")
            return
        with dbmod.connection() as conn:
            conn.autocommit = True
            self._groups = repo.search(
                conn, needle, self.replace_field.text(),
                match_case=self.match_case.isChecked(),
            )
        self._paint_tree()
        total = sum(g.count for g in self._groups)
        editable = sum(g.count for g in self._groups if g.target.editable)
        self.context.setText(
            f"{total} row(s) contain “{needle}” — {editable} editable, "
            f"{total - editable} mirroring the master disk."
            if total else f"Nothing contains “{needle}”."
        )

    def _repreview(self) -> None:
        """Recompute every `After` cell as the replacement is typed, so
        the preview is never stale relative to the field."""
        if not self._groups:
            return
        needle = self.find_field.text()
        replacement = self.replace_field.text()
        match_case = self.match_case.isChecked()
        for group in self._groups:
            for row in group.rows:
                row.new_value = repo.replace_text(row.value, needle, replacement, match_case)
        self._paint_tree()

    def _paint_tree(self) -> None:
        self.tree.blockSignals(True)
        self.tree.clear()
        any_editable = False
        for group in self._groups:
            target = group.target
            head = QTreeWidgetItem(self.tree)
            suffix = f" ({group.count})"
            head.setText(0, target.label + suffix)
            head.setFirstColumnSpanned(False)
            if target.editable:
                any_editable = True
                head.setFlags(head.flags() | Qt.ItemIsUserCheckable)
                head.setCheckState(0, Qt.Checked)
            else:
                # No checkbox at all, and the reason where it would be.
                head.setFlags(head.flags() & ~Qt.ItemIsUserCheckable)
                head.setText(1, "mirrors the master disk — not editable")
                head.setToolTip(1, READ_ONLY_REASON)
            if target.note:
                head.setToolTip(0, target.note)
            head.setData(0, _ROW_ROLE, None)

            for row in group.rows:
                child = QTreeWidgetItem(head)
                child.setText(0, row.context)
                child.setText(1, _elide(row.value))
                child.setText(2, _elide(row.new_value) if target.editable else "—")
                child.setToolTip(1, row.value or "")
                child.setToolTip(2, row.new_value or "")
                child.setData(0, _ROW_ROLE, row)
                if target.editable:
                    child.setFlags(child.flags() | Qt.ItemIsUserCheckable)
                    child.setCheckState(0, Qt.Checked)
                else:
                    child.setFlags(child.flags() & ~Qt.ItemIsUserCheckable)
            if group.truncated:
                # Apply only writes the rows that are listed, so say what
                # to do about the rest rather than leaving the count to be
                # read as "and these were handled too".
                more = QTreeWidgetItem(head)
                more.setText(
                    0,
                    f"… {group.count - len(group.rows)} more not listed — "
                    "Apply changes the rows above, then search again for these",
                )
                more.setFlags(more.flags() & ~Qt.ItemIsUserCheckable)
                more.setData(0, _ROW_ROLE, None)
            head.setExpanded(group.count <= 20)
        self.tree.blockSignals(False)
        self.apply_btn.setEnabled(any_editable)

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        """Ticking a group ticks its rows. Children are the truth at
        apply time, so the group box is a convenience, not a second
        definition of what is selected."""
        if column != 0 or item.parent() is not None:
            return
        state = item.checkState(0)
        self.tree.blockSignals(True)
        for i in range(item.childCount()):
            child = item.child(i)
            if child.flags() & Qt.ItemIsUserCheckable:
                child.setCheckState(0, state)
        self.tree.blockSignals(False)

    def _selected_rows(self) -> list[repo.MatchRow]:
        rows: list[repo.MatchRow] = []
        for i in range(self.tree.topLevelItemCount()):
            head = self.tree.topLevelItem(i)
            for j in range(head.childCount()):
                child = head.child(j)
                row = child.data(0, _ROW_ROLE)
                if row is None:
                    continue
                if not (child.flags() & Qt.ItemIsUserCheckable):
                    continue
                if child.checkState(0) != Qt.Checked:
                    continue
                if row.changed:
                    rows.append(row)
        return rows

    # -- apply / undo --------------------------------------------------

    def _apply(self) -> None:
        rows = self._selected_rows()
        if not rows:
            self.context.setText("Nothing ticked that would change.")
            return
        needle = self.find_field.text()
        replacement = self.replace_field.text()
        confirm = QMessageBox.question(
            self, "Apply correction",
            f"Replace “{needle}” with “{replacement}” in {len(rows)} row(s)?\n\n"
            "Every change is audited and can be undone from this tab.",
        )
        if confirm != QMessageBox.Yes:
            return

        with dbmod.connection() as conn:
            conn.autocommit = False
            try:
                result = repo.apply_correction(
                    conn, needle=needle, replacement=replacement,
                    match_case=self.match_case.isChecked(), rows=rows,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        msg = f"Changed {result.changed} row(s). Batch {result.batch_id[:8]}."
        if result.skipped:
            msg += f" Skipped {len(result.skipped)}: " + "; ".join(result.skipped[:5])
            if len(result.skipped) > 5:
                msg += f" (+{len(result.skipped) - 5} more)"
        self.decision.setText(msg)
        self._reload_batches()
        self._search()

    def _reload_batches(self) -> None:
        with dbmod.connection() as conn:
            conn.autocommit = True
            batches = repo.list_batches(conn)
        self.batches.clear()
        for b in batches:
            when = b.created_at.strftime("%Y-%m-%d %H:%M") if b.created_at else "?"
            label = f"{when} · “{b.search}” → “{b.replace}” · {b.changed} row(s)"
            if b.undone:
                label += " · undone"
            self.batches.addItem(label, b.batch_id)
        self.undo_btn.setEnabled(self.batches.count() > 0)

    def _undo(self) -> None:
        batch_id = self.batches.currentData()
        if not batch_id:
            return
        with dbmod.connection() as conn:
            conn.autocommit = False
            try:
                result = repo.undo_batch(conn, batch_id)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        msg = f"Restored {result.restored} row(s) of batch {batch_id[:8]}."
        if result.skipped_ids:
            msg += (
                f" Left alone (changed since the correction): "
                + "; ".join(result.skipped_ids[:5])
            )
            if len(result.skipped_ids) > 5:
                msg += f" (+{len(result.skipped_ids) - 5} more)"
        self.decision.setText(msg)
        self._reload_batches()
        if self.find_field.text():
            self._search()


def _elide(value: str | None, limit: int = 90) -> str:
    text = (value or "").replace("\n", " ⏎ ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ----------------------------------------------------------------------
# Albums
# ----------------------------------------------------------------------

class AlbumsTab(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._albums: list[albums_repo.AlbumRow] = []
        self._current: int | None = None

        outer = QVBoxLayout(self)
        note = QLabel(
            "Albums are edited here only — the web stays read-only for albums "
            "until the album pull-back exists. Changes reach the site on the "
            "next push."
        )
        note.setWordWrap(True)
        outer.addWidget(note)

        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search albums…")
        self.search.textChanged.connect(lambda _t: self._reload())
        self.show_deleted = QCheckBox("Show deleted")
        self.show_deleted.toggled.connect(lambda _b: self._reload())
        top.addWidget(self.search, 1)
        top.addWidget(self.show_deleted)
        outer.addLayout(top)

        splitter = QSplitter(Qt.Horizontal)
        self.list = QListWidget()
        self.list.currentItemChanged.connect(self._on_selected)
        splitter.addWidget(self.list)

        editor = QWidget()
        ed = QVBoxLayout(editor)
        form = QFormLayout()
        self.f_name = QLineEdit()
        self.f_description = QTextEdit()
        self.f_description.setFixedHeight(60)
        form.addRow("Name", self.f_name)
        form.addRow("Description", self.f_description)
        ed.addLayout(form)

        row1 = QHBoxLayout()
        self.save_btn = QPushButton("Save")
        self.save_btn.clicked.connect(self._save)
        self.delete_btn = QPushButton("Delete album")
        self.delete_btn.clicked.connect(self._toggle_deleted)
        row1.addWidget(self.save_btn)
        row1.addWidget(self.delete_btn)
        row1.addStretch(1)
        ed.addLayout(row1)

        ed.addWidget(QLabel("Photos in this album (drag to reorder)"))
        self.photos = QListWidget()
        self.photos.setDragDropMode(QAbstractItemView.InternalMove)
        self.photos.setSelectionMode(QAbstractItemView.ExtendedSelection)
        ed.addWidget(self.photos, 1)

        row2 = QHBoxLayout()
        self.save_order_btn = QPushButton("Save order")
        self.save_order_btn.clicked.connect(self._save_order)
        self.remove_photo_btn = QPushButton("Remove selected photo(s)")
        self.remove_photo_btn.clicked.connect(self._remove_photos)
        row2.addWidget(self.save_order_btn)
        row2.addWidget(self.remove_photo_btn)
        row2.addStretch(1)
        ed.addLayout(row2)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        ed.addWidget(self.status)

        splitter.addWidget(editor)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        outer.addWidget(splitter, 1)

        QTimer.singleShot(0, self._reload)

    def _reload(self) -> None:
        keep = self._current
        with dbmod.connection() as conn:
            conn.autocommit = True
            self._albums = albums_repo.list_albums(
                conn, self.search.text(), include_deleted=self.show_deleted.isChecked(),
            )
        self.list.clear()
        for a in self._albums:
            label = f"{a.name}  · {a.photo_count} photo(s)"
            if a.is_deleted:
                label += "  · deleted"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, a.id)
            self.list.addItem(item)
        for i in range(self.list.count()):
            if int(self.list.item(i).data(Qt.UserRole)) == keep:
                self.list.setCurrentRow(i)
                return
        if self.list.count():
            self.list.setCurrentRow(0)
        else:
            self._current = None
            self._paint(None)

    def _album(self, album_id: int | None) -> albums_repo.AlbumRow | None:
        return next((a for a in self._albums if a.id == album_id), None)

    def _on_selected(self, current, _prev) -> None:
        self._current = int(current.data(Qt.UserRole)) if current else None
        self._paint(self._album(self._current))

    def _paint(self, album: albums_repo.AlbumRow | None) -> None:
        self.f_name.setText(album.name if album else "")
        self.f_description.setPlainText((album.description if album else "") or "")
        self.delete_btn.setText(
            "Restore album" if (album and album.is_deleted) else "Delete album"
        )
        for w in (self.save_btn, self.delete_btn, self.save_order_btn, self.remove_photo_btn):
            w.setEnabled(album is not None)
        self.photos.clear()
        if album is None:
            return
        with dbmod.connection() as conn:
            conn.autocommit = True
            rows = albums_repo.album_photos(conn, album.id)
        for r in rows:
            bits = [f"#{r.photo_id}", r.source_filename or "(no filename)"]
            if r.capture_date:
                bits.append(str(r.capture_date))
            if r.is_private:
                bits.append("private")
            item = QListWidgetItem("  ·  ".join(bits))
            item.setData(Qt.UserRole, r.photo_id)
            self.photos.addItem(item)

    def _save(self) -> None:
        if self._current is None:
            return
        with dbmod.connection() as conn:
            conn.autocommit = False
            try:
                changed = albums_repo.update_album(
                    conn, self._current,
                    name=self.f_name.text(),
                    description=self.f_description.toPlainText(),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        self.status.setText(
            "Saved — reaches the site on the next push." if changed else "No change."
        )
        self._reload()

    def _toggle_deleted(self) -> None:
        album = self._album(self._current)
        if album is None:
            return
        target = not album.is_deleted
        if target:
            confirm = QMessageBox.question(
                self, "Delete album",
                f"Soft-delete “{album.name}”?\n\nThe photos stay; the album is "
                "hidden and restorable from here.",
            )
            if confirm != QMessageBox.Yes:
                return
        with dbmod.connection() as conn:
            conn.autocommit = False
            try:
                albums_repo.set_album_deleted(conn, album.id, target)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        self.status.setText("Album deleted." if target else "Album restored.")
        self._reload()

    def _remove_photos(self) -> None:
        if self._current is None:
            return
        ids = [int(i.data(Qt.UserRole)) for i in self.photos.selectedItems()]
        if not ids:
            self.status.setText("Select one or more photos first.")
            return
        with dbmod.connection() as conn:
            conn.autocommit = False
            try:
                n = sum(
                    1 for pid in ids
                    if albums_repo.set_photo_in_album(conn, self._current, pid, False)
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        self.status.setText(
            f"Removed {n} photo(s) from the album — soft-deleted, so the removal "
            "reaches the site on the next push."
        )
        self._reload()

    def _save_order(self) -> None:
        if self._current is None:
            return
        order = [
            int(self.photos.item(i).data(Qt.UserRole))
            for i in range(self.photos.count())
        ]
        with dbmod.connection() as conn:
            conn.autocommit = False
            try:
                moved = albums_repo.reorder_album(conn, self._current, order)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        self.status.setText(f"Repositioned {moved} photo(s)." if moved else "Order unchanged.")


# ----------------------------------------------------------------------
# Places
# ----------------------------------------------------------------------

class PlacesTab(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._places: list[places_repo.PlaceRow] = []
        self._current: int | None = None

        outer = QVBoxLayout(self)
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search places…")
        self.search.textChanged.connect(lambda _t: self._reload())
        top.addWidget(self.search, 1)
        outer.addLayout(top)

        splitter = QSplitter(Qt.Horizontal)
        self.list = QListWidget()
        self.list.currentItemChanged.connect(self._on_selected)
        splitter.addWidget(self.list)

        editor = QWidget()
        ed = QVBoxLayout(editor)
        form = QFormLayout()
        self.f_name = QLineEdit()
        self.f_lat = QDoubleSpinBox()
        self.f_lat.setRange(-90.0, 90.0)
        self.f_lat.setDecimals(6)
        self.f_lat.setSpecialValueText("—")
        self.f_lon = QDoubleSpinBox()
        self.f_lon.setRange(-180.0, 180.0)
        self.f_lon.setDecimals(6)
        self.f_lon.setSpecialValueText("—")
        self.f_notes = QTextEdit()
        self.f_notes.setFixedHeight(60)
        form.addRow("Name", self.f_name)
        form.addRow("Latitude", self.f_lat)
        form.addRow("Longitude", self.f_lon)
        form.addRow("Notes", self.f_notes)
        ed.addLayout(form)

        row1 = QHBoxLayout()
        self.save_btn = QPushButton("Save")
        self.save_btn.clicked.connect(self._save)
        row1.addWidget(self.save_btn)
        row1.addStretch(1)
        ed.addLayout(row1)

        ed.addWidget(QLabel("Aliases (other names the search should match)"))
        self.aliases = QListWidget()
        self.aliases.setFixedHeight(110)
        ed.addWidget(self.aliases)
        row2 = QHBoxLayout()
        add_btn = QPushButton("Add alias…")
        add_btn.clicked.connect(self._add_alias)
        rm_btn = QPushButton("Remove alias")
        rm_btn.clicked.connect(self._remove_alias)
        row2.addWidget(add_btn)
        row2.addWidget(rm_btn)
        row2.addStretch(1)
        ed.addLayout(row2)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        ed.addWidget(self.status)
        ed.addStretch(1)

        splitter.addWidget(editor)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        outer.addWidget(splitter, 1)

        QTimer.singleShot(0, self._reload)

    def _reload(self) -> None:
        keep = self._current
        with dbmod.connection() as conn:
            conn.autocommit = True
            self._places = places_repo.list_places(conn, self.search.text())
        self.list.clear()
        for p in self._places:
            item = QListWidgetItem(f"{p.name}  · {p.photo_count} photo(s)")
            item.setData(Qt.UserRole, p.id)
            self.list.addItem(item)
        for i in range(self.list.count()):
            if int(self.list.item(i).data(Qt.UserRole)) == keep:
                self.list.setCurrentRow(i)
                return
        if self.list.count():
            self.list.setCurrentRow(0)
        else:
            self._current = None
            self._paint(None)

    def _place(self, place_id: int | None) -> places_repo.PlaceRow | None:
        return next((p for p in self._places if p.id == place_id), None)

    def _on_selected(self, current, _prev) -> None:
        self._current = int(current.data(Qt.UserRole)) if current else None
        self._paint(self._place(self._current))

    def _paint(self, place: places_repo.PlaceRow | None) -> None:
        self.f_name.setText(place.name if place else "")
        self.f_lat.setValue(place.latitude if (place and place.latitude is not None) else -90.0)
        self.f_lon.setValue(place.longitude if (place and place.longitude is not None) else -180.0)
        self.f_notes.setPlainText((place.notes if place else "") or "")
        self.save_btn.setEnabled(place is not None)
        self.aliases.clear()
        if place is None:
            return
        with dbmod.connection() as conn:
            conn.autocommit = True
            for alias, kind in places_repo.list_aliases(conn, place.id):
                item = QListWidgetItem(f"{alias}  ({kind})")
                item.setData(Qt.UserRole, alias)
                self.aliases.addItem(item)

    def _coords(self) -> tuple[float | None, float | None]:
        lat = None if self.f_lat.value() <= -90.0 else self.f_lat.value()
        lon = None if self.f_lon.value() <= -180.0 else self.f_lon.value()
        return lat, lon

    def _save(self) -> None:
        if self._current is None:
            return
        name = self.f_name.text().strip()
        lat, lon = self._coords()
        with dbmod.connection() as conn:
            conn.autocommit = False
            try:
                holder = places_repo.name_holder(conn, name, excluding=self._current)
                if holder is not None:
                    conn.rollback()
                    self.status.setText(
                        f"Place #{holder} is already called “{name}”. "
                        "Rename that one first, or merge them."
                    )
                    return
                changed = places_repo.update_place(
                    conn, self._current, name=name,
                    notes=self.f_notes.toPlainText(),
                    latitude=lat, longitude=lon, set_coords=True,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        self.status.setText(
            "Saved — reaches the site on the next push." if changed else "No change."
        )
        self._reload()

    def _add_alias(self) -> None:
        if self._current is None:
            return
        alias, ok = QInputDialog.getText(self, "Add alias", "Alias:")
        if not ok or not alias.strip():
            return
        with dbmod.connection() as conn:
            conn.autocommit = False
            try:
                added = places_repo.add_alias(conn, self._current, alias)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        self.status.setText("Alias added." if added else "That alias is already there.")
        self._paint(self._place(self._current))

    def _remove_alias(self) -> None:
        item = self.aliases.currentItem()
        if self._current is None or item is None:
            return
        alias = item.data(Qt.UserRole)
        with dbmod.connection() as conn:
            conn.autocommit = False
            try:
                places_repo.remove_alias(conn, self._current, alias)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        self.status.setText(f"Removed alias “{alias}”.")
        self._paint(self._place(self._current))
