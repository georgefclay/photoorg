"""Review grid for pending ingest proposals (backs + rescans).

Keyboard: A=accept, R=reject, arrows=navigate, Z=undo last, Ctrl+A=accept
all remaining with score/1-distance ≥ 0.9."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpacerItem,
    QVBoxLayout,
    QWidget,
)

from ... import db
from ...config import Settings
from . import decisions

log = logging.getLogger(__name__)


@dataclass
class Proposal:
    kind: str  # "pairing" | "rescan"
    id: int
    left_thumb: Path | None
    right_thumb: Path | None
    caption: str
    score_or_distance: float  # normalized 0..1; 1 = strongest


class ReviewGridDialog(QDialog):
    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Review proposals")
        self._settings = settings
        self.resize(1200, 720)

        self._pending: list[Proposal] = _load_pending(settings)
        self._history: list[tuple[str, int, str]] = []  # (kind, id, action)
        self._idx = 0

        self._left = QLabel(alignment=Qt.AlignCenter)
        self._right = QLabel(alignment=Qt.AlignCenter)
        for lbl in (self._left, self._right):
            lbl.setMinimumSize(400, 400)
            lbl.setStyleSheet("background: #222; color: #ccc")
            lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        imgs = QHBoxLayout()
        imgs.addWidget(self._left)
        imgs.addWidget(self._right)

        self._caption = QLabel(alignment=Qt.AlignCenter)
        self._caption.setStyleSheet("font-size: 12pt")

        self._counter = QLabel(alignment=Qt.AlignCenter)
        self._counter.setStyleSheet("color: gray")

        accept = QPushButton("Accept (A)")
        reject = QPushButton("Reject (R)")
        prev = QPushButton("← Prev")
        nxt = QPushButton("Next →")
        undo = QPushButton("Undo (Z)")
        accept_all = QPushButton("Accept all ≥ 0.9 (Ctrl+A)")
        buttons = QHBoxLayout()
        for b in (prev, reject, accept, nxt, undo, accept_all):
            buttons.addWidget(b)
        accept.clicked.connect(self._accept)
        reject.clicked.connect(self._reject)
        prev.clicked.connect(self._prev)
        nxt.clicked.connect(self._next)
        undo.clicked.connect(self._undo)
        accept_all.clicked.connect(self._accept_all_confident)

        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.reject)

        outer = QVBoxLayout(self)
        outer.addWidget(self._counter)
        outer.addLayout(imgs)
        outer.addWidget(self._caption)
        outer.addSpacerItem(QSpacerItem(1, 8))
        outer.addLayout(buttons)
        outer.addWidget(close)

        # Shortcuts
        QShortcut(QKeySequence("A"), self, activated=self._accept)
        QShortcut(QKeySequence("R"), self, activated=self._reject)
        QShortcut(QKeySequence("Right"), self, activated=self._next)
        QShortcut(QKeySequence("Left"), self, activated=self._prev)
        QShortcut(QKeySequence("Z"), self, activated=self._undo)
        QShortcut(QKeySequence("Ctrl+A"), self, activated=self._accept_all_confident)

        self._render()

    # ---- render ----

    def _current(self) -> Proposal | None:
        if not self._pending:
            return None
        self._idx = max(0, min(self._idx, len(self._pending) - 1))
        return self._pending[self._idx]

    def _render(self) -> None:
        cur = self._current()
        if cur is None:
            self._left.setText("(no pending)")
            self._right.setText("")
            self._caption.setText("Nothing to review.")
            self._counter.setText("0 / 0")
            return
        self._counter.setText(f"{self._idx + 1} / {len(self._pending)}")
        self._caption.setText(cur.caption)
        _set_pixmap(self._left, cur.left_thumb)
        _set_pixmap(self._right, cur.right_thumb)

    # ---- actions ----

    def _accept(self) -> None:
        self._apply(accept=True)

    def _reject(self) -> None:
        self._apply(accept=False)

    def _apply(self, *, accept: bool) -> None:
        cur = self._current()
        if cur is None:
            return
        try:
            if cur.kind == "pairing":
                if accept:
                    decisions.accept_pairing(self._settings, cur.id)
                else:
                    decisions.reject_pairing(self._settings, cur.id)
            else:
                if accept:
                    decisions.accept_rescan(self._settings, cur.id)
                else:
                    decisions.reject_rescan(self._settings, cur.id)
        except Exception as e:
            log.exception("decision failed")
            QMessageBox.critical(self, "Decision failed", str(e))
            return
        self._history.append((cur.kind, cur.id, "accept" if accept else "reject"))
        del self._pending[self._idx]
        if self._idx >= len(self._pending):
            self._idx = len(self._pending) - 1
        self._render()

    def _prev(self) -> None:
        if self._idx > 0:
            self._idx -= 1
            self._render()

    def _next(self) -> None:
        if self._idx < len(self._pending) - 1:
            self._idx += 1
            self._render()

    def _undo(self) -> None:
        if not self._history:
            return
        # Undo of Phase 2 = advisory-only reload from DB. Fully reversing an
        # accepted back would require deleting the photo_backs row and
        # restoring the staging files — deliberately out of scope for now.
        QMessageBox.information(
            self, "Undo",
            "Undo isn't reversible from here in Phase 2 — reload the review "
            "to see fresh state. Manually reversing accepted proposals means "
            "editing the DB.",
        )

    def _accept_all_confident(self) -> None:
        threshold = 0.9
        candidates = [
            (i, p) for i, p in enumerate(self._pending)
            if p.score_or_distance >= threshold
        ]
        if not candidates:
            QMessageBox.information(self, "Nothing to auto-accept",
                                    f"No proposals at ≥ {threshold:.2f}.")
            return
        reply = QMessageBox.question(
            self, "Accept all",
            f"Accept {len(candidates)} proposals at score/1-distance ≥ {threshold:.2f}?",
        )
        if reply != QMessageBox.Yes:
            return
        # Iterate high-to-low index to keep indices stable while deleting.
        for i, p in sorted(candidates, key=lambda x: -x[0]):
            try:
                if p.kind == "pairing":
                    decisions.accept_pairing(self._settings, p.id)
                else:
                    decisions.accept_rescan(self._settings, p.id)
                self._history.append((p.kind, p.id, "accept"))
                del self._pending[i]
            except Exception as e:
                log.exception("bulk accept failed for %s %d", p.kind, p.id)
                QMessageBox.critical(self, "Bulk accept aborted", str(e))
                break
        self._idx = 0
        self._render()


def _set_pixmap(label: QLabel, path: Path | None) -> None:
    if path is None or not path.exists():
        label.setText("(no thumb)")
        return
    pm = QPixmap(str(path))
    if pm.isNull():
        label.setText("(unreadable)")
        return
    label.setPixmap(pm.scaled(label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))


def _load_pending(settings: Settings) -> list[Proposal]:
    out: list[Proposal] = []
    with db.connection() as conn:
        # Pairings
        rows = conn.execute(
            """
            select ip.id, ip.staging_thumb_path, ip.back_score,
                   ip.back_source_folder, ip.back_source_filename,
                   ip.back_scan_sequence,
                   p.scan_batch, p.scan_sequence, p.source_folder,
                   p.source_filename,
                   (select thumb.jpg_path from (
                     select
                       ('__thumb__' || lpad(p.id::text, 8, '0') || '.jpg') as jpg_path
                   ) thumb)
            from ingest_pairings ip
            join photos p on p.id = ip.front_photo_id
            where ip.status = 'pending'
            order by ip.id
            """
        ).fetchall()
        for r in rows:
            (pid, tpath, score, back_folder, back_name, back_seq,
             front_batch, front_seq, front_folder, front_name, _) = r
            front_thumb = settings.THUMBS_DIR / f"{_photo_id_of_front(conn, pid):08d}.jpg"
            back_thumb = Path(tpath) if tpath else None
            batch = front_batch or (front_folder.split('/', 1)[0] if front_folder else "")
            caption = (
                f"BACK PROPOSAL — {batch} #{front_seq or '?'} → #{back_seq or '?'}   "
                f"score {score:.2f}"
            )
            out.append(Proposal(
                kind="pairing", id=pid,
                left_thumb=front_thumb, right_thumb=back_thumb,
                caption=caption, score_or_distance=float(score),
            ))

        # Rescans
        rows = conn.execute(
            """
            select ir.id, ir.staging_thumb_path, ir.distance,
                   ir.new_source_folder, ir.new_source_filename, ir.new_scan_batch,
                   ir.new_scan_sequence,
                   p.id as existing_id, p.scan_batch, p.scan_sequence,
                   p.source_folder, p.source_filename
            from ingest_rescans ir
            join photos p on p.id = ir.existing_photo_id
            where ir.status = 'pending'
            order by ir.id
            """
        ).fetchall()
        for r in rows:
            (rid, tpath, distance, new_folder, new_name, new_batch, new_seq,
             existing_id, ex_batch, ex_seq, ex_folder, ex_name) = r
            existing_thumb = settings.THUMBS_DIR / f"{existing_id:08d}.jpg"
            new_thumb = Path(tpath) if tpath else None
            caption = (
                f"RESCAN — new {new_batch or new_folder} / {new_name}   "
                f"⇄ existing {ex_batch or ex_folder} #{ex_seq or '?'} / {ex_name}   "
                f"distance {distance}"
            )
            # Normalise distance (0..~64+) to a 0..1 confidence; 0 dist = 1.0.
            conf = max(0.0, 1.0 - (distance / 32.0))
            out.append(Proposal(
                kind="rescan", id=rid,
                left_thumb=existing_thumb, right_thumb=new_thumb,
                caption=caption, score_or_distance=conf,
            ))
    return out


def _photo_id_of_front(conn, pairing_id: int) -> int:
    row = conn.execute(
        "select front_photo_id from ingest_pairings where id = %s", (pairing_id,)
    ).fetchone()
    return int(row[0])
