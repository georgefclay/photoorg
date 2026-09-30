"""The dialog for correcting a split by hand (fix-up 5).

A thin skin over `regions.py`, which holds every rule. This file knows about
the mouse and nothing else: where a drag started, which handle it grabbed, and
how to turn view coordinates into the scan's own.

It is a dialog rather than an overlay on the review pane because that pane's
two views already own the mouse for pan and zoom, and a drag that sometimes
pans and sometimes draws a region is the kind of thing that gets clicked
wrong at eleven at night with four hundred scans to go.
"""
from __future__ import annotations

import logging
from typing import Sequence

from PySide6.QtCore import QPoint, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton, QSpinBox,
    QVBoxLayout, QWidget,
)

from ...config import Settings
from .regions import Box, bounds_of, grid_boxes, problems, reading_order

log = logging.getLogger(__name__)

HANDLE_PX = 9
MIN_DRAG_PX = 4
_CORNERS = ("tl", "tr", "bl", "br")


class RegionCanvas(QWidget):
    """The scan, with the regions drawn over it."""

    def __init__(self, pixmap: QPixmap, src_w: int, src_h: int,
                 boxes: Sequence[Box], parent=None) -> None:
        super().__init__(parent)
        self._pix = pixmap
        self._src_w = max(1, int(src_w))
        self._src_h = max(1, int(src_h))
        self.boxes: list[Box] = list(boxes)
        self.selected: int | None = 0 if boxes else None
        self._drag: dict | None = None
        self.setMinimumSize(420, 320)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)

    # -- coordinate mapping ---------------------------------------------

    def _fit(self) -> tuple[float, float, float]:
        """(scale, offset_x, offset_y) mapping scan coords into the widget."""
        aw, ah = max(1, self.width()), max(1, self.height())
        scale = min(aw / self._src_w, ah / self._src_h)
        return scale, (aw - self._src_w * scale) / 2.0, (ah - self._src_h * scale) / 2.0

    def to_view(self, x: float, y: float) -> tuple[float, float]:
        s, ox, oy = self._fit()
        return x * s + ox, y * s + oy

    def to_scan(self, vx: float, vy: float) -> tuple[float, float]:
        s, ox, oy = self._fit()
        return (vx - ox) / s, (vy - oy) / s

    def _view_rect(self, b: Box) -> QRectF:
        x0, y0 = self.to_view(b.x, b.y)
        x1, y1 = self.to_view(b.x + b.w, b.y + b.h)
        return QRectF(x0, y0, x1 - x0, y1 - y0)

    # -- hit testing -----------------------------------------------------

    def _corner_at(self, pos: QPoint) -> tuple[int, str] | None:
        for i, b in enumerate(self.boxes):
            r = self._view_rect(b)
            for name, pt in (("tl", r.topLeft()), ("tr", r.topRight()),
                             ("bl", r.bottomLeft()), ("br", r.bottomRight())):
                if (abs(pos.x() - pt.x()) <= HANDLE_PX
                        and abs(pos.y() - pt.y()) <= HANDLE_PX):
                    return i, name
        return None

    def _box_at(self, pos: QPoint) -> int | None:
        # Last drawn is on top, so search backwards.
        for i in range(len(self.boxes) - 1, -1, -1):
            if self._view_rect(self.boxes[i]).contains(pos):
                return i
        return None

    # -- painting --------------------------------------------------------

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(24, 24, 28))
        s, ox, oy = self._fit()
        target = QRect(int(ox), int(oy),
                       int(self._src_w * s), int(self._src_h * s))
        if not self._pix.isNull():
            p.drawPixmap(target, self._pix)

        font = QFont()
        font.setBold(True)
        p.setFont(font)
        for i, b in enumerate(self.boxes):
            r = self._view_rect(b)
            on = (i == self.selected)
            colour = QColor(120, 220, 130) if on else QColor(90, 170, 255)
            p.setPen(QPen(colour, 3 if on else 2))
            p.drawRect(r)
            p.drawText(r.adjusted(6, 4, -6, -6), Qt.AlignLeft | Qt.AlignTop,
                       str(i + 1))
            if on:
                p.setBrush(colour)
                for pt in (r.topLeft(), r.topRight(),
                           r.bottomLeft(), r.bottomRight()):
                    p.drawRect(QRectF(pt.x() - HANDLE_PX / 2,
                                      pt.y() - HANDLE_PX / 2,
                                      HANDLE_PX, HANDLE_PX))
                p.setBrush(Qt.NoBrush)

        # The region currently being dragged out, before it is committed.
        if self._drag and self._drag.get("preview") is not None:
            p.setPen(QPen(QColor(255, 220, 120), 2, Qt.DashLine))
            p.drawRect(self._view_rect(self._drag["preview"]))
        p.end()

    # -- mouse -----------------------------------------------------------

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.LeftButton:
            return
        pos = event.position().toPoint()
        hit = self._corner_at(pos)
        if hit is not None:
            i, corner = hit
            self.selected = i
            self._drag = {"kind": "resize", "index": i, "corner": corner}
        else:
            i = self._box_at(pos)
            if i is not None:
                self.selected = i
                sx, sy = self.to_scan(pos.x(), pos.y())
                self._drag = {"kind": "move", "index": i,
                              "dx": sx - self.boxes[i].x,
                              "dy": sy - self.boxes[i].y}
            else:
                sx, sy = self.to_scan(pos.x(), pos.y())
                self._drag = {"kind": "new", "x0": sx, "y0": sy,
                              "start": pos}
        self.update()

    def mouseMoveEvent(self, event) -> None:
        if not self._drag:
            return
        pos = event.position().toPoint()
        sx, sy = self.to_scan(pos.x(), pos.y())
        d = self._drag
        if d["kind"] == "move":
            b = self.boxes[d["index"]]
            self.boxes[d["index"]] = Box(
                x=sx - d["dx"], y=sy - d["dy"], w=b.w, h=b.h, angle=b.angle
            ).clamped(self._src_w, self._src_h)
        elif d["kind"] == "resize":
            self.boxes[d["index"]] = self._resized(
                self.boxes[d["index"]], d["corner"], sx, sy)
        elif d["kind"] == "new":
            if (abs(pos.x() - d["start"].x()) >= MIN_DRAG_PX
                    or abs(pos.y() - d["start"].y()) >= MIN_DRAG_PX):
                d["preview"] = self._from_drag(d["x0"], d["y0"], sx, sy)
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        d, self._drag = self._drag, None
        if d and d["kind"] == "new" and "preview" in d:
            self.boxes.append(d["preview"].clamped(self._src_w, self._src_h))
            self.selected = len(self.boxes) - 1
        self.update()
        self.parent_changed()

    def _from_drag(self, x0: float, y0: float, x1: float, y1: float) -> Box:
        return Box(x=min(x0, x1), y=min(y0, y1),
                   w=abs(x1 - x0), h=abs(y1 - y0))

    def _resized(self, b: Box, corner: str, sx: float, sy: float) -> Box:
        left, top = b.x, b.y
        right, bottom = b.x + b.w, b.y + b.h
        if corner in ("tl", "bl"):
            left = sx
        else:
            right = sx
        if corner in ("tl", "tr"):
            top = sy
        else:
            bottom = sy
        return Box(x=min(left, right), y=min(top, bottom),
                   w=max(1.0, abs(right - left)),
                   h=max(1.0, abs(bottom - top)),
                   angle=b.angle).clamped(self._src_w, self._src_h)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.delete_selected()
        else:
            super().keyPressEvent(event)

    def delete_selected(self) -> None:
        if self.selected is None or not self.boxes:
            return
        del self.boxes[self.selected]
        self.selected = min(self.selected, len(self.boxes) - 1) if self.boxes else None
        self.update()
        self.parent_changed()

    def set_boxes(self, boxes: Sequence[Box]) -> None:
        self.boxes = list(boxes)
        self.selected = 0 if self.boxes else None
        self.update()
        self.parent_changed()

    # Set by the dialog; a plain attribute rather than a signal so the canvas
    # stays usable on its own in a test.
    def parent_changed(self) -> None:
        pass


class RegionEditorDialog(QDialog):
    """Correct a split: move, resize, add and delete regions."""

    def __init__(self, pixmap: QPixmap, *, src_w: int, src_h: int,
                 boxes: Sequence[Box], settings: Settings, photo_id: int,
                 parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Edit split regions — photo #{photo_id}")
        self.resize(1000, 760)
        self._settings = settings
        self._src_w, self._src_h = src_w, src_h

        self.canvas = RegionCanvas(pixmap, src_w, src_h, boxes, self)
        self.canvas.parent_changed = self._revalidate

        self._rows = QSpinBox()
        self._rows.setRange(1, 12)
        self._rows.setValue(3)
        self._cols = QSpinBox()
        self._cols.setRange(1, 12)
        self._cols.setValue(4)
        grid_btn = QPushButton("Lay out grid")
        grid_btn.setToolTip(
            "Replace the regions with an even grid over the area they cover, "
            "then nudge the ones that need it. For a proof sheet whose prints "
            "touch, this is the whole job."
        )
        grid_btn.clicked.connect(self._lay_out_grid)
        del_btn = QPushButton("Delete region (Del)")
        del_btn.clicked.connect(self.canvas.delete_selected)

        tools = QHBoxLayout()
        tools.addWidget(QLabel("Rows:"))
        tools.addWidget(self._rows)
        tools.addWidget(QLabel("Columns:"))
        tools.addWidget(self._cols)
        tools.addWidget(grid_btn)
        tools.addSpacing(18)
        tools.addWidget(del_btn)
        tools.addStretch(1)

        self._hint = QLabel(
            "Drag a region to move it, a corner to resize, or drag on empty "
            "bed to add one. Del removes the selected region."
        )
        self._hint.setWordWrap(True)
        self._problems = QLabel("")
        self._problems.setWordWrap(True)
        self._problems.setStyleSheet("color: #ff9a9a;")

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(tools)
        layout.addWidget(self.canvas, stretch=1)
        layout.addWidget(self._hint)
        layout.addWidget(self._problems)
        layout.addWidget(self._buttons)
        self._revalidate()

    # -- behaviour -------------------------------------------------------

    def _lay_out_grid(self) -> None:
        area = bounds_of(self.canvas.boxes)
        if area is None:
            area = Box(x=0.0, y=0.0, w=float(self._src_w), h=float(self._src_h))
        self.canvas.set_boxes(
            grid_boxes(area, self._rows.value(), self._cols.value()))

    def _revalidate(self) -> None:
        issues = problems(self.canvas.boxes, src_w=self._src_w,
                          src_h=self._src_h)
        self._problems.setText("  ".join(issues))
        save = self._buttons.button(QDialogButtonBox.Save)
        if save is not None:
            save.setEnabled(not issues)

    def result_boxes(self) -> list[Box]:
        """The edited regions, numbered the way they sit on the bed."""
        return reading_order(self.canvas.boxes)

    def keyPressEvent(self, event) -> None:
        # Enter must not close the dialog while a region is being dragged
        # about; Save is a deliberate click or Ctrl+Enter.
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and \
                not (event.modifiers() & Qt.ControlModifier):
            return
        super().keyPressEvent(event)
