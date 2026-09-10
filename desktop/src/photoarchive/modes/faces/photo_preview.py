"""Full-photo preview for the Faces mode.

Assembled here:
- `PhotoCanvas` (fix-up 5) — paints the photo + face overlays; also
  supports manual bbox editing since fix-up 9: drag a box to move it,
  drag its corner to resize, drag on empty canvas to draw a new box,
  arrow keys nudge (Shift = larger step), Delete removes. All DB
  writes happen in the parent via signals.
- `BackPanel` (fix-up 10) — back image, transcription with chips,
  Fix / Confirm buttons.
- `SuggestionsPanel` (fix-up 10) — date / description / folder
  suggestions, with an Accept-date button that promotes into
  `photos.capture_date`.
- `PhotoPreviewWidget` — composes header + canvas + back panel +
  suggestions block. `T` key on the parent toggles the main pane
  between the front photo and the back scan.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps
from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QImage,
    QMouseEvent,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import repo

log = logging.getLogger(__name__)


PEEK_HOLD_MS = 300         # Space press-and-release faster than this stays "locked" open
CORNER_HANDLE_PX = 10      # radius within which a click is treated as a resize handle
DRAG_THRESHOLD_PX = 4      # release within this of the press is a click, not a drag


@dataclass
class _Overlay:
    """Rect on the widget canvas + a payload for click routing."""
    canvas_rect: QRectF
    face_id: int
    person_id: int | None
    person_name: str | None
    is_current: bool
    is_disputed: bool


# --- PhotoCanvas -----------------------------------------------------------


class PhotoCanvas(QWidget):
    """Paints the full photo and its face overlays; emits face clicks
    and bbox edits (fix-up 9)."""

    face_clicked = Signal(int, bool)           # (face_id, is_labelled)
    face_bbox_changed = Signal(int, dict)      # (face_id, new_bbox_in_image_coords)
    new_face_requested = Signal(dict)          # bbox in image coords
    face_delete_requested = Signal(int)        # face_id

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self._image: QImage | None = None
        self._image_w: int = 0
        self._image_h: int = 0
        self._context: repo.PhotoContext | None = None
        self._current_face_id: int | None = None
        self._overlays: list[_Overlay] = []
        self._load_error: str | None = None
        # Bbox-edit drag state (fix-up 9).
        self._drag_kind: str | None = None           # 'move' | 'resize' | 'draw'
        self._drag_face_id: int | None = None
        self._drag_corner: str | None = None         # 'nw' | 'ne' | 'sw' | 'se'
        self._drag_start_canvas: QPointF | None = None
        self._drag_start_bbox: dict | None = None    # image coords
        self._drag_current_bbox: dict | None = None
        self._display_scale: float = 1.0
        self._display_offset: tuple[int, int] = (0, 0)

    def set_photo(
        self,
        context: repo.PhotoContext | None,
        current_face_id: int | None,
    ) -> None:
        self._context = context
        self._current_face_id = current_face_id
        self._image = None
        self._load_error = None
        self._drag_kind = None
        self._drag_current_bbox = None
        if context is None:
            self._image_w = self._image_h = 0
            self.update()
            return
        if context.working_path:
            path = Path(context.working_path)
            abs_path = str(path.resolve()) if path.exists() else str(path)
            if not path.exists():
                log.warning(
                    "PhotoCanvas: working file missing for photo %d at %s "
                    "(consider `python -m photoarchive.tools.check_working_files`)",
                    context.photo_id, abs_path,
                )
                self._load_error = f"working file missing:\n{abs_path}"
            else:
                try:
                    with Image.open(path) as im:
                        im = ImageOps.exif_transpose(im)
                        if im.mode != "RGB":
                            im = im.convert("RGB")
                        data = im.tobytes("raw", "RGB")
                        self._image = QImage(
                            data, im.width, im.height, 3 * im.width,
                            QImage.Format_RGB888,
                        ).copy()
                        self._image_w, self._image_h = im.width, im.height
                except Exception as e:
                    log.warning("PhotoCanvas: could not load %s: %s", abs_path, e)
                    self._load_error = f"could not load working file:\n{abs_path}\n{e}"
        if self._image is None:
            self._image_w = context.width or 1
            self._image_h = context.height or 1
        self._overlays.clear()
        self.update()

    def show_arbitrary_image(self, path: Path, header: str | None = None) -> None:
        """Fix-up 10: T key swaps to a back image. Same rendering pipeline
        as the front (exif-transpose + RGB coerce) but with no face overlays."""
        self._context = None
        self._current_face_id = None
        self._overlays.clear()
        self._image = None
        self._load_error = None
        if not path.exists():
            self._load_error = f"back image missing:\n{path}"
            self._image_w = self._image_h = 1
            self.update()
            return
        try:
            with Image.open(path) as im:
                im = ImageOps.exif_transpose(im)
                if im.mode != "RGB":
                    im = im.convert("RGB")
                data = im.tobytes("raw", "RGB")
                self._image = QImage(
                    data, im.width, im.height, 3 * im.width,
                    QImage.Format_RGB888,
                ).copy()
                self._image_w, self._image_h = im.width, im.height
        except Exception as e:
            log.warning("PhotoCanvas: could not load back %s: %s", path, e)
            self._load_error = f"could not load back:\n{path}\n{e}"
            self._image_w = self._image_h = 1
        self.update()

    # --- painting ---------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), QColor("#101010"))
        if self._image_w == 0 or self._image_h == 0:
            p.setPen(QPen(QColor("#888")))
            p.drawText(self.rect(), Qt.AlignCenter, "No preview.")
            return
        widget_w = self.width()
        widget_h = self.height()
        scale = min(widget_w / self._image_w, widget_h / self._image_h)
        draw_w = int(self._image_w * scale)
        draw_h = int(self._image_h * scale)
        ox = (widget_w - draw_w) // 2
        oy = (widget_h - draw_h) // 2
        self._display_scale = scale
        self._display_offset = (ox, oy)

        if self._image is not None:
            target = QRect(ox, oy, draw_w, draw_h)
            p.drawImage(target, self._image)
        else:
            p.setPen(QPen(QColor("#666")))
            p.drawRect(ox, oy, draw_w, draw_h)
            banner = self._load_error or (
                f"Working file missing:\n{self._context.working_path}"
                if self._context else "No preview."
            )
            p.drawText(
                QRect(ox, oy, draw_w, draw_h),
                Qt.AlignCenter | Qt.TextWordWrap,
                banner,
            )

        self._overlays.clear()
        if self._context is None:
            return

        label_font = QFont()
        label_font.setPointSize(9)
        p.setFont(label_font)
        fm = QFontMetrics(label_font)
        for face in self._context.faces:
            # If this face is currently being dragged, use the live rect.
            if (self._drag_face_id == face.face_id
                    and self._drag_current_bbox is not None):
                bbox = self._drag_current_bbox
            else:
                bbox = face.bbox
            rect = self._image_bbox_to_canvas(bbox)
            is_current = (face.face_id == self._current_face_id)
            if is_current:
                colour = QColor("#ffd94d")
            elif face.is_disputed:
                colour = QColor("#e35555")
            elif face.review_status == "ignore":
                colour = QColor("#7f7f7f")
            elif face.review_status == "unknown":
                colour = QColor("#66d9ef")
            else:
                colour = QColor("#ffffff")
            pen = QPen(colour)
            pen.setWidth(3 if is_current else 2)
            p.setPen(pen)
            p.drawRect(rect)
            if is_current:
                # Draw four small corner handles.
                for corner in (rect.topLeft(), rect.topRight(),
                               rect.bottomLeft(), rect.bottomRight()):
                    handle = QRectF(corner.x() - 4, corner.y() - 4, 8, 8)
                    p.fillRect(handle, QColor("#ffd94d"))

            if face.person_name:
                label = face.person_name
            elif face.review_status == "unknown":
                label = "unknown"
            elif face.review_status == "ignore":
                label = "ignore"
            elif face.person_id is None:
                label = "unlabelled"
            else:
                label = f"person {face.person_id}"
            text_w = fm.horizontalAdvance(label) + 8
            text_h = fm.height() + 2
            lx = int(rect.left())
            ly = int(rect.top()) - text_h - 2
            if ly < 0:
                ly = int(rect.bottom()) + 2
            p.fillRect(lx, ly, text_w, text_h, QColor(0, 0, 0, 190))
            p.setPen(QPen(colour))
            p.drawText(lx + 4, ly + fm.ascent() + 1, label)

            self._overlays.append(_Overlay(
                canvas_rect=rect,
                face_id=face.face_id,
                person_id=face.person_id,
                person_name=face.person_name,
                is_current=is_current,
                is_disputed=face.is_disputed,
            ))

        # In-progress "draw new box" rectangle.
        if self._drag_kind == "draw" and self._drag_current_bbox is not None:
            r = self._image_bbox_to_canvas(self._drag_current_bbox)
            pen = QPen(QColor("#7ce07c"))
            pen.setWidth(2)
            pen.setStyle(Qt.DashLine)
            p.setPen(pen)
            p.drawRect(r)

    # --- input ------------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton:
            return
        pos = QPointF(event.position())
        self._drag_start_canvas = pos
        self._drag_current_bbox = None
        # Corner hit on the current face → resize.
        if self._current_face_id is not None:
            current_ov = next(
                (o for o in self._overlays if o.face_id == self._current_face_id),
                None,
            )
            if current_ov is not None:
                corner = _corner_hit(current_ov.canvas_rect, pos)
                if corner is not None:
                    self._drag_kind = "resize"
                    self._drag_face_id = self._current_face_id
                    self._drag_corner = corner
                    self._drag_start_bbox = dict(self._face_bbox(self._current_face_id) or {})
                    return
        # Body hit on any face → could be a click OR a move (decided on release).
        for ov in self._overlays:
            if ov.canvas_rect.contains(pos):
                self._drag_kind = "move"
                self._drag_face_id = ov.face_id
                self._drag_start_bbox = dict(self._face_bbox(ov.face_id) or {})
                return
        # Empty space press → potential new-face draw.
        self._drag_kind = "draw"
        self._drag_face_id = None
        image_pt = self._canvas_to_image(pos)
        if image_pt is None:
            self._drag_kind = None
            return
        self._drag_start_bbox = {"x": image_pt[0], "y": image_pt[1], "w": 0, "h": 0}
        self._drag_current_bbox = dict(self._drag_start_bbox)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag_kind is None or self._drag_start_canvas is None:
            return
        pos = QPointF(event.position())
        dx_canvas = pos.x() - self._drag_start_canvas.x()
        dy_canvas = pos.y() - self._drag_start_canvas.y()
        image_pt = self._canvas_to_image(pos)
        if image_pt is None:
            return

        if self._drag_kind == "move" and self._drag_start_bbox is not None:
            # Only start moving once past the click/drag threshold.
            if abs(dx_canvas) < DRAG_THRESHOLD_PX and abs(dy_canvas) < DRAG_THRESHOLD_PX:
                return
            image_dx = dx_canvas / max(self._display_scale, 1e-6)
            image_dy = dy_canvas / max(self._display_scale, 1e-6)
            self._drag_current_bbox = {
                "x": self._drag_start_bbox["x"] + image_dx,
                "y": self._drag_start_bbox["y"] + image_dy,
                "w": self._drag_start_bbox["w"],
                "h": self._drag_start_bbox["h"],
            }
        elif self._drag_kind == "resize" and self._drag_start_bbox is not None:
            self._drag_current_bbox = _resize_bbox(
                self._drag_start_bbox, self._drag_corner, image_pt,
            )
        elif self._drag_kind == "draw" and self._drag_start_bbox is not None:
            x0, y0 = self._drag_start_bbox["x"], self._drag_start_bbox["y"]
            x1, y1 = image_pt
            self._drag_current_bbox = {
                "x": min(x0, x1), "y": min(y0, y1),
                "w": abs(x1 - x0), "h": abs(y1 - y0),
            }
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton or self._drag_kind is None:
            return
        pos = QPointF(event.position())
        released_kind = self._drag_kind
        face_id = self._drag_face_id
        proposed = self._drag_current_bbox
        start = self._drag_start_canvas
        self._drag_kind = None
        self._drag_face_id = None
        self._drag_corner = None
        self._drag_start_canvas = None
        self._drag_start_bbox = None
        self._drag_current_bbox = None

        if released_kind == "move":
            dx = abs(pos.x() - (start.x() if start else pos.x()))
            dy = abs(pos.y() - (start.y() if start else pos.y()))
            if dx < DRAG_THRESHOLD_PX and dy < DRAG_THRESHOLD_PX:
                # It was actually a click — route the existing signal.
                if face_id is not None:
                    ov = next((o for o in self._overlays if o.face_id == face_id), None)
                    is_labelled = ov.person_id is not None if ov else False
                    self.face_clicked.emit(face_id, is_labelled)
                self.update()
                return
            if face_id is not None and proposed is not None:
                self.face_bbox_changed.emit(face_id, proposed)
        elif released_kind == "resize":
            if face_id is not None and proposed is not None:
                self.face_bbox_changed.emit(face_id, proposed)
        elif released_kind == "draw":
            if proposed is not None and proposed["w"] >= 8 and proposed["h"] >= 8:
                self.new_face_requested.emit(proposed)
        self.update()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        # Arrow-key nudge on the current face (fix-up 9).
        key = event.key()
        if key not in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down):
            super().keyPressEvent(event)
            return
        if self._current_face_id is None:
            return
        step = 10 if event.modifiers() & Qt.ShiftModifier else 2
        bbox = self._face_bbox(self._current_face_id)
        if bbox is None:
            return
        dx = -step if key == Qt.Key_Left else (step if key == Qt.Key_Right else 0)
        dy = -step if key == Qt.Key_Up else (step if key == Qt.Key_Down else 0)
        new_bbox = {
            "x": float(bbox.get("x", 0)) + dx,
            "y": float(bbox.get("y", 0)) + dy,
            "w": float(bbox.get("w", 0)),
            "h": float(bbox.get("h", 0)),
        }
        self.face_bbox_changed.emit(self._current_face_id, new_bbox)

    # --- helpers ---------------------------------------------------------

    def _face_bbox(self, face_id: int) -> dict | None:
        if self._context is None:
            return None
        for f in self._context.faces:
            if f.face_id == face_id:
                return dict(f.bbox)
        return None

    def _image_bbox_to_canvas(self, bbox: dict) -> QRectF:
        ox, oy = self._display_offset
        s = self._display_scale
        return QRectF(
            float(bbox.get("x", 0)) * s + ox,
            float(bbox.get("y", 0)) * s + oy,
            float(bbox.get("w", 0)) * s,
            float(bbox.get("h", 0)) * s,
        )

    def _canvas_to_image(self, pos: QPointF) -> tuple[float, float] | None:
        ox, oy = self._display_offset
        s = self._display_scale
        if s <= 0:
            return None
        ix = (pos.x() - ox) / s
        iy = (pos.y() - oy) / s
        if ix < 0 or iy < 0 or ix > self._image_w or iy > self._image_h:
            return None
        return ix, iy


def _corner_hit(rect: QRectF, pt: QPointF) -> str | None:
    corners = {
        "nw": rect.topLeft(),
        "ne": rect.topRight(),
        "sw": rect.bottomLeft(),
        "se": rect.bottomRight(),
    }
    for name, corner in corners.items():
        if abs(pt.x() - corner.x()) <= CORNER_HANDLE_PX and abs(pt.y() - corner.y()) <= CORNER_HANDLE_PX:
            return name
    return None


def _resize_bbox(start_bbox: dict, corner: str | None, image_pt: tuple[float, float]) -> dict:
    x = float(start_bbox["x"])
    y = float(start_bbox["y"])
    w = float(start_bbox["w"])
    h = float(start_bbox["h"])
    px, py = image_pt
    if corner == "nw":
        new_x, new_y = px, py
        new_w = (x + w) - new_x
        new_h = (y + h) - new_y
    elif corner == "ne":
        new_x = x
        new_y = py
        new_w = px - x
        new_h = (y + h) - new_y
    elif corner == "sw":
        new_x = px
        new_y = y
        new_w = (x + w) - new_x
        new_h = py - y
    else:  # 'se' or None
        new_x = x
        new_y = y
        new_w = px - x
        new_h = py - y
    # Never let a resize go through zero — clamp to a minimum size.
    new_w = max(new_w, 4.0)
    new_h = max(new_h, 4.0)
    return {"x": new_x, "y": new_y, "w": new_w, "h": new_h}


# --- BackPanel (fix-up 10) ------------------------------------------------


class BackPanel(QWidget):
    """Below the main canvas — back image thumbnail + transcription +
    Fix / Confirm buttons + chips for parsed_dates and names."""

    confirm_requested = Signal(int)                    # photo_back_id
    edit_confirmed = Signal(int, str)                  # photo_back_id, new_text
    toggle_main_pane_requested = Signal(int)           # photo_back_id (T key routes through)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(8)

        # Left: back thumbnail.
        self.thumb = QLabel("(no back)")
        self.thumb.setFixedWidth(220)
        self.thumb.setAlignment(Qt.AlignCenter)
        self.thumb.setStyleSheet("background: #202020; color: #888")
        self.thumb.setFrameShape(QFrame.StyledPanel)
        outer.addWidget(self.thumb)

        # Right: transcription + chips + buttons.
        right = QVBoxLayout()
        right.setSpacing(4)

        self.status_line = QLabel("")
        self.status_line.setStyleSheet("color: #bfbfbf")
        self.status_line.setWordWrap(True)
        right.addWidget(self.status_line)

        self.text_view = QTextEdit()
        self.text_view.setReadOnly(True)
        self.text_view.setFixedHeight(120)
        right.addWidget(self.text_view)

        self.chips = QLabel("")
        self.chips.setStyleSheet("color: #a0d0ff")
        self.chips.setWordWrap(True)
        right.addWidget(self.chips)

        buttons = QHBoxLayout()
        self.toggle_btn = QPushButton("Show back image (T)")
        self.toggle_btn.clicked.connect(self._on_toggle)
        buttons.addWidget(self.toggle_btn)
        self.confirm_btn = QPushButton("Confirm transcription")
        self.confirm_btn.clicked.connect(self._on_confirm)
        buttons.addWidget(self.confirm_btn)
        self.edit_btn = QPushButton("Fix transcription…")
        self.edit_btn.clicked.connect(self._on_edit)
        buttons.addWidget(self.edit_btn)
        buttons.addStretch(1)
        right.addLayout(buttons)

        outer.addLayout(right, 1)

        self._back: repo.PhotoBack | None = None

    def set_back(self, back: repo.PhotoBack | None) -> None:
        self._back = back
        if back is None:
            self.thumb.setPixmap(QPixmap())
            self.thumb.setText("(no back)")
            self.status_line.setText("")
            self.text_view.setPlainText("")
            self.chips.setText("")
            for w in (self.toggle_btn, self.confirm_btn, self.edit_btn):
                w.setEnabled(False)
            return
        for w in (self.toggle_btn, self.confirm_btn, self.edit_btn):
            w.setEnabled(True)
        # Thumbnail from working path (or master as fallback).
        path = Path(back.working_path) if back.working_path else Path(back.master_path)
        pm = QPixmap()
        if path.exists():
            try:
                with Image.open(path) as im:
                    im = ImageOps.exif_transpose(im)
                    if im.mode != "RGB":
                        im = im.convert("RGB")
                    im.thumbnail((200, 200), Image.LANCZOS)
                    data = im.tobytes("raw", "RGB")
                    q = QImage(
                        data, im.width, im.height, 3 * im.width,
                        QImage.Format_RGB888,
                    ).copy()
                    pm = QPixmap.fromImage(q)
            except Exception as e:
                log.warning("BackPanel: could not thumbnail %s: %s", path, e)
        if pm.isNull():
            self.thumb.setPixmap(QPixmap())
            self.thumb.setText(f"(back image missing)\n{path}")
        else:
            self.thumb.setPixmap(pm)

        status_bits = []
        if back.transcription_confidence is not None:
            status_bits.append(f"confidence {back.transcription_confidence:.2f}")
        if back.orientation_used:
            status_bits.append(f"orientation {back.orientation_used}")
        if back.transcription_confirmed:
            status_bits.append("confirmed")
        self.status_line.setText(" · ".join(status_bits) or "unreviewed")
        self.text_view.setPlainText(back.transcribed_text or "")

        chips = []
        for pd in back.parsed_dates:
            text = pd.get("text") or pd.get("iso") or ""
            if text:
                chips.append(f"📅 {text}")
        for n in back.names:
            if n:
                chips.append(f"👤 {n}")
        self.chips.setText("  ·  ".join(chips))

    def current_back(self) -> repo.PhotoBack | None:
        return self._back

    def _on_toggle(self) -> None:
        if self._back is not None:
            self.toggle_main_pane_requested.emit(self._back.id)

    def _on_confirm(self) -> None:
        if self._back is not None:
            self.confirm_requested.emit(self._back.id)

    def _on_edit(self) -> None:
        if self._back is None:
            return
        from PySide6.QtWidgets import QDialog, QDialogButtonBox
        dlg = QDialog(self)
        dlg.setWindowTitle("Fix transcription")
        lay = QVBoxLayout(dlg)
        editor = QTextEdit()
        editor.setPlainText(self._back.transcribed_text or "")
        editor.setFixedSize(500, 240)
        lay.addWidget(editor)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        lay.addWidget(buttons)
        if dlg.exec() != QDialog.Accepted:
            return
        new_text = editor.toPlainText()
        self.edit_confirmed.emit(self._back.id, new_text)


# --- SuggestionsPanel (fix-up 10) -----------------------------------------


class SuggestionsPanel(QWidget):
    """Fix-up 10 item 3: read-only list of the photo's AI/import
    suggestions, with an Accept-date button per date suggestion that
    promotes into `photos.capture_date`."""

    accept_date_requested = Signal(int)  # suggestion_id

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(4)
        self.header = QLabel("Suggestions")
        self.header.setStyleSheet("font-weight: bold")
        outer.addWidget(self.header)
        self.body = QLabel("(none)")
        self.body.setWordWrap(True)
        outer.addWidget(self.body)
        # Container that gets refilled per photo.
        self._buttons_holder = QWidget()
        self._buttons_layout = QVBoxLayout(self._buttons_holder)
        self._buttons_layout.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._buttons_holder)
        outer.addStretch(1)

    def set_context(self, context: repo.PhotoContext | None) -> None:
        # Clear existing buttons.
        while self._buttons_layout.count() > 0:
            item = self._buttons_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        if context is None:
            self.body.setText("(none)")
            return
        parts: list[str] = []
        if context.description_suggestion:
            parts.append(f"Description: {context.description_suggestion}")
        if context.folder_hint:
            parts.append(f"Folder hint: {context.folder_hint}")
        if context.date_suggestions:
            for d in context.date_suggestions:
                text = d.date or (
                    f"{d.year_range[0]}–{d.year_range[1]}"
                    if d.year_range else "(no date)"
                )
                conf = f"conf {d.confidence:.2f}" if d.confidence is not None else ""
                evidence = f" — {d.evidence}" if d.evidence else ""
                parts.append(f"Date: {text} · {d.precision} · {conf}{evidence}")
        if not parts:
            self.body.setText("(no suggestions)")
        else:
            self.body.setText("\n".join(parts))
        for d in context.date_suggestions:
            btn = QPushButton(
                f"Accept date: {d.date or d.year_range or '(range)'} ({d.precision})"
            )
            sid = d.suggestion_id
            btn.clicked.connect(lambda _=None, s=sid: self.accept_date_requested.emit(s))
            self._buttons_layout.addWidget(btn)


# --- PhotoPreviewWidget ---------------------------------------------------


class PhotoPreviewWidget(QWidget):
    """Preview pane — header, canvas, back panel, suggestions. Owned by
    the FacesPanel."""

    face_clicked = Signal(int, bool)
    face_bbox_changed = Signal(int, dict)
    new_face_requested = Signal(dict)
    face_delete_requested = Signal(int)
    confirm_back_requested = Signal(int)             # photo_back_id
    edit_back_confirmed = Signal(int, str)           # photo_back_id, text
    accept_date_requested = Signal(int)              # suggestion_id
    toggle_front_back_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(4)

        self.header = QLabel("")
        self.header.setStyleSheet("font-weight: bold")
        outer.addWidget(self.header)

        self.canvas = PhotoCanvas(self)
        self.canvas.face_clicked.connect(self.face_clicked)
        self.canvas.face_bbox_changed.connect(self.face_bbox_changed)
        self.canvas.new_face_requested.connect(self.new_face_requested)
        self.canvas.face_delete_requested.connect(self.face_delete_requested)
        outer.addWidget(self.canvas, 1)

        self.caption = QLabel("")
        self.caption.setWordWrap(True)
        self.caption.setStyleSheet("color: #bfbfbf")
        outer.addWidget(self.caption)

        self.back_panel = BackPanel(self)
        self.back_panel.confirm_requested.connect(self.confirm_back_requested)
        self.back_panel.edit_confirmed.connect(self.edit_back_confirmed)
        self.back_panel.toggle_main_pane_requested.connect(
            lambda _bid: self.toggle_front_back_requested.emit()
        )
        outer.addWidget(self.back_panel)
        self.back_panel.setVisible(False)

        self.suggestions = SuggestionsPanel(self)
        self.suggestions.accept_date_requested.connect(self.accept_date_requested)
        outer.addWidget(self.suggestions)
        self.suggestions.setVisible(False)

        self._context: repo.PhotoContext | None = None
        self._current_face_id: int | None = None
        self._showing_back: bool = False

    def show_photo(self, context: repo.PhotoContext, current_face_id: int) -> None:
        self._context = context
        self._current_face_id = current_face_id
        self._showing_back = False
        self.canvas.set_photo(context, current_face_id)
        parts: list[str] = []
        if context.capture_year is not None:
            parts.append(str(context.capture_year))
        if context.scan_batch and context.scan_sequence is not None:
            parts.append(f"{context.scan_batch} #{context.scan_sequence}")
        elif context.source_folder:
            parts.append(context.source_folder)
        parts.append(f"photo {context.photo_id}")
        self.header.setText("  ·  ".join(parts))
        if context.back_transcription:
            snippet = context.back_transcription.strip()
            if len(snippet) > 300:
                snippet = snippet[:300] + "…"
            self.caption.setText(f"back: {snippet}")
        else:
            self.caption.setText("")
        # Back panel: show the first back if any.
        first_back = context.backs[0] if context.backs else None
        self.back_panel.set_back(first_back)
        self.back_panel.setVisible(first_back is not None)
        # Suggestions block.
        has_sugg = (
            bool(context.date_suggestions)
            or bool(context.description_suggestion)
        )
        self.suggestions.set_context(context)
        self.suggestions.setVisible(has_sugg)

    def toggle_front_back(self) -> None:
        """T key: toggle the main pane between the front photo and the
        currently-displayed back image."""
        if self._context is None:
            return
        back = self.back_panel.current_back()
        if back is None:
            return
        if self._showing_back:
            self.canvas.set_photo(self._context, self._current_face_id)
            self._showing_back = False
            self.back_panel.toggle_btn.setText("Show back image (T)")
        else:
            path = Path(back.working_path) if back.working_path else Path(back.master_path)
            self.canvas.show_arbitrary_image(path)
            self._showing_back = True
            self.back_panel.toggle_btn.setText("Show front photo (T)")
