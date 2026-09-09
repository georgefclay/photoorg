"""Full-photo preview for the Faces mode (Phase 6 fix-up 5).

Space (or double-click) on a face tile opens this pane. It shows the
photo fitted to the pane, the current face outlined in yellow, every
other detected face outlined in white, labelled with its person name
where known. A caption strip below carries year / batch#sequence /
folder and the back transcription if the photo has one. Arrow keys
step to prev / next face within the cluster (driven by the parent).
Esc closes.

Hold Space (peek): the parent tracks Space press time and hides the
pane when a press was held > PEEK_HOLD_MS on release.

Clicking on any face box in the preview emits `face_clicked(face_id,
is_labelled)` so the parent can jump to that face's cluster (if
unlabelled) or open that person (if labelled).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps
from PySide6.QtCore import QPoint, QRect, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QImage,
    QMouseEvent,
    QPainter,
    QPen,
    QPixmap,
    QResizeEvent,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from . import repo

log = logging.getLogger(__name__)


PEEK_HOLD_MS = 300  # Space press-and-release faster than this stays "locked" open


@dataclass
class _Overlay:
    """Rect on the widget canvas + a payload for click routing."""
    canvas_rect: QRectF
    face_id: int
    person_id: int | None
    person_name: str | None
    is_current: bool
    is_disputed: bool


class PhotoCanvas(QWidget):
    """Paints the full photo and its face overlays; emits face clicks."""

    face_clicked = Signal(int, bool)  # (face_id, is_labelled)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)
        self._image: QImage | None = None
        self._image_w: int = 0
        self._image_h: int = 0
        self._context: repo.PhotoContext | None = None
        self._current_face_id: int | None = None
        self._overlays: list[_Overlay] = []
        self._load_error: str | None = None

    def set_photo(
        self,
        context: repo.PhotoContext,
        current_face_id: int | None,
    ) -> None:
        self._context = context
        self._current_face_id = current_face_id
        self._image = None
        self._load_error: str | None = None
        if context.working_path:
            path = Path(context.working_path)
            abs_path = str(path.resolve()) if path.exists() else str(path)
            if not path.exists():
                # Fix-up 7: never report "missing" without the exact
                # path we tried, so the log dock (and the banner) makes
                # `tools/check_working_files.py` an obvious next step.
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
                        if im.mode not in ("RGB", "L"):
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
        # If we couldn't read pixel dims from the file, fall back to the DB row.
        if self._image is None:
            self._image_w = context.width or 1
            self._image_h = context.height or 1
        self._overlays.clear()
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 — Qt override
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), QColor("#101010"))
        if self._context is None or self._image_w == 0:
            p.setPen(QPen(QColor("#888")))
            p.drawText(self.rect(), Qt.AlignCenter, "No preview.")
            return

        # Fit-in-widget geometry
        widget_w = self.width()
        widget_h = self.height()
        scale = min(widget_w / self._image_w, widget_h / self._image_h)
        draw_w = int(self._image_w * scale)
        draw_h = int(self._image_h * scale)
        ox = (widget_w - draw_w) // 2
        oy = (widget_h - draw_h) // 2

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

        # Overlays
        self._overlays.clear()
        label_font = QFont()
        label_font.setPointSize(9)
        p.setFont(label_font)
        fm = QFontMetrics(label_font)
        for face in self._context.faces:
            bbox = face.bbox
            fx = float(bbox.get("x", 0)) * scale + ox
            fy = float(bbox.get("y", 0)) * scale + oy
            fw = float(bbox.get("w", 0)) * scale
            fh = float(bbox.get("h", 0)) * scale
            rect = QRectF(fx, fy, fw, fh)
            is_current = (face.face_id == self._current_face_id)
            # Yellow for the current face, red for disputed, white for others.
            if is_current:
                colour = QColor("#ffd94d")
            elif face.is_disputed:
                colour = QColor("#e35555")
            else:
                colour = QColor("#ffffff")
            pen = QPen(colour)
            pen.setWidth(3 if is_current else 2)
            p.setPen(pen)
            p.drawRect(rect)

            # Label: person name if known, else "unknown".
            label = face.person_name or ("unknown" if face.person_id is None else f"person {face.person_id}")
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

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton:
            return
        pos = event.position() if hasattr(event, "position") else event.localPos()
        pt = QPoint(int(pos.x()), int(pos.y()))
        for ov in self._overlays:
            if ov.canvas_rect.contains(pt):
                self.face_clicked.emit(
                    ov.face_id, ov.person_id is not None
                )
                return


class PhotoPreviewWidget(QWidget):
    """Preview pane — header, canvas, caption. Owned by the FacesPanel."""

    face_clicked = Signal(int, bool)  # (face_id, is_labelled)

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
        outer.addWidget(self.canvas, 1)

        self.caption = QLabel("")
        self.caption.setWordWrap(True)
        self.caption.setStyleSheet("color: #bfbfbf")
        outer.addWidget(self.caption)

    def show_photo(self, context: repo.PhotoContext, current_face_id: int) -> None:
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
            if len(snippet) > 500:
                snippet = snippet[:500] + "…"
            self.caption.setText(f"back: {snippet}")
        else:
            self.caption.setText("")
