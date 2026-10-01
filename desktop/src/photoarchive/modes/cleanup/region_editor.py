"""The dialog for splitting a scan by hand (fix-ups 5 and 6).

A thin skin over `regions.py`, which holds every rule. This file knows about
the mouse and nothing else: where a drag started, which handle it grabbed, and
how to turn view coordinates into the scan's own.

It is a dialog rather than an overlay on the review pane because that pane's
two views already own the mouse for pan and zoom, and a drag that sometimes
pans and sometimes draws a region is the kind of thing that gets clicked
wrong at eleven at night with four hundred scans to go.

The grid has its own **frame** — the dashed rectangle the cells are divided
out of. It starts as the sheet (the outer bounds of every print the analyser
found) and is dragged like anything else. Laying the grid over the *regions*
instead is what made the boxes drift on #3817: the two measured regions were
narrower than the sheet, so every cell came out narrow and the error piled up
across the columns until the rightmost box sat well left of its print.
"""
from __future__ import annotations

import logging
from typing import Sequence

from PySide6.QtCore import QPoint, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QDoubleSpinBox, QHBoxLayout, QLabel,
    QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from ...config import Settings
from .regions import (
    Box, bounds_of, grid_boxes, moved, problems, reading_order, same_column,
    same_row,
)

log = logging.getLogger(__name__)

HANDLE_PX = 9
FRAME_EDGE_PX = 7
MIN_DRAG_PX = 4
NUDGE_PX = 2.0
NUDGE_FAST_PX = 10.0


class RegionCanvas(QWidget):
    """The scan, the grid frame, and the regions drawn over it."""

    def __init__(self, pixmap: QPixmap, src_w: int, src_h: int,
                 boxes: Sequence[Box], frame: Box | None = None,
                 parent=None) -> None:
        super().__init__(parent)
        self._pix = pixmap
        self._src_w = max(1, int(src_w))
        self._src_h = max(1, int(src_h))
        self.boxes: list[Box] = list(boxes)
        self.frame: Box = frame or bounds_of(boxes) or Box(
            0.0, 0.0, float(src_w), float(src_h))
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
    #
    # Priority, outermost first: a region's corner, a region's body, the
    # frame's corner, the frame's edge, then empty space (which draws a new
    # region). The frame is grabbed by its border rather than its interior,
    # so the inside of the frame is still free for drawing regions.

    @staticmethod
    def _corners(r: QRectF):
        return (("tl", r.topLeft()), ("tr", r.topRight()),
                ("bl", r.bottomLeft()), ("br", r.bottomRight()))

    def _corner_at(self, pos: QPoint) -> tuple[int, str] | None:
        for i, b in enumerate(self.boxes):
            for name, pt in self._corners(self._view_rect(b)):
                if (abs(pos.x() - pt.x()) <= HANDLE_PX
                        and abs(pos.y() - pt.y()) <= HANDLE_PX):
                    return i, name
        return None

    def _frame_corner_at(self, pos: QPoint) -> str | None:
        for name, pt in self._corners(self._view_rect(self.frame)):
            if (abs(pos.x() - pt.x()) <= HANDLE_PX
                    and abs(pos.y() - pt.y()) <= HANDLE_PX):
                return name
        return None

    def _on_frame_edge(self, pos: QPoint) -> bool:
        r = self._view_rect(self.frame)
        outer = r.adjusted(-FRAME_EDGE_PX, -FRAME_EDGE_PX,
                           FRAME_EDGE_PX, FRAME_EDGE_PX)
        inner = r.adjusted(FRAME_EDGE_PX, FRAME_EDGE_PX,
                           -FRAME_EDGE_PX, -FRAME_EDGE_PX)
        return outer.contains(pos) and not inner.contains(pos)

    def _box_at(self, pos: QPoint) -> int | None:
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

        # The grid frame, behind everything else.
        p.setPen(QPen(QColor(255, 190, 90), 2, Qt.DashLine))
        fr = self._view_rect(self.frame)
        p.drawRect(fr)
        p.setBrush(QColor(255, 190, 90))
        for _name, pt in self._corners(fr):
            p.drawRect(QRectF(pt.x() - HANDLE_PX / 2, pt.y() - HANDLE_PX / 2,
                              HANDLE_PX, HANDLE_PX))
        p.setBrush(Qt.NoBrush)

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
                for _name, pt in self._corners(r):
                    p.drawRect(QRectF(pt.x() - HANDLE_PX / 2,
                                      pt.y() - HANDLE_PX / 2,
                                      HANDLE_PX, HANDLE_PX))
                p.setBrush(Qt.NoBrush)

        if self._drag and self._drag.get("preview") is not None:
            p.setPen(QPen(QColor(255, 220, 120), 2, Qt.DashLine))
            p.drawRect(self._view_rect(self._drag["preview"]))
        p.end()

    # -- mouse -----------------------------------------------------------

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.LeftButton:
            return
        pos = event.position().toPoint()
        sx, sy = self.to_scan(pos.x(), pos.y())

        hit = self._corner_at(pos)
        if hit is not None:
            i, corner = hit
            self.selected = i
            self._drag = {"kind": "resize", "index": i, "corner": corner}
            self.update()
            return

        i = self._box_at(pos)
        if i is not None:
            self.selected = i
            self._drag = {"kind": "move", "index": i,
                          "dx": sx - self.boxes[i].x,
                          "dy": sy - self.boxes[i].y}
            self.update()
            return

        fc = self._frame_corner_at(pos)
        if fc is not None:
            self._drag = {"kind": "frame_resize", "corner": fc}
            self.update()
            return

        if self._on_frame_edge(pos):
            self._drag = {"kind": "frame_move",
                          "dx": sx - self.frame.x, "dy": sy - self.frame.y}
            self.update()
            return

        self._drag = {"kind": "new", "x0": sx, "y0": sy, "start": pos}
        self.update()

    def mouseMoveEvent(self, event) -> None:
        if not self._drag:
            return
        pos = event.position().toPoint()
        sx, sy = self.to_scan(pos.x(), pos.y())
        d = self._drag
        kind = d["kind"]
        if kind == "move":
            b = self.boxes[d["index"]]
            self.boxes[d["index"]] = Box(
                x=sx - d["dx"], y=sy - d["dy"], w=b.w, h=b.h, angle=b.angle
            ).clamped(self._src_w, self._src_h)
        elif kind == "resize":
            self.boxes[d["index"]] = self._resized(
                self.boxes[d["index"]], d["corner"], sx, sy)
        elif kind == "frame_move":
            self.frame = Box(
                x=sx - d["dx"], y=sy - d["dy"], w=self.frame.w,
                h=self.frame.h, angle=self.frame.angle
            ).clamped(self._src_w, self._src_h)
        elif kind == "frame_resize":
            self.frame = self._resized(self.frame, d["corner"], sx, sy)
        elif kind == "new":
            if (abs(pos.x() - d["start"].x()) >= MIN_DRAG_PX
                    or abs(pos.y() - d["start"].y()) >= MIN_DRAG_PX):
                d["preview"] = Box(x=min(d["x0"], sx), y=min(d["y0"], sy),
                                   w=abs(sx - d["x0"]), h=abs(sy - d["y0"]))
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        d, self._drag = self._drag, None
        if d and d["kind"] == "new" and "preview" in d:
            self.boxes.append(d["preview"].clamped(self._src_w, self._src_h))
            self.selected = len(self.boxes) - 1
        self.update()
        self.changed()

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

    # -- keyboard --------------------------------------------------------

    _ARROWS = {Qt.Key_Left: (-1.0, 0.0), Qt.Key_Right: (1.0, 0.0),
               Qt.Key_Up: (0.0, -1.0), Qt.Key_Down: (0.0, 1.0)}

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key in (Qt.Key_Delete, Qt.Key_Backspace):
            self.delete_selected()
            return
        step = self._ARROWS.get(key)
        if step is None or self.selected is None or not self.boxes:
            super().keyPressEvent(event)
            return
        mods = event.modifiers()
        dist = NUDGE_FAST_PX if mods & Qt.ShiftModifier else NUDGE_PX
        if mods & Qt.ControlModifier:
            targets = same_column(self.boxes, self.selected)
        elif mods & Qt.AltModifier:
            targets = same_row(self.boxes, self.selected)
        else:
            targets = [self.selected]
        self.nudge(targets, step[0] * dist, step[1] * dist)

    def nudge(self, indices: Sequence[int], dx: float, dy: float) -> None:
        """Move these regions together. A sheet that is slightly skew is
        fixed by shifting a whole column, not by dragging four boxes."""
        for i in indices:
            self.boxes[i] = moved(self.boxes[i], dx, dy).clamped(
                self._src_w, self._src_h)
        self.update()
        self.changed()

    # -- edits -----------------------------------------------------------

    def delete_selected(self) -> None:
        if self.selected is None or not self.boxes:
            return
        del self.boxes[self.selected]
        self.selected = min(self.selected, len(self.boxes) - 1) if self.boxes else None
        self.update()
        self.changed()

    def set_boxes(self, boxes: Sequence[Box]) -> None:
        self.boxes = list(boxes)
        self.selected = 0 if self.boxes else None
        self.update()
        self.changed()

    def set_frame(self, frame: Box) -> None:
        self.frame = frame.clamped(self._src_w, self._src_h)
        self.update()

    #: Set by the dialog; a plain attribute rather than a signal so the canvas
    #: stays usable on its own in a test.
    def changed(self) -> None:
        pass


class RegionEditorDialog(QDialog):
    """Split a scan by hand: move, resize, add and delete regions, or lay a
    grid over the sheet and nudge it."""

    def __init__(self, pixmap: QPixmap, *, src_w: int, src_h: int,
                 boxes: Sequence[Box], settings: Settings, photo_id: int,
                 frame: Box | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Split regions — photo #{photo_id}")
        self.resize(1000, 800)
        self._settings = settings
        self._src_w, self._src_h = src_w, src_h

        self.canvas = RegionCanvas(pixmap, src_w, src_h, boxes, frame, self)
        self.canvas.changed = self._revalidate

        self._rows = QSpinBox()
        self._rows.setRange(1, 12)
        self._rows.setValue(3)
        self._cols = QSpinBox()
        self._cols.setRange(1, 12)
        self._cols.setValue(4)
        self._gutter = QDoubleSpinBox()
        self._gutter.setRange(0.0, 40.0)
        self._gutter.setSingleStep(1.0)
        self._gutter.setValue(0.0)
        self._gutter.setSuffix(" %")
        self._gutter.setToolTip(
            "Shrink every cell about its centre, for a sheet with white space "
            "between the pictures. 0 % means the cells touch."
        )
        grid_btn = QPushButton("Lay out grid")
        grid_btn.setToolTip(
            "Fill the dashed frame with rows x columns. Drag the frame to the "
            "sheet first; press again after moving it to re-lay the cells."
        )
        grid_btn.clicked.connect(self._lay_out_grid)
        reset_btn = QPushButton("Frame = whole scan")
        reset_btn.clicked.connect(
            lambda: self.canvas.set_frame(
                Box(0.0, 0.0, float(src_w), float(src_h))))
        del_btn = QPushButton("Delete region (Del)")
        del_btn.clicked.connect(self.canvas.delete_selected)

        tools = QHBoxLayout()
        tools.addWidget(QLabel("Rows:"))
        tools.addWidget(self._rows)
        tools.addWidget(QLabel("Columns:"))
        tools.addWidget(self._cols)
        tools.addWidget(QLabel("Gutter:"))
        tools.addWidget(self._gutter)
        tools.addWidget(grid_btn)
        tools.addWidget(reset_btn)
        tools.addSpacing(18)
        tools.addWidget(del_btn)
        tools.addStretch(1)

        self._hint = QLabel(
            "Drag the dashed frame (its edge or a corner) onto the sheet, set "
            "rows and columns, then Lay out grid. Drag a region to move it, a "
            "corner to resize, or drag inside the frame to add one. "
            "Arrows nudge 2 px, Shift 10 px; Ctrl+arrows move the whole "
            "column, Alt+arrows the whole row. Del removes a region."
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
        self.canvas.set_boxes(grid_boxes(
            self.canvas.frame, self._rows.value(), self._cols.value(),
            gutter_pct=self._gutter.value() / 100.0))

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
        # Enter must not close the dialog while regions are being moved
        # about; Save is a deliberate click or Ctrl+Enter. Arrows belong to
        # the canvas, which nudges with them.
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and \
                not (event.modifiers() & Qt.ControlModifier):
            return
        if event.key() in RegionCanvas._ARROWS:
            self.canvas.keyPressEvent(event)
            return
        super().keyPressEvent(event)
