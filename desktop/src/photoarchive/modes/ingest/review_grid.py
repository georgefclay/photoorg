"""Review grid for pending ingest proposals (backs + rescans).

Keyboard:
    A          accept
    R          reject
    S          swap front — re-pair the back with the FOLLOWING file
    Left/Right navigate
    F          toggle "high-confidence first" filter (score >= 0.9)
    Ctrl+A     accept everything remaining at >= 0.9
    Z          undo (see note in _undo)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpacerItem,
    QVBoxLayout,
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
    score_or_distance: float
    back_photo_id: int | None = None  # non-null → photo-as-back proposal


class ReviewGridDialog(QDialog):
    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Review proposals")
        self._settings = settings
        self.resize(1400, 900)

        self._all_pending: list[Proposal] = _load_pending(settings)
        self._pending: list[Proposal] = list(self._all_pending)
        self._history: list[tuple[str, int, str]] = []
        self._idx = 0
        self._filter_hi_confidence = False

        self._left = QLabel(alignment=Qt.AlignCenter)
        self._right = QLabel(alignment=Qt.AlignCenter)
        for lbl in (self._left, self._right):
            lbl.setStyleSheet("background: #222; color: #ccc")
            lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            lbl.setMinimumHeight(500)

        imgs = QHBoxLayout()
        imgs.addWidget(self._left)
        imgs.addWidget(self._right)

        self._caption = QLabel(alignment=Qt.AlignCenter)
        self._caption.setStyleSheet("font-size: 12pt")
        self._counter = QLabel(alignment=Qt.AlignCenter)
        self._counter.setStyleSheet("color: gray")

        self._hi_toggle = QCheckBox("High-confidence only (score ≥ 0.9)  [F]")
        self._hi_toggle.toggled.connect(self._on_hi_toggle)

        accept = QPushButton("Accept (A)")
        reject = QPushButton("Reject (R)")
        swap = QPushButton("Swap front (S)")
        prev = QPushButton("← Prev")
        nxt = QPushButton("Next →")
        undo = QPushButton("Undo (Z)")
        accept_all = QPushButton("Accept all ≥ 0.9 (Ctrl+A)")
        for b, cb in [
            (accept, self._accept), (reject, self._reject),
            (swap, self._swap_front),
            (prev, self._prev), (nxt, self._next),
            (undo, self._undo), (accept_all, self._accept_all_confident),
        ]:
            b.clicked.connect(cb)

        buttons = QHBoxLayout()
        for b in (prev, reject, swap, accept, nxt, undo, accept_all):
            buttons.addWidget(b)

        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.reject)

        outer = QVBoxLayout(self)
        outer.addWidget(self._counter)
        outer.addWidget(self._hi_toggle)
        outer.addLayout(imgs, stretch=1)
        outer.addWidget(self._caption)
        outer.addSpacerItem(QSpacerItem(1, 8))
        outer.addLayout(buttons)
        outer.addWidget(close)

        QShortcut(QKeySequence("A"), self, activated=self._accept)
        QShortcut(QKeySequence("R"), self, activated=self._reject)
        QShortcut(QKeySequence("S"), self, activated=self._swap_front)
        QShortcut(QKeySequence("F"), self, activated=self._hi_toggle.toggle)
        QShortcut(QKeySequence("Right"), self, activated=self._next)
        QShortcut(QKeySequence("Left"), self, activated=self._prev)
        QShortcut(QKeySequence("Z"), self, activated=self._undo)
        QShortcut(QKeySequence("Ctrl+A"), self, activated=self._accept_all_confident)

        self._render()

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
            self._counter.setText(f"0 / 0  (total {len(self._all_pending)})")
            return
        self._counter.setText(
            f"{self._idx + 1} / {len(self._pending)}"
            f"  (total pending {len(self._all_pending)})"
        )
        self._caption.setText(cur.caption)
        _set_pixmap(self._left, cur.left_thumb)
        _set_pixmap(self._right, cur.right_thumb)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cur = self._current()
        if cur is not None:
            _set_pixmap(self._left, cur.left_thumb)
            _set_pixmap(self._right, cur.right_thumb)

    def _on_hi_toggle(self, on: bool) -> None:
        self._filter_hi_confidence = on
        self._apply_filter()
        self._render()

    def _apply_filter(self) -> None:
        if self._filter_hi_confidence:
            self._pending = [p for p in self._all_pending if p.score_or_distance >= 0.9]
        else:
            self._pending = list(self._all_pending)
        self._idx = 0

    def _accept(self) -> None:
        self._apply(accept=True)

    def _reject(self) -> None:
        self._apply(accept=False)

    def _swap_front(self) -> None:
        cur = self._current()
        if cur is None or cur.kind != "pairing":
            return
        try:
            new_front_id, new_caption = _swap_front_in_db(self._settings, cur.id)
        except LookupError as e:
            QMessageBox.information(self, "Swap front", str(e))
            return
        except Exception as e:
            log.exception("swap front failed")
            QMessageBox.critical(self, "Swap failed", str(e))
            return
        cur.caption = new_caption
        cur.left_thumb = self._settings.THUMBS_DIR / f"{new_front_id:08d}.jpg"
        self._render()

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
        self._all_pending = [p for p in self._all_pending if p.id != cur.id or p.kind != cur.kind]
        self._apply_filter()
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
        QMessageBox.information(
            self, "Undo",
            "Undo isn't reversible from here in Phase 2 — reload the review "
            "to see fresh state. Manually reversing accepted proposals means "
            "editing the DB.",
        )

    def _accept_all_confident(self) -> None:
        threshold = 0.9
        candidates = [p for p in self._pending if p.score_or_distance >= threshold]
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
        for p in candidates:
            try:
                if p.kind == "pairing":
                    decisions.accept_pairing(self._settings, p.id)
                else:
                    decisions.accept_rescan(self._settings, p.id)
                self._history.append((p.kind, p.id, "accept"))
            except Exception as e:
                log.exception("bulk accept failed for %s %d", p.kind, p.id)
                QMessageBox.critical(self, "Bulk accept aborted", str(e))
                break
        accepted_ids = {(p.kind, p.id) for p in candidates}
        self._all_pending = [p for p in self._all_pending
                             if (p.kind, p.id) not in accepted_ids]
        self._apply_filter()
        self._render()


def _set_pixmap(label: QLabel, path: Path | None) -> None:
    if path is None or not path.exists():
        label.setText("(no thumb)")
        return
    pm = QPixmap(str(path))
    if pm.isNull():
        label.setText("(unreadable)")
        return
    # KeepAspectRatio + FastTransformation avoids over-sampling the tiny
    # 320px thumbs. Use the label's *current* size so we scale up to fill
    # the whole available area on window resize.
    scaled = pm.scaled(label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
    label.setPixmap(scaled)


def _load_pending(settings: Settings) -> list[Proposal]:
    out: list[Proposal] = []
    with db.connection() as conn:
        rows = conn.execute(
            """
            select ip.id, ip.staging_thumb_path, ip.back_score,
                   ip.back_source_folder, ip.back_source_filename,
                   ip.back_scan_sequence,
                   ip.back_photo_id,
                   p.scan_batch, p.scan_sequence, p.source_folder,
                   p.source_filename,
                   p.id as front_photo_id
            from ingest_pairings ip
            join photos p on p.id = ip.front_photo_id
            where ip.status = 'pending'
            order by ip.back_score desc, ip.id
            """
        ).fetchall()
        for r in rows:
            (pair_id, tpath, score, back_folder, back_name, back_seq,
             back_pid,
             front_batch, front_seq, front_folder, front_name,
             front_photo_id) = r
            front_thumb = settings.THUMBS_DIR / f"{front_photo_id:08d}.jpg"
            back_thumb = Path(tpath) if tpath else None
            batch = front_batch or (front_folder.split('/', 1)[0] if front_folder else "")
            source_hint = " [photo-as-back]" if back_pid is not None else ""
            caption = (
                f"BACK PROPOSAL{source_hint} — {batch} "
                f"#{front_seq or '?'} (front) ← #{back_seq or '?'} (back)   "
                f"score {score:.2f}"
            )
            out.append(Proposal(
                kind="pairing", id=pair_id,
                left_thumb=front_thumb, right_thumb=back_thumb,
                caption=caption, score_or_distance=float(score),
                back_photo_id=back_pid,
            ))

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
            order by ir.distance, ir.id
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
            conf = max(0.0, 1.0 - (distance / 32.0))
            out.append(Proposal(
                kind="rescan", id=rid,
                left_thumb=existing_thumb, right_thumb=new_thumb,
                caption=caption, score_or_distance=conf,
            ))
    return out


def _swap_front_in_db(settings: Settings, pair_id: int) -> tuple[int, str]:
    """Re-point a pairing's front to the FOLLOWING file in scan order.
    Returns (new_front_photo_id, new_caption)."""
    with db.connection() as conn:
        conn.autocommit = False
        try:
            row = conn.execute(
                """
                select ip.front_photo_id, ip.back_scan_sequence,
                       ip.back_source_folder, ip.back_score,
                       p.source_root
                from ingest_pairings ip
                join photos p on p.id = ip.front_photo_id
                where ip.id = %s for update
                """,
                (pair_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"pairing {pair_id} not found")
            front_id, back_seq, back_folder, score, source_root = row
            if back_seq is None:
                raise LookupError("Back has no scan_sequence; can't swap.")
            new_front = conn.execute(
                """
                select id, scan_batch, scan_sequence, source_filename
                from photos
                where source_root = %s
                  and source_folder = %s
                  and scan_sequence > %s
                  and not is_deleted
                order by scan_sequence asc
                limit 1
                """,
                (source_root, back_folder, back_seq),
            ).fetchone()
            if new_front is None:
                raise LookupError(
                    "No following file in this folder — can't swap."
                )
            new_id, new_batch, new_seq, _ = new_front
            conn.execute(
                "update ingest_pairings set front_photo_id = %s where id = %s",
                (new_id, pair_id),
            )
            db.audit(
                conn, actor="desktop", action="pairing.swap_front",
                entity_type="ingest_pairing", entity_id=pair_id,
                previous_value={"front_photo_id": front_id},
                new_value={"front_photo_id": new_id, "new_seq": new_seq},
            )
            conn.commit()
            batch = new_batch or (back_folder.split('/', 1)[0] if back_folder else "")
            caption = (
                f"BACK PROPOSAL (swapped) — {batch} "
                f"#{new_seq or '?'} (front) ← #{back_seq or '?'} (back)   "
                f"score {score:.2f}"
            )
            return new_id, caption
        except Exception:
            conn.rollback()
            raise
