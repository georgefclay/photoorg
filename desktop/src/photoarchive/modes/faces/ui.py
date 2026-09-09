"""Faces mode — cluster view first, keyboard-driven.

Cluster loop (post fix-up 2): unlabelled+non-deleted faces with
embeddings, filter out low quality (det_score < FACE_MIN_SCORE OR bbox
short-edge < FACE_MIN_PX), then average-linkage cosine clustering with
recursive split above FACE_MAX_CLUSTER. Show clusters big-first, small
(< 3) at the back. For each cluster:
  - Grid of face crop thumbnails from THUMBS_DIR/faces/{face_id}.jpg,
    ordered closest-to-centroid first (outliers land at the tail so
    Shift-range-select picks up the "other person").
  - Year context under each crop (capture-date year if known).
  - Suggested match: nearest labelled person by mean embedding
    (excluding is_disputed=true and low-quality faces).
  - Keys: Enter accept, N new person, X toggle selection under the
    cursor, S split selected into a brand-new cluster shown next, K
    skip, Delete "not a face" on selected (soft delete + audit),
    B split by nearest of two labelled people (mixed-sibling clusters).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np
from PySide6.QtCore import QElapsedTimer, QEvent, Qt, Signal
from PySide6.QtGui import QAction, QIcon, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
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
from .clustering import (
    ClusterMeta,
    ClusteringResult,
    annotate_clusters_with_likely_person,
    cluster_faces,
    diagnose_faces,
    mean_pairwise_cosine,
    nearest_person,
    nearest_person_by_prototype,
    nearest_two_people,
    order_cluster_by_centroid_distance,
    topk_persons_by_prototype,
)
from .merge import merge_people
from .person_dialog import MergePeopleDialog, PersonDialog
from .photo_preview import PEEK_HOLD_MS, PhotoPreviewWidget
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
        # Fix-up 3: per-person K-means prototypes (up to 5). Suggestions
        # use nearest-prototype distance instead of a single mean so one
        # person's lifetime doesn't split across age bands.
        self._people_prototypes: dict[int, list[np.ndarray]] = {}
        self._cluster_result: ClusteringResult | None = None
        self._cluster_queue: list[ClusterMeta] = []
        self._cluster_index: int = -1
        self._low_quality_faces: list[repo.FaceRow] = []

        outer = QVBoxLayout(self)

        toolbar = QHBoxLayout()
        self.status_label = QLabel("Load faces to begin.")
        self.status_label.setStyleSheet("font-weight: bold")
        toolbar.addWidget(self.status_label)
        toolbar.addStretch(1)

        self.include_low_q_check = QCheckBox("Include low-quality")
        self.include_low_q_check.setToolTip(
            "By default faces below FACE_MIN_SCORE or FACE_MIN_PX are excluded "
            "from clustering. Tick to include them on the next Recompute."
        )
        toolbar.addWidget(self.include_low_q_check)

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
        self._splitter = splitter

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

        self.split_by_nearest_btn = QPushButton("Split by nearest person (B)")
        self.split_by_nearest_btn.setToolTip(
            "Mixed-sibling clusters: for each face, assign to whichever of "
            "the two nearest labelled people (by cluster centroid) it is "
            "closer to. Preview + confirm before it commits."
        )
        self.split_by_nearest_btn.clicked.connect(self._split_by_nearest_person)
        side_layout.addWidget(self.split_by_nearest_btn)

        self.skip_btn = QPushButton("Skip (K)")
        self.skip_btn.clicked.connect(self._skip_cluster)
        side_layout.addWidget(self.skip_btn)

        self.delete_btn = QPushButton("Not a face (Del)")
        self.delete_btn.clicked.connect(self._delete_selected)
        side_layout.addWidget(self.delete_btn)

        side_layout.addStretch(1)
        splitter.addWidget(side)

        # Fix-up 5: full-photo preview pane. Hidden by default; Space or
        # double-click on a face tile opens it, arrow keys step through
        # the cluster, Esc closes.
        self.preview = PhotoPreviewWidget()
        self.preview.face_clicked.connect(self._on_preview_face_clicked)
        self.preview.setVisible(False)
        splitter.addWidget(self.preview)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 4)
        splitter.setSizes([600, 220, 0])  # preview collapsed to start

        outer.addWidget(splitter, 1)

        self.grid.itemDoubleClicked.connect(
            lambda item: self._open_preview_for_face(int(item.data(Qt.UserRole)))
        )
        self._peek_timer = QElapsedTimer()
        self._space_press_active = False
        self.grid.installEventFilter(self)

        # Keyboard shortcuts
        QShortcut(QKeySequence(Qt.Key_Return), self, self._accept_suggestion)
        QShortcut(QKeySequence(Qt.Key_Enter), self, self._accept_suggestion)
        QShortcut(QKeySequence(Qt.Key_N), self, self._assign_new)
        QShortcut(QKeySequence(Qt.Key_K), self, self._skip_cluster)
        QShortcut(QKeySequence(Qt.Key_S), self, self._split_selected)
        QShortcut(QKeySequence(Qt.Key_B), self, self._split_by_nearest_person)
        QShortcut(QKeySequence(Qt.Key_Delete), self, self._delete_selected)
        # X toggles selection under the cursor
        QShortcut(QKeySequence(Qt.Key_X), self, self._toggle_current)
        # Fix-up 5: Esc closes the preview even when it's the focused widget
        QShortcut(QKeySequence(Qt.Key_Escape), self, self._close_preview)

    # --- clustering -------------------------------------------------------

    def _recompute_clicked(self) -> None:
        self.status_label.setText("Loading faces and clustering…")
        self.recompute_btn.setEnabled(False)
        include_low_q = self.include_low_q_check.isChecked()

        def _target(progress_cb, cancel_token):
            settings = load_settings()
            with dbmod.connection() as conn:
                conn.autocommit = True
                faces = repo.unlabelled_faces_with_embeddings(conn)
                people = repo.list_people(conn)
                # Reference set also honours the quality gate — a wrong
                # tag on a blurry crop must not poison future matches.
                people_means = repo.person_reference_means(
                    conn,
                    min_score=settings.FACE_MIN_SCORE,
                    min_short_edge_px=settings.FACE_MIN_PX,
                )
                # Fix-up 3: multi-prototype references. Same quality
                # gate. Only used if a person has enough faces to
                # partition; otherwise falls back to their mean.
                people_prototypes = repo.person_prototypes(
                    conn,
                    min_score=settings.FACE_MIN_SCORE,
                    min_short_edge_px=settings.FACE_MIN_PX,
                )

            det_scores = [f.confidence for f in faces]
            short_edges = [f.short_edge_px for f in faces]
            all_ids = [f.id for f in faces]
            all_embs = (
                np.array([f.embedding for f in faces], dtype=np.float32)
                if faces else np.zeros((0, 0), dtype=np.float32)
            )
            diagnostics = diagnose_faces(all_ids, all_embs, det_scores, short_edges)

            if include_low_q:
                keepers = faces
                low_q: list[repo.FaceRow] = []
            else:
                keepers = []
                low_q = []
                for f in faces:
                    score_ok = (f.confidence or 0.0) >= settings.FACE_MIN_SCORE
                    se = f.short_edge_px
                    size_ok = se is None or se >= settings.FACE_MIN_PX
                    if score_ok and size_ok:
                        keepers.append(f)
                    else:
                        low_q.append(f)
            diagnostics["kept_for_clustering"] = len(keepers)
            diagnostics["low_quality_excluded"] = len(low_q)

            if not keepers:
                return {
                    "faces": faces, "people": people, "means": people_means,
                    "prototypes": people_prototypes,
                    "result": None, "diagnostics": diagnostics,
                    "low_quality": low_q,
                }
            face_ids = [f.id for f in keepers]
            embeddings = np.array([f.embedding for f in keepers], dtype=np.float32)
            result = cluster_faces(
                face_ids, embeddings,
                max_cosine_distance=settings.FACE_CLUSTER_DIST,
                max_cluster=settings.FACE_MAX_CLUSTER,
            )
            # Fix-up 3 item 3: tag clusters that already look like a
            # labelled person so George can Enter through them.
            emb_by_id = {fid: emb for fid, emb in zip(face_ids, embeddings)}
            annotate_clusters_with_likely_person(
                result.clusters,
                emb_by_id,
                people_prototypes,
                match_threshold=settings.FACE_CLUSTER_DIST,
            )

            # Diagnose the biggest cluster: mean pairwise cosine + score
            # / size stats. Numbers are what we present to George when a
            # cluster looks huge.
            if result.clusters:
                biggest = result.clusters[0]
                if len(biggest.face_ids) > 50:
                    big_embs = np.array(
                        [dict((f.id, f.embedding) for f in keepers)[fid]
                         for fid in biggest.face_ids],
                        dtype=np.float32,
                    )
                    diagnostics["biggest_cluster_size"] = len(biggest.face_ids)
                    diagnostics["biggest_cluster_mean_cosine"] = round(
                        mean_pairwise_cosine(big_embs), 4
                    )
            return {
                "faces": faces, "people": people, "means": people_means,
                "prototypes": people_prototypes,
                "result": result, "diagnostics": diagnostics,
                "low_quality": low_q,
            }

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
        self._people_prototypes = s.get("prototypes", {})
        self._low_quality_faces = s.get("low_quality", [])
        diagnostics = s.get("diagnostics", {})
        log.info("faces: cluster diagnostics = %s", diagnostics)
        result: ClusteringResult | None = s.get("result")
        if result is None or not result.clusters:
            summary = (
                f"No clusterable faces. Diagnostics: total={diagnostics.get('total_faces', 0)}"
                f" · low-quality excluded={diagnostics.get('low_quality_excluded', 0)}"
            )
            self.status_label.setText(summary)
            self.grid.clear()
            self._cluster_queue = []
            self._cluster_index = -1
            self._paint_side(None)
            return
        self._cluster_result = result
        self._cluster_queue = list(result.clusters)
        self._cluster_index = 0
        hist = result.size_histogram()
        low_q_line = (
            f" · low-quality excluded={diagnostics.get('low_quality_excluded', 0)}"
            if diagnostics.get('low_quality_excluded') else ""
        )
        big_line = ""
        if "biggest_cluster_mean_cosine" in diagnostics:
            big_line = (
                f" · biggest={diagnostics['biggest_cluster_size']} "
                f"faces mean-cos {diagnostics['biggest_cluster_mean_cosine']}"
            )
        self.status_label.setText(
            f"{len(result.clusters)} clusters over {result.total_faces} faces "
            f"(threshold {result.threshold:.2f}, avg-linkage). "
            f"Sizes: {hist}{low_q_line}{big_line}"
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
        meta = self._cluster_queue[self._cluster_index]
        cluster = meta.face_ids
        # Fix-up 2 item 7: order closest-to-centroid first so the "other"
        # person in a mixed-sibling cluster collects at the tail — easy
        # to Shift-select and split off.
        emb_map = {
            fid: np.asarray(self._face_index[fid].embedding, dtype=np.float32)
            for fid in cluster
            if fid in self._face_index and self._face_index[fid].embedding is not None
        }
        ordered = order_cluster_by_centroid_distance(cluster, emb_map) if emb_map else list(cluster)
        # Persist the ordered face_ids back so subsequent actions (split,
        # split-by-nearest) work on the same order George is looking at.
        meta.face_ids = ordered

        thumbs_dir = self._settings.THUMBS_DIR / "faces"
        for face_id in ordered:
            face = self._face_index.get(face_id)
            item = QListWidgetItem()
            item.setData(Qt.UserRole, face_id)
            year_label = (
                str(face.photo_capture_year)
                if face and face.photo_capture_year else ""
            )
            item.setText(year_label)
            item.setTextAlignment(Qt.AlignHCenter | Qt.AlignBottom)
            pix = _load_pixmap(thumbs_dir / f"{face_id}.jpg", THUMB_TILE_PX)
            if pix is not None:
                item.setIcon(QIcon(pix))
            tt_bits = [f"face {face_id}"]
            if face:
                tt_bits.append(f"photo {face.photo_id}")
                if face.confidence is not None:
                    tt_bits.append(f"score {face.confidence:.2f}")
                se = face.short_edge_px
                if se is not None:
                    tt_bits.append(f"short edge {int(se)}px")
                if face.photo_capture_year:
                    tt_bits.append(f"year {face.photo_capture_year}")
            item.setToolTip("\n".join(tt_bits))
            self.grid.addItem(item)
        split_note = " · split from a larger cluster" if meta.split_from_larger else ""
        likely_note = ""
        if meta.likely_person_id is not None:
            likely_person = self._people_by_id.get(meta.likely_person_id)
            likely_name = likely_person.display_name if likely_person else f"person {meta.likely_person_id}"
            likely_note = (
                f" · <b>likely {likely_name}</b> ({meta.likely_person_distance:.3f})"
            )
        self.cluster_label.setText(
            f"Cluster {self._cluster_index + 1} of {len(self._cluster_queue)} "
            f"— {len(cluster)} face{'s' if len(cluster) != 1 else ''}"
            f"{split_note}{likely_note}"
        )
        self._paint_side(cluster)

    def _paint_side(self, cluster: list[int] | None) -> None:
        if not cluster:
            self.suggest_label.setText("—")
            self.accept_btn.setEnabled(False)
            self.accept_btn.setProperty("suggested_person_id", None)
            return
        embs = []
        for fid in cluster:
            face = self._face_index.get(fid)
            if face and face.embedding is not None:
                embs.append(np.asarray(face.embedding, dtype=np.float32))
        if not embs or not self._people_prototypes:
            self.suggest_label.setText("No labelled people yet — press N to name this cluster.")
            self.accept_btn.setEnabled(False)
            self.accept_btn.setProperty("suggested_person_id", None)
            return
        centroid = np.mean(np.stack(embs), axis=0)
        # Fix-up 3: nearest-prototype suggestion, plus the next two
        # candidates below it so a near-miss is one click away.
        topk = topk_persons_by_prototype(centroid, self._people_prototypes, k=3)
        if not topk:
            self.suggest_label.setText("No suggestion.")
            self.accept_btn.setEnabled(False)
            self.accept_btn.setProperty("suggested_person_id", None)
            return
        pid, dist = topk[0]
        person = self._people_by_id.get(pid)
        pname = person.display_name if person else f"person {pid}"
        lines = [f"Suggested: <b>{pname}</b> · cosine {dist:.3f}"]
        also = [t for t in topk[1:] if t[0] != pid]
        if also:
            lines.append("Also probably:")
            for other_pid, other_dist in also:
                other = self._people_by_id.get(other_pid)
                oname = other.display_name if other else f"person {other_pid}"
                lines.append(f" · {oname} — {other_dist:.3f}")
        self.suggest_label.setText("<br>".join(lines))
        self.accept_btn.setEnabled(True)
        self.accept_btn.setProperty("suggested_person_id", pid)

    # --- actions ----------------------------------------------------------

    def _current_cluster(self) -> list[int] | None:
        meta = self._current_meta()
        return meta.face_ids if meta is not None else None

    def _current_meta(self) -> ClusterMeta | None:
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
        meta = self._current_meta()
        if meta is None:
            return
        cluster = meta.face_ids
        picked = set(self._selected_face_ids())
        if not picked or picked == set(cluster):
            QMessageBox.information(self, "Split",
                                    "Select at least one face and leave at least one behind.")
            return
        rest = [fid for fid in cluster if fid not in picked]
        new_cluster = [fid for fid in cluster if fid in picked]
        # Replace current cluster with the "rest" and insert the new cluster
        # immediately after so George sees it next.
        meta.face_ids = rest
        self._cluster_queue.insert(
            self._cluster_index + 1,
            ClusterMeta(
                face_ids=new_cluster,
                split_from_larger=True,
                threshold_used=meta.threshold_used,
            ),
        )
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
        meta = self._current_meta()
        if meta is not None:
            remaining = [fid for fid in meta.face_ids if fid not in set(picked)]
            meta.face_ids = remaining
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

    def _split_by_nearest_person(self) -> None:
        """B key. For each face in the current cluster, assign to whichever
        of the two nearest labelled people (measured against the cluster
        centroid) it is closer to. Show a preview, George confirms."""
        meta = self._current_meta()
        if meta is None:
            return
        cluster = meta.face_ids
        if len(cluster) < 2:
            QMessageBox.information(self, "Split by nearest",
                                    "Need at least two faces in the cluster.")
            return
        if len(self._people_prototypes) < 2:
            QMessageBox.information(self, "Split by nearest",
                                    "Need at least two labelled people to split against.")
            return

        emb_map = {
            fid: np.asarray(self._face_index[fid].embedding, dtype=np.float32)
            for fid in cluster
            if fid in self._face_index and self._face_index[fid].embedding is not None
        }
        if not emb_map:
            return
        centroid = np.mean(np.stack(list(emb_map.values())), axis=0)
        # Rank labelled people by nearest-prototype distance to the centroid,
        # then take the two best (fix-up 3 harmony).
        ranked = topk_persons_by_prototype(centroid, self._people_prototypes, k=2)
        if len(ranked) < 2:
            QMessageBox.information(self, "Split by nearest",
                                    "Could not find two nearby people.")
            return
        (pid_a, dist_a), (pid_b, dist_b) = ranked[0], ranked[1]
        protos_a = self._people_prototypes[pid_a]
        protos_b = self._people_prototypes[pid_b]

        def _min_dist(e: np.ndarray, protos: list[np.ndarray]) -> float:
            en = e / max(float(np.linalg.norm(e)), 1e-9)
            best = float("inf")
            for p in protos:
                pn = p / max(float(np.linalg.norm(p)), 1e-9)
                d = 1.0 - float(np.dot(en, pn))
                if d < best:
                    best = d
            return best

        group_a: list[int] = []
        group_b: list[int] = []
        for fid in cluster:
            e = emb_map.get(fid)
            if e is None:
                group_a.append(fid)  # can't decide — default to the closer overall
                continue
            da = _min_dist(e, protos_a)
            db_ = _min_dist(e, protos_b)
            (group_a if da <= db_ else group_b).append(fid)

        name_a = self._people_by_id[pid_a].display_name if pid_a in self._people_by_id else f"person {pid_a}"
        name_b = self._people_by_id[pid_b].display_name if pid_b in self._people_by_id else f"person {pid_b}"
        confirm = QMessageBox.question(
            self, "Split by nearest person",
            f"Assign {len(group_a)} face(s) to {name_a} (distance {dist_a:.3f}) "
            f"and {len(group_b)} face(s) to {name_b} (distance {dist_b:.3f})?\n\n"
            "Both groups will be committed to their respective people.",
        )
        if confirm != QMessageBox.Yes:
            return

        with dbmod.connection() as conn:
            conn.autocommit = False
            try:
                for fid in group_a:
                    repo.assign_face(conn, face_id=fid, person_id=pid_a,
                                     source="human", audit_reason="split_by_nearest_person")
                for fid in group_b:
                    repo.assign_face(conn, face_id=fid, person_id=pid_b,
                                     source="human", audit_reason="split_by_nearest_person")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        for fid in cluster:
            self._face_index.pop(fid, None)
        self._advance_cluster()

    # --- fix-up 5: full-photo preview -------------------------------------

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 — Qt override
        # Space press/release on the grid drives peek-vs-lock behaviour.
        # We do NOT bind Space as a QShortcut because we need press+release
        # timing to distinguish a tap from a hold.
        if obj is self.grid and event.type() == QEvent.KeyPress:
            if event.key() == Qt.Key_Space and not event.isAutoRepeat():
                self._on_space_pressed()
                return True
            if event.key() in (Qt.Key_Right, Qt.Key_Left) and self.preview.isVisible():
                if event.key() == Qt.Key_Right:
                    self._preview_step(+1)
                else:
                    self._preview_step(-1)
                return True
            if event.key() == Qt.Key_Escape and self.preview.isVisible():
                self._close_preview()
                return True
        elif obj is self.grid and event.type() == QEvent.KeyRelease:
            if event.key() == Qt.Key_Space and not event.isAutoRepeat():
                self._on_space_released()
                return True
        return super().eventFilter(obj, event)

    def _on_space_pressed(self) -> None:
        self._peek_timer.restart()
        self._space_press_active = True
        item = self.grid.currentItem() or (
            self.grid.item(0) if self.grid.count() else None
        )
        if item is not None:
            fid = int(item.data(Qt.UserRole))
            self._open_preview_for_face(fid)

    def _on_space_released(self) -> None:
        if not self._space_press_active:
            return
        self._space_press_active = False
        held = self._peek_timer.elapsed()
        # A short tap leaves the preview open (lock). A long hold treats
        # the whole gesture as a peek and closes on release.
        if held >= PEEK_HOLD_MS and self.preview.isVisible():
            self._close_preview()

    def _open_preview_for_face(self, face_id: int) -> None:
        face = self._face_index.get(face_id)
        if face is None:
            # Might be an already-labelled face (Person view). Read fresh.
            with dbmod.connection() as conn:
                conn.autocommit = True
                context = repo.load_photo_context(conn, self._photo_for_face(face_id))
        else:
            with dbmod.connection() as conn:
                conn.autocommit = True
                context = repo.load_photo_context(conn, face.photo_id)
        if context is None:
            self.status_label.setText(f"No photo context for face {face_id}.")
            return
        self.preview.show_photo(context, face_id)
        if not self.preview.isVisible():
            self.preview.setVisible(True)
            sizes = self._splitter.sizes()
            if len(sizes) == 3 and sizes[2] == 0:
                total = sum(sizes) or self.width()
                # Give the preview ~55% of the width.
                new_preview = max(400, int(total * 0.55))
                new_grid = max(200, int(total * 0.3))
                new_side = max(150, total - new_preview - new_grid)
                self._splitter.setSizes([new_grid, new_side, new_preview])
        # Keep grid focused so subsequent keys still route to it.
        self.grid.setFocus()

    def _close_preview(self) -> None:
        if not self.preview.isVisible():
            return
        self.preview.setVisible(False)
        sizes = self._splitter.sizes()
        if len(sizes) == 3:
            self._splitter.setSizes([sizes[0] + sizes[2], sizes[1], 0])

    def _preview_step(self, delta: int) -> None:
        cluster = self._current_cluster()
        if not cluster:
            return
        # Which face is currently shown?
        item = self.grid.currentItem()
        current_fid = int(item.data(Qt.UserRole)) if item is not None else cluster[0]
        try:
            idx = cluster.index(current_fid)
        except ValueError:
            idx = 0
        new_idx = max(0, min(len(cluster) - 1, idx + delta))
        if new_idx == idx:
            return
        new_fid = cluster[new_idx]
        new_item = None
        for i in range(self.grid.count()):
            it = self.grid.item(i)
            if int(it.data(Qt.UserRole)) == new_fid:
                new_item = it
                break
        if new_item is not None:
            self.grid.setCurrentItem(new_item)
        self._open_preview_for_face(new_fid)

    def _on_preview_face_clicked(self, face_id: int, is_labelled: bool) -> None:
        """User clicked another face box inside the preview: jump to its
        cluster if unlabelled, or open the person editor if labelled."""
        if is_labelled:
            # Look up the person and open their editor.
            with dbmod.connection() as conn:
                conn.autocommit = True
                # Find the person via the face.
                row = conn.execute(
                    "select person_id from faces where id = %s", (face_id,)
                ).fetchone()
                if not row or row[0] is None:
                    return
                pid = int(row[0])
                person = repo.get_person(conn, pid)
            if person is None:
                return
            dlg = PersonDialog(existing=person, parent=self)
            if dlg.exec() == QDialog.Accepted:
                with dbmod.connection() as conn:
                    conn.autocommit = False
                    try:
                        repo.update_person(conn, pid, **dlg.values())
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise
            return
        # Unlabelled: jump the grid cursor to that face's cluster.
        for idx, meta in enumerate(self._cluster_queue):
            if face_id in meta.face_ids:
                self._cluster_index = idx
                self._show_current_cluster()
                for i in range(self.grid.count()):
                    it = self.grid.item(i)
                    if int(it.data(Qt.UserRole)) == face_id:
                        self.grid.setCurrentItem(it)
                        break
                self._open_preview_for_face(face_id)
                return
        # Not in any cluster (e.g. low-quality bucket): just show its preview.
        self._open_preview_for_face(face_id)

    def _photo_for_face(self, face_id: int) -> int:
        with dbmod.connection() as conn:
            conn.autocommit = True
            row = conn.execute(
                "select photo_id from faces where id = %s", (face_id,)
            ).fetchone()
        return int(row[0]) if row else 0

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
