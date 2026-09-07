"""Faces mode — cluster view first, keyboard-driven.

Cluster loop: unlabelled+non-deleted faces with embeddings, run
in-memory agglomerative clustering on cosine distance (threshold
FACE_CLUSTER_DIST). Show clusters largest first. For each cluster:
  - Grid of face crop thumbnails from THUMBS_DIR/faces/{face_id}.jpg.
  - Suggested match: nearest labelled-person mean embedding (excluding
    is_disputed=true faces), with cosine distance.
  - Keys: Enter accept, N new person, X toggle selection under the
    cursor, S split selected into a brand-new cluster shown next, K
    skip, Delete "not a face" on selected (soft delete + audit).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QIcon, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ... import db as dbmod
from ...config import load as load_settings
from ...workers import BackgroundJob
from .clustering import ClusteringResult, cluster_faces, nearest_person
from .merge import merge_people
from .person_dialog import MergePeopleDialog, PersonDialog
from . import repo

log = logging.getLogger(__name__)


THUMB_TILE_PX = 128


class FacesPanel(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._settings = load_settings()
        self._face_index: dict[int, repo.FaceRow] = {}
        self._people_by_id: dict[int, repo.PersonRow] = {}
        self._people_means: dict[int, np.ndarray] = {}
        self._cluster_result: ClusteringResult | None = None
        self._cluster_queue: list[list[int]] = []
        self._cluster_index: int = -1

        outer = QVBoxLayout(self)

        toolbar = QHBoxLayout()
        self.status_label = QLabel("Load faces to begin.")
        self.status_label.setStyleSheet("font-weight: bold")
        toolbar.addWidget(self.status_label)
        toolbar.addStretch(1)

        self.recompute_btn = QPushButton("Recompute clusters")
        self.recompute_btn.clicked.connect(self._recompute_clicked)
        toolbar.addWidget(self.recompute_btn)

        self.people_btn = QPushButton("People…")
        self.people_btn.clicked.connect(self._open_people_dialog)
        toolbar.addWidget(self.people_btn)

        self.merge_btn = QPushButton("Merge…")
        self.merge_btn.clicked.connect(self._open_merge_dialog)
        toolbar.addWidget(self.merge_btn)

        outer.addLayout(toolbar)

        splitter = QSplitter(Qt.Horizontal)

        self.grid = QListWidget()
        self.grid.setViewMode(QListWidget.IconMode)
        self.grid.setIconSize(_qsize(THUMB_TILE_PX, THUMB_TILE_PX))
        self.grid.setResizeMode(QListWidget.Adjust)
        self.grid.setMovement(QListWidget.Static)
        self.grid.setSelectionMode(QListWidget.ExtendedSelection)
        self.grid.setUniformItemSizes(True)
        self.grid.setSpacing(6)
        self.grid.setContextMenuPolicy(Qt.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(self._show_grid_context)
        splitter.addWidget(self.grid)

        # Right side: suggestion + actions.
        side = QWidget()
        side_layout = QVBoxLayout(side)
        self.cluster_label = QLabel("No cluster")
        self.cluster_label.setStyleSheet("font-size: 12pt; font-weight: bold")
        side_layout.addWidget(self.cluster_label)

        self.suggest_label = QLabel("")
        self.suggest_label.setWordWrap(True)
        side_layout.addWidget(self.suggest_label)

        self.accept_btn = QPushButton("Accept suggestion (Enter)")
        self.accept_btn.clicked.connect(self._accept_suggestion)
        side_layout.addWidget(self.accept_btn)

        self.assign_btn = QPushButton("Assign to existing person…")
        self.assign_btn.clicked.connect(self._assign_existing)
        side_layout.addWidget(self.assign_btn)

        self.new_btn = QPushButton("New person (N)")
        self.new_btn.clicked.connect(self._assign_new)
        side_layout.addWidget(self.new_btn)

        self.split_btn = QPushButton("Split selected (S)")
        self.split_btn.clicked.connect(self._split_selected)
        side_layout.addWidget(self.split_btn)

        self.skip_btn = QPushButton("Skip (K)")
        self.skip_btn.clicked.connect(self._skip_cluster)
        side_layout.addWidget(self.skip_btn)

        self.delete_btn = QPushButton("Not a face (Del)")
        self.delete_btn.clicked.connect(self._delete_selected)
        side_layout.addWidget(self.delete_btn)

        side_layout.addStretch(1)
        splitter.addWidget(side)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)

        outer.addWidget(splitter, 1)

        # Keyboard shortcuts
        QShortcut(QKeySequence(Qt.Key_Return), self, self._accept_suggestion)
        QShortcut(QKeySequence(Qt.Key_Enter), self, self._accept_suggestion)
        QShortcut(QKeySequence(Qt.Key_N), self, self._assign_new)
        QShortcut(QKeySequence(Qt.Key_K), self, self._skip_cluster)
        QShortcut(QKeySequence(Qt.Key_S), self, self._split_selected)
        QShortcut(QKeySequence(Qt.Key_Delete), self, self._delete_selected)
        # X toggles selection under the cursor
        QShortcut(QKeySequence(Qt.Key_X), self, self._toggle_current)

    # --- clustering -------------------------------------------------------

    def _recompute_clicked(self) -> None:
        self.status_label.setText("Loading faces and clustering…")
        self.recompute_btn.setEnabled(False)

        def _target(progress_cb, cancel_token):
            with dbmod.connection() as conn:
                conn.autocommit = True
                faces = repo.unlabelled_faces_with_embeddings(conn)
                people = repo.list_people(conn)
                people_means = repo.person_reference_means(conn)
            if not faces:
                return {"faces": [], "people": people, "means": people_means, "result": None}
            face_ids = [f.id for f in faces]
            embeddings = np.array([f.embedding for f in faces], dtype=np.float32)
            settings = load_settings()
            result = cluster_faces(
                face_ids, embeddings,
                max_cosine_distance=settings.FACE_CLUSTER_DIST,
            )
            return {"faces": faces, "people": people, "means": people_means, "result": result}

        job = BackgroundJob(_target)
        job.signals.finished.connect(self._on_cluster_finished)
        job.signals.failed.connect(self._on_cluster_failed)
        self._cluster_worker = job
        job.start()

    def _on_cluster_finished(self, s: dict[str, Any]) -> None:
        self.recompute_btn.setEnabled(True)
        faces: list[repo.FaceRow] = s.get("faces", [])
        self._face_index = {f.id: f for f in faces}
        self._people_by_id = {p.id: p for p in s.get("people", [])}
        self._people_means = s.get("means", {})
        result: ClusteringResult | None = s.get("result")
        if result is None or not result.clusters:
            self.status_label.setText("No unlabelled faces to cluster.")
            self.grid.clear()
            self._cluster_queue = []
            self._cluster_index = -1
            self._paint_side(None)
            return
        self._cluster_result = result
        self._cluster_queue = list(result.clusters)
        self._cluster_index = 0
        hist = result.size_histogram()
        self.status_label.setText(
            f"{len(result.clusters)} clusters over {result.total_faces} faces "
            f"(threshold {result.threshold:.2f}). Sizes: {hist}"
        )
        self._show_current_cluster()

    def _on_cluster_failed(self, tb: str) -> None:
        self.recompute_btn.setEnabled(True)
        log.error("faces: cluster failed:\n%s", tb)
        self.status_label.setText("Clustering failed — see log dock.")

    def _show_current_cluster(self) -> None:
        self.grid.clear()
        if not self._cluster_queue or self._cluster_index < 0:
            self._paint_side(None)
            return
        if self._cluster_index >= len(self._cluster_queue):
            self.cluster_label.setText("All clusters processed.")
            self._paint_side(None)
            return
        cluster = self._cluster_queue[self._cluster_index]
        thumbs_dir = self._settings.THUMBS_DIR / "faces"
        for face_id in cluster:
            face = self._face_index.get(face_id)
            item = QListWidgetItem(str(face_id))
            item.setData(Qt.UserRole, face_id)
            pix = _load_pixmap(thumbs_dir / f"{face_id}.jpg", THUMB_TILE_PX)
            if pix is not None:
                item.setIcon(QIcon(pix))
            item.setToolTip(f"face {face_id}\nphoto {face.photo_id if face else '?'}")
            self.grid.addItem(item)
        self.cluster_label.setText(
            f"Cluster {self._cluster_index + 1} of {len(self._cluster_queue)} "
            f"— {len(cluster)} face{'s' if len(cluster) != 1 else ''}"
        )
        self._paint_side(cluster)

    def _paint_side(self, cluster: list[int] | None) -> None:
        if not cluster:
            self.suggest_label.setText("—")
            self.accept_btn.setEnabled(False)
            self.accept_btn.setProperty("suggested_person_id", None)
            return
        # Mean embedding for this cluster, then nearest labelled person.
        embs = []
        for fid in cluster:
            face = self._face_index.get(fid)
            if face and face.embedding is not None:
                embs.append(np.asarray(face.embedding, dtype=np.float32))
        if not embs or not self._people_means:
            self.suggest_label.setText("No labelled people yet — press N to name this cluster.")
            self.accept_btn.setEnabled(False)
            self.accept_btn.setProperty("suggested_person_id", None)
            return
        mean = np.mean(np.stack(embs), axis=0)
        pid, dist = nearest_person(mean, self._people_means)
        if pid is None:
            self.suggest_label.setText("No suggestion.")
            self.accept_btn.setEnabled(False)
            self.accept_btn.setProperty("suggested_person_id", None)
            return
        person = self._people_by_id.get(pid)
        pname = person.display_name if person else f"person {pid}"
        self.suggest_label.setText(f"Suggested: <b>{pname}</b>\ncosine distance: {dist:.3f}")
        self.accept_btn.setEnabled(True)
        self.accept_btn.setProperty("suggested_person_id", pid)

    # --- actions ----------------------------------------------------------

    def _current_cluster(self) -> list[int] | None:
        if not self._cluster_queue or not (0 <= self._cluster_index < len(self._cluster_queue)):
            return None
        return self._cluster_queue[self._cluster_index]

    def _selected_face_ids(self) -> list[int]:
        return [int(i.data(Qt.UserRole)) for i in self.grid.selectedItems()]

    def _accept_suggestion(self) -> None:
        cluster = self._current_cluster()
        if cluster is None:
            return
        pid = self.accept_btn.property("suggested_person_id")
        if pid is None:
            QMessageBox.information(self, "Faces", "No suggestion to accept.")
            return
        self._assign_cluster(cluster, int(pid), source="human", reason="accepted_suggestion")

    def _assign_existing(self) -> None:
        cluster = self._current_cluster()
        if cluster is None:
            return
        pid = self._pick_person()
        if pid is None:
            return
        self._assign_cluster(cluster, pid, source="human", reason="assign_from_cluster")

    def _pick_person(self) -> int | None:
        with dbmod.connection() as conn:
            conn.autocommit = True
            people = repo.list_people(conn)
        if not people:
            QMessageBox.information(self, "Faces", "No people yet. Press N to create one.")
            return None
        dlg = QDialog(self)
        dlg.setWindowTitle("Assign to person")
        layout = QVBoxLayout(dlg)
        combo = QComboBox()
        combo.setEditable(True)
        for p in people:
            combo.addItem(p.display_name or f"person {p.id}", p.id)
        layout.addWidget(combo)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        layout.addWidget(btns)
        if dlg.exec() != QDialog.Accepted:
            return None
        return int(combo.currentData()) if combo.currentData() is not None else None

    def _assign_new(self) -> None:
        cluster = self._current_cluster()
        if cluster is None:
            return
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
        self._assign_cluster(cluster, pid, source="human", reason="new_person_from_cluster")

    def _assign_cluster(self, cluster: list[int], person_id: int, *, source: str, reason: str) -> None:
        with dbmod.connection() as conn:
            conn.autocommit = False
            try:
                for fid in cluster:
                    repo.assign_face(conn, face_id=fid, person_id=person_id,
                                     source=source, audit_reason=reason)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        for fid in cluster:
            self._face_index.pop(fid, None)
        self._advance_cluster()

    def _split_selected(self) -> None:
        cluster = self._current_cluster()
        if cluster is None:
            return
        picked = set(self._selected_face_ids())
        if not picked or picked == set(cluster):
            QMessageBox.information(self, "Split",
                                    "Select at least one face and leave at least one behind.")
            return
        rest = [fid for fid in cluster if fid not in picked]
        new_cluster = [fid for fid in cluster if fid in picked]
        # Replace current cluster with the "rest" and insert the new cluster
        # immediately after so George sees it next.
        self._cluster_queue[self._cluster_index] = rest
        self._cluster_queue.insert(self._cluster_index + 1, new_cluster)
        self._show_current_cluster()

    def _skip_cluster(self) -> None:
        self._advance_cluster()

    def _delete_selected(self) -> None:
        picked = self._selected_face_ids()
        if not picked:
            return
        confirm = QMessageBox.question(
            self, "Not a face",
            f"Soft-delete {len(picked)} face(s)? Audit row will record the change.",
        )
        if confirm != QMessageBox.Yes:
            return
        with dbmod.connection() as conn:
            conn.autocommit = False
            try:
                for fid in picked:
                    repo.soft_delete_face(conn, face_id=fid, reason="not_a_face")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        # Remove from current cluster in-place.
        cluster = self._current_cluster()
        if cluster is not None:
            remaining = [fid for fid in cluster if fid not in set(picked)]
            self._cluster_queue[self._cluster_index] = remaining
            if not remaining:
                self._advance_cluster()
                return
        for fid in picked:
            self._face_index.pop(fid, None)
        self._show_current_cluster()

    def _toggle_current(self) -> None:
        item = self.grid.currentItem()
        if item is None:
            return
        item.setSelected(not item.isSelected())

    def _advance_cluster(self) -> None:
        self._cluster_index += 1
        if self._cluster_index >= len(self._cluster_queue):
            self.status_label.setText("Done with this pass. Recompute for more.")
            self.grid.clear()
            self.cluster_label.setText("No more clusters.")
            self._paint_side(None)
            return
        self._show_current_cluster()

    # --- context menu / people mgmt --------------------------------------

    def _show_grid_context(self, pos) -> None:
        item = self.grid.itemAt(pos)
        if item is None:
            return
        fid = int(item.data(Qt.UserRole))
        menu = QMenu(self)
        toggle = QAction("Toggle select (X)", menu)
        toggle.triggered.connect(self._toggle_current)
        menu.addAction(toggle)
        delete = QAction("Not a face (Del)", menu)
        delete.triggered.connect(self._delete_selected)
        menu.addAction(delete)
        menu.exec(self.grid.mapToGlobal(pos))

    def _open_people_dialog(self) -> None:
        with dbmod.connection() as conn:
            conn.autocommit = True
            people = repo.list_people(conn)
        dlg = _PeopleListDialog(people, self)
        dlg.exec()

    def _open_merge_dialog(self) -> None:
        with dbmod.connection() as conn:
            conn.autocommit = True
            people = repo.list_people(conn)
        if len(people) < 2:
            QMessageBox.information(self, "Merge", "Need at least two people to merge.")
            return
        dlg = MergePeopleDialog(people, parent=self)
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
            f"Merged. Moved {out['faces_moved']} face(s) and {out['variants_moved']} name variant(s).",
        )


class _PeopleListDialog(QDialog):
    def __init__(self, people: list[repo.PersonRow], parent) -> None:
        super().__init__(parent)
        self.setWindowTitle("People")
        layout = QVBoxLayout(self)
        self.list = QListWidget()
        for p in people:
            item = QListWidgetItem(f"{p.display_name}  (id {p.id})")
            item.setData(Qt.UserRole, p.id)
            self.list.addItem(item)
        layout.addWidget(self.list)
        row = QHBoxLayout()
        new_btn = QPushButton("New…")
        edit_btn = QPushButton("Edit…")
        close_btn = QPushButton("Close")
        row.addWidget(new_btn)
        row.addWidget(edit_btn)
        row.addStretch(1)
        row.addWidget(close_btn)
        layout.addLayout(row)
        new_btn.clicked.connect(self._new)
        edit_btn.clicked.connect(self._edit)
        close_btn.clicked.connect(self.accept)

    def _new(self) -> None:
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
        with dbmod.connection() as conn:
            conn.autocommit = True
            person = repo.get_person(conn, pid)
        if person:
            item = QListWidgetItem(f"{person.display_name}  (id {person.id})")
            item.setData(Qt.UserRole, person.id)
            self.list.addItem(item)

    def _edit(self) -> None:
        item = self.list.currentItem()
        if item is None:
            return
        pid = int(item.data(Qt.UserRole))
        with dbmod.connection() as conn:
            conn.autocommit = True
            person = repo.get_person(conn, pid)
        if person is None:
            return
        dlg = PersonDialog(existing=person, parent=self)
        if dlg.exec() != QDialog.Accepted:
            return
        with dbmod.connection() as conn:
            conn.autocommit = False
            try:
                repo.update_person(conn, pid, **dlg.values())
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        with dbmod.connection() as conn:
            conn.autocommit = True
            refreshed = repo.get_person(conn, pid)
        if refreshed:
            item.setText(f"{refreshed.display_name}  (id {refreshed.id})")


# --- helpers ----------------------------------------------------------


def _qsize(w: int, h: int):
    from PySide6.QtCore import QSize
    return QSize(w, h)


def _load_pixmap(path: Path, edge_px: int) -> QPixmap | None:
    if not path.exists():
        return None
    pm = QPixmap(str(path))
    if pm.isNull():
        return None
    return pm.scaled(edge_px, edge_px, Qt.KeepAspectRatio, Qt.SmoothTransformation)
