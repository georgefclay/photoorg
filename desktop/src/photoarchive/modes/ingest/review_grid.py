"""Review grid for pending ingest proposals (backs + rescans).

Keyboard:
    A          accept (front + back photo_backs row)
    R          reject
    S          swap: re-pair with the FOLLOWING file
    N          accept as orphan (photo_backs.photo_id = null)
    F          focus filmstrip; arrows to select; Enter to make it the front
    Escape     leave filmstrip focus
    Left/Right when not in filmstrip: navigate proposals
    Ctrl+A     accept all remaining at score >= 0.9
    Z          undo (see _undo)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
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

FILMSTRIP_RADIUS = 5


@dataclass
class FilmstripCell:
    photo_id: int
    scan_sequence: int | None
    thumb_path: Path | None
    is_current_front: bool
    is_current_back: bool
    is_deleted: bool


@dataclass
class Proposal:
    kind: str  # "pairing" | "rescan"
    id: int
    left_thumb: Path | None
    right_thumb: Path | None
    caption: str
    score_or_distance: float
    back_photo_id: int | None = None
    back_aspect_mismatch: bool = False
    scan_batch: str | None = None
    back_scan_sequence: int | None = None
    back_source_folder: str = ""
    source_root: str = ""
    front_photo_id: int | None = None
    front_scan_sequence: int | None = None
    filmstrip: list[FilmstripCell] = field(default_factory=list)


class _FilmstripCellWidget(QFrame):
    """One filmstrip cell: thumbnail on top, label underneath (never
    overlaid on the pixmap). Emits `clicked` with the cell's photo_id."""
    clicked = Signal(int)

    THUMB_SIZE = 110

    def __init__(self, cell: FilmstripCell, parent=None) -> None:
        super().__init__(parent)
        self.cell = cell
        self.setFrameShape(QFrame.NoFrame)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedWidth(self.THUMB_SIZE + 8)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(2, 2, 2, 2)
        outer.setSpacing(2)

        self._thumb = QLabel()
        self._thumb.setAlignment(Qt.AlignCenter)
        self._thumb.setFixedSize(self.THUMB_SIZE, self.THUMB_SIZE)
        self._thumb.setStyleSheet("background: #111")
        if cell.thumb_path and cell.thumb_path.exists():
            pm = QPixmap(str(cell.thumb_path))
            if not pm.isNull():
                self._thumb.setPixmap(pm.scaled(
                    self._thumb.size(), Qt.KeepAspectRatio,
                    Qt.SmoothTransformation,
                ))
            else:
                self._thumb.setText("(unreadable)")
                self._thumb.setStyleSheet("background: #111; color: #666")
        else:
            self._thumb.setText("(no thumb)")
            self._thumb.setStyleSheet("background: #111; color: #666")
        outer.addWidget(self._thumb, alignment=Qt.AlignHCenter)

        self._label = QLabel()
        self._label.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self._label.setWordWrap(True)
        self._label.setTextFormat(Qt.PlainText)
        self._label.setText(self._label_text(cell))
        self._label.setStyleSheet(
            f"font-size: 9pt; color: {self._label_fg(cell)}"
        )
        outer.addWidget(self._label)

        self._focused = False
        self.setStyleSheet(_frame_style(cell, focused=False))

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.cell.photo_id)
        super().mouseReleaseEvent(event)

    def set_focused(self, on: bool) -> None:
        self._focused = on
        self.setStyleSheet(_frame_style(self.cell, focused=on))

    @staticmethod
    def _label_text(cell: FilmstripCell) -> str:
        parts = [f"#{cell.scan_sequence if cell.scan_sequence is not None else '?'}"]
        tags: list[str] = []
        if cell.is_current_front:
            tags.append("FRONT")
        if cell.is_current_back:
            tags.append("BACK")
        if cell.is_deleted:
            tags.append("del")
        if tags:
            parts.append(" ".join(tags))
        return "\n".join(parts)

    @staticmethod
    def _label_fg(cell: FilmstripCell) -> str:
        return "#666" if cell.is_deleted else "#ccc"


class _FilmstripBar(QFrame):
    """Row of clickable thumbnail cells representing scan-order neighbours."""
    picked = Signal(int)  # emits photo_id

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFrameStyle(QFrame.StyledPanel)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(4, 4, 4, 4)
        self._layout.setSpacing(4)
        self._cells: list[tuple[FilmstripCell, _FilmstripCellWidget]] = []
        self._focused = False
        self._selected_idx = 0

    def set_cells(self, cells: list[FilmstripCell]) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._cells = []
        for cell in cells:
            w = _FilmstripCellWidget(cell)
            w.clicked.connect(self.picked)
            self._layout.addWidget(w)
            self._cells.append((cell, w))
        # Default selection is the FRONT (fix-up 5 rule 3); back is
        # highlighted by its own coloured outline regardless of focus.
        # `x or y` is wrong here — index 0 is a valid selection.
        default = self._front_index()
        if default is None:
            default = self._back_index()
        self._selected_idx = default if default is not None else 0
        self._focused = False
        self._paint_focus()

    def _front_index(self) -> int | None:
        for i, (c, _) in enumerate(self._cells):
            if c.is_current_front:
                return i
        return None

    def _back_index(self) -> int | None:
        for i, (c, _) in enumerate(self._cells):
            if c.is_current_back:
                return i
        return None

    def set_focused(self, on: bool) -> None:
        self._focused = on
        if on and self._cells:
            # Start on the back if it's in the strip; else on the front.
            back = self._back_index()
            if back is not None:
                self._selected_idx = back
            else:
                front = self._front_index()
                if front is not None:
                    self._selected_idx = front
        self._paint_focus()

    def is_focused(self) -> bool:
        return self._focused

    def selected_index(self) -> int:
        return self._selected_idx

    def move_selection(self, delta: int) -> None:
        if not self._cells:
            return
        self._selected_idx = max(0, min(len(self._cells) - 1, self._selected_idx + delta))
        self._paint_focus()

    def confirm(self) -> None:
        if not self._focused or not self._cells:
            return
        cell, _ = self._cells[self._selected_idx]
        self.picked.emit(cell.photo_id)

    def _paint_focus(self) -> None:
        for i, (_cell, w) in enumerate(self._cells):
            w.set_focused(self._focused and i == self._selected_idx)


def _frame_style(cell: FilmstripCell, *, focused: bool) -> str:
    """Border colour reflects role (front/back) so it's obvious even
    when the strip isn't focused. The focus ring is a bright blue on
    top of that."""
    if focused:
        border = "3px solid #4dc3ff"
    elif cell.is_current_back:
        border = "3px solid #d97728"     # orange for the back tile
    elif cell.is_current_front:
        border = "2px solid #6dc06d"     # green for the front tile
    else:
        border = "1px solid #444"
    bg = "#222"
    if cell.is_current_front:
        bg = "#1e3a1e"
    elif cell.is_current_back:
        bg = "#3a1e1e"
    return f"_FilmstripCellWidget {{ background: {bg}; border: {border}; }}"


class ReviewGridDialog(QDialog):
    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Review proposals")
        self._settings = settings
        self.resize(1500, 950)

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
            lbl.setMinimumHeight(450)

        imgs = QHBoxLayout()
        imgs.addWidget(self._left)
        imgs.addWidget(self._right)

        self._caption = QLabel(alignment=Qt.AlignCenter)
        self._caption.setStyleSheet("font-size: 12pt")
        self._counter = QLabel(alignment=Qt.AlignCenter)
        self._counter.setStyleSheet("color: gray")
        self._batch_header = QLabel(alignment=Qt.AlignCenter)
        self._batch_header.setStyleSheet("font-weight: bold; font-size: 11pt")

        self._hi_toggle = QCheckBox("High-confidence only (score ≥ 0.9)  [F key toggles filmstrip focus]")
        self._hi_toggle.toggled.connect(self._on_hi_toggle)

        self._filmstrip = _FilmstripBar()
        self._filmstrip.picked.connect(self._filmstrip_picked)

        accept = QPushButton("Accept (A)")
        reject = QPushButton("Reject (R)")
        swap = QPushButton("Swap (S)")
        orphan = QPushButton("Orphan back (N)")
        prev = QPushButton("← Prev")
        nxt = QPushButton("Next →")
        undo = QPushButton("Undo (Z)")
        accept_all = QPushButton("Accept all ≥ 0.9 (Ctrl+A)")
        for b, cb in [
            (accept, self._accept), (reject, self._reject),
            (swap, self._swap_front),
            (orphan, self._orphan),
            (prev, self._prev), (nxt, self._next),
            (undo, self._undo), (accept_all, self._accept_all_confident),
        ]:
            b.clicked.connect(cb)

        buttons = QHBoxLayout()
        for b in (prev, reject, swap, accept, orphan, nxt, undo, accept_all):
            buttons.addWidget(b)

        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.reject)

        outer = QVBoxLayout(self)
        outer.addWidget(self._batch_header)
        outer.addWidget(self._counter)
        outer.addWidget(self._hi_toggle)
        outer.addLayout(imgs, stretch=1)
        outer.addWidget(self._caption)
        outer.addSpacerItem(QSpacerItem(1, 6))
        outer.addWidget(self._filmstrip)
        outer.addSpacerItem(QSpacerItem(1, 6))
        outer.addLayout(buttons)
        outer.addWidget(close)

        QShortcut(QKeySequence("A"), self, activated=self._accept)
        QShortcut(QKeySequence("R"), self, activated=self._reject)
        QShortcut(QKeySequence("S"), self, activated=self._swap_front)
        QShortcut(QKeySequence("N"), self, activated=self._orphan)
        QShortcut(QKeySequence("F"), self, activated=self._toggle_filmstrip_focus)
        QShortcut(QKeySequence("Escape"), self, activated=self._exit_filmstrip_focus)
        QShortcut(QKeySequence("Return"), self, activated=self._filmstrip_confirm)
        QShortcut(QKeySequence("Right"), self, activated=self._right_key)
        QShortcut(QKeySequence("Left"), self, activated=self._left_key)
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
            self._batch_header.setText("")
            self._filmstrip.set_cells([])
            return
        # Recompute filmstrip for this proposal (fresh from DB so recent
        # front changes are reflected).
        cur.filmstrip = _load_filmstrip(self._settings, cur)
        self._counter.setText(
            f"{self._idx + 1} / {len(self._pending)}"
            f"  (total pending {len(self._all_pending)})"
        )
        self._caption.setText(cur.caption)
        # Batch N of M header
        batches = sorted({p.scan_batch for p in self._pending if p.scan_batch})
        if cur.scan_batch and cur.scan_batch in batches:
            n = batches.index(cur.scan_batch) + 1
            m = len(batches)
            self._batch_header.setText(f"{cur.scan_batch} — batch {n} of {m}")
        else:
            self._batch_header.setText(cur.scan_batch or "")
        _set_pixmap(self._left, cur.left_thumb)
        _set_pixmap(self._right, cur.right_thumb)
        self._filmstrip.set_cells(cur.filmstrip)

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

    def _orphan(self) -> None:
        cur = self._current()
        if cur is None or cur.kind != "pairing":
            return
        try:
            decisions.accept_pairing_orphan(self._settings, cur.id)
        except decisions.SourceFileMissing as e:
            log.warning("orphan-accept refused — source missing: %s", e)
            QMessageBox.warning(
                self, "Orphan accept refused",
                str(e) + "\n\nExpected the file to still be at that path. "
                "Nothing was changed.",
            )
            return
        except Exception as e:
            log.exception("orphan-accept failed")
            QMessageBox.critical(self, "Orphan accept failed", str(e))
            return
        self._history.append((cur.kind, cur.id, "orphan"))
        self._all_pending = [p for p in self._all_pending
                             if not (p.id == cur.id and p.kind == cur.kind)]
        self._apply_filter()
        self._render()

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
        cur.front_photo_id = new_front_id
        cur.left_thumb = self._settings.THUMBS_DIR / f"{new_front_id:08d}.jpg"
        self._render()

    def _filmstrip_picked(self, photo_id: int) -> None:
        cur = self._current()
        if cur is None or cur.kind != "pairing":
            return
        if photo_id == cur.front_photo_id:
            return  # no-op
        if photo_id == cur.back_photo_id:
            QMessageBox.information(
                self, "Pick front",
                "That thumbnail is the back of this proposal; can't use it as the front.",
            )
            return
        try:
            decisions.change_pairing_front(self._settings, cur.id, photo_id)
        except Exception as e:
            log.exception("change front failed")
            QMessageBox.critical(self, "Pick front failed", str(e))
            return
        cur.front_photo_id = photo_id
        cur.left_thumb = self._settings.THUMBS_DIR / f"{photo_id:08d}.jpg"
        # Rebuild the caption's "#N (front)" so the change is visible.
        cur.caption = _rebuild_caption_after_front_change(self._settings, cur)
        self._render()

    def _toggle_filmstrip_focus(self) -> None:
        cur = self._current()
        if cur is None or cur.kind != "pairing":
            return
        self._filmstrip.set_focused(not self._filmstrip.is_focused())

    def _exit_filmstrip_focus(self) -> None:
        if self._filmstrip.is_focused():
            self._filmstrip.set_focused(False)

    def _filmstrip_confirm(self) -> None:
        if self._filmstrip.is_focused():
            self._filmstrip.confirm()

    def _left_key(self) -> None:
        if self._filmstrip.is_focused():
            self._filmstrip.move_selection(-1)
        else:
            self._prev()

    def _right_key(self) -> None:
        if self._filmstrip.is_focused():
            self._filmstrip.move_selection(+1)
        else:
            self._next()

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
        except decisions.SourceFileMissing as e:
            # Fix-up 6: friendly message, never a raw WinError.
            log.warning("decision refused — source missing: %s", e)
            QMessageBox.warning(
                self, "Decision refused",
                str(e) + "\n\nExpected the file to still be at that path. "
                "Nothing was changed. Run the pairing integrity check to "
                "reconcile.",
            )
            return
        except Exception as e:
            log.exception("decision failed")
            QMessageBox.critical(self, "Decision failed", str(e))
            return
        self._history.append((cur.kind, cur.id, "accept" if accept else "reject"))
        self._all_pending = [p for p in self._all_pending
                             if not (p.id == cur.id and p.kind == cur.kind)]
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
            except decisions.SourceFileMissing as e:
                log.warning("bulk accept skipped %s %d: %s", p.kind, p.id, e)
                continue
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
    scaled = pm.scaled(label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
    label.setPixmap(scaled)


def _proposal_caption(
    scan_batch: str | None, source_folder: str,
    front_seq: int | None, back_seq: int | None,
    score: float, back_pid: int | None, aspect_mismatch: bool,
    orphan_front: bool = False,
) -> str:
    batch = scan_batch or (source_folder.split('/', 1)[0] if source_folder else "")
    tags: list[str] = []
    if back_pid is not None:
        tags.append("photo-as-back")
    if aspect_mismatch:
        tags.append("aspect differs")
    if orphan_front:
        tags.append("no front")
    tag_str = f" [{', '.join(tags)}]" if tags else ""
    front_part = "front unknown" if orphan_front else f"#{front_seq or '?'} (front)"
    return (
        f"BACK PROPOSAL{tag_str} — {batch} "
        f"{front_part} ← #{back_seq or '?'} (back)   "
        f"score {score:.2f}"
    )


def _load_pending(settings: Settings) -> list[Proposal]:
    out: list[Proposal] = []
    with db.connection() as conn:
        rows = conn.execute(
            """
            select ip.id, ip.staging_thumb_path, ip.back_score,
                   ip.back_source_folder, ip.back_source_filename,
                   ip.back_scan_sequence,
                   ip.back_photo_id, ip.back_aspect_mismatch,
                   ip.front_photo_id,
                   pf.scan_batch, pf.scan_sequence,
                   pb.scan_batch as back_batch,
                   coalesce(pf.source_root, pb.source_root) as source_root
            from ingest_pairings ip
            left join photos pf on pf.id = ip.front_photo_id
            left join photos pb on pb.id = ip.back_photo_id
            where ip.status = 'pending'
            order by coalesce(pf.scan_batch, pb.scan_batch) nulls last,
                     ip.back_scan_sequence nulls last, ip.id
            """
        ).fetchall()
        for r in rows:
            (pair_id, tpath, score, back_folder, back_name, back_seq,
             back_pid, back_aspect_mismatch,
             front_photo_id, front_batch, front_seq, back_batch,
             source_root) = r
            batch = front_batch or back_batch
            # Fix-up 5: resolve thumbs the same way the filmstrip does.
            # For a photo-as-back proposal (back_photo_id set) the thumb
            # lives at THUMBS_DIR/{back_pid:08d}.jpg — the staged path is
            # only relevant for held (not-yet-committed) backs. Same for
            # the front — front_photo_id is always a committed photo.
            front_thumb: Path | None = None
            if front_photo_id is not None:
                front_thumb = settings.THUMBS_DIR / f"{front_photo_id:08d}.jpg"
            if back_pid is not None:
                back_thumb: Path | None = (
                    settings.THUMBS_DIR / f"{back_pid:08d}.jpg"
                )
            elif tpath:
                back_thumb = Path(tpath)
            else:
                back_thumb = None
            caption = _proposal_caption(
                batch, back_folder, front_seq, back_seq,
                float(score), back_pid, bool(back_aspect_mismatch),
                orphan_front=(front_photo_id is None),
            )
            out.append(Proposal(
                kind="pairing", id=pair_id,
                left_thumb=front_thumb, right_thumb=back_thumb,
                caption=caption, score_or_distance=float(score),
                back_photo_id=back_pid,
                back_aspect_mismatch=bool(back_aspect_mismatch),
                scan_batch=batch,
                back_scan_sequence=back_seq,
                back_source_folder=back_folder,
                source_root=source_root,
                front_photo_id=front_photo_id,
                front_scan_sequence=front_seq,
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


def _load_filmstrip(settings: Settings, proposal: Proposal) -> list[FilmstripCell]:
    if proposal.kind != "pairing" or proposal.back_scan_sequence is None:
        return []
    lo = max(1, proposal.back_scan_sequence - FILMSTRIP_RADIUS)
    hi = proposal.back_scan_sequence + FILMSTRIP_RADIUS
    cells: list[FilmstripCell] = []
    with db.connection() as conn:
        rows = conn.execute(
            """
            select id, scan_sequence, is_deleted
            from photos
            where source_root = %s
              and source_folder = %s
              and scan_sequence between %s and %s
            order by scan_sequence asc
            """,
            (proposal.source_root, proposal.back_source_folder, lo, hi),
        ).fetchall()
    for pid, seq, is_del in rows:
        cells.append(FilmstripCell(
            photo_id=pid,
            scan_sequence=seq,
            thumb_path=settings.THUMBS_DIR / f"{pid:08d}.jpg",
            is_current_front=(pid == proposal.front_photo_id),
            is_current_back=(pid == proposal.back_photo_id),
            is_deleted=bool(is_del),
        ))
    return cells


def _rebuild_caption_after_front_change(settings: Settings, cur: Proposal) -> str:
    """Fetch the new front's scan_sequence and rebuild the caption text."""
    if cur.front_photo_id is None:
        return cur.caption
    with db.connection() as conn:
        row = conn.execute(
            "select scan_sequence, scan_batch from photos where id = %s",
            (cur.front_photo_id,),
        ).fetchone()
    if row is None:
        return cur.caption
    cur.front_scan_sequence, cur.scan_batch = row
    return _proposal_caption(
        cur.scan_batch, cur.back_source_folder,
        cur.front_scan_sequence, cur.back_scan_sequence,
        cur.score_or_distance, cur.back_photo_id, cur.back_aspect_mismatch,
    )


def _swap_front_in_db(settings: Settings, pair_id: int) -> tuple[int, str]:
    """Re-point a pairing's front to the FOLLOWING file in scan order.
    Works when front is currently NULL (orphan) too — resolves source_root
    via the back's own photo (photo-as-back proposals always have
    back_photo_id set).

    Returns (new_front_photo_id, new_caption)."""
    with db.connection() as conn:
        conn.autocommit = False
        try:
            row = conn.execute(
                """
                select ip.front_photo_id, ip.back_scan_sequence,
                       ip.back_source_folder, ip.back_score, ip.back_photo_id,
                       ip.back_aspect_mismatch,
                       coalesce(pf.source_root, pb.source_root) as source_root
                from ingest_pairings ip
                left join photos pf on pf.id = ip.front_photo_id
                left join photos pb on pb.id = ip.back_photo_id
                where ip.id = %s for update
                """,
                (pair_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"pairing {pair_id} not found")
            (front_id, back_seq, back_folder, score, back_pid,
             aspect_mismatch, source_root) = row
            if source_root is None:
                raise LookupError("Can't determine source_root for this proposal.")
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
            caption = _proposal_caption(
                new_batch, back_folder, new_seq, back_seq,
                float(score), back_pid, bool(aspect_mismatch),
            ) + "  (swapped)"
            return new_id, caption
        except Exception:
            conn.rollback()
            raise
