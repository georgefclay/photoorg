"""Two side-by-side QGraphicsView panes whose zoom and pan stay in sync.

Reusable: Phase 4 Dedupe uses it for keeper vs loser at full resolution;
Phase 7 Cleanup will use it for before/after. The API is simple:

    viewer = SyncedViewer()
    viewer.set_left(QPixmap(...), caption="keeper")
    viewer.set_right(QPixmap(...), caption="loser 1")

Wheel over either pane zooms both around the cursor. Left-drag pans both.
"Fit to window" (F key or fit_to_window()) resets to show each full pixmap.
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QMouseEvent, QPixmap, QWheelEvent
from PySide6.QtWidgets import (
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


_MIN_SCALE = 0.05
_MAX_SCALE = 32.0
_ZOOM_STEP = 1.15


class _SyncedGraphicsView(QGraphicsView):
    """One pane. Forwards wheel and drag events to the owner so the peer
    can mirror them.
    """

    zoom_requested = Signal(float, QPoint)  # (factor, cursor pos in view coords)
    pan_requested = Signal(int, int)        # (dx, dy)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHints(self.renderHints())
        self.setDragMode(QGraphicsView.NoDrag)
        self.setTransformationAnchor(QGraphicsView.NoAnchor)
        self.setResizeAnchor(QGraphicsView.NoAnchor)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QGraphicsView.NoFrame)
        self.setBackgroundBrush(Qt.black)
        self._pan_last: QPoint | None = None
        self._item: QGraphicsPixmapItem | None = None

    def set_pixmap(self, pix: QPixmap | None) -> None:
        self.scene().clear()
        self._item = None
        if pix is None or pix.isNull():
            return
        self._item = self.scene().addPixmap(pix)
        self.setSceneRect(QRectF(pix.rect()))

    def pixmap_size(self) -> tuple[int, int] | None:
        if self._item is None:
            return None
        r = self._item.pixmap().rect()
        return r.width(), r.height()

    def wheelEvent(self, event: QWheelEvent) -> None:
        factor = _ZOOM_STEP if event.angleDelta().y() > 0 else 1 / _ZOOM_STEP
        self.zoom_requested.emit(factor, event.position().toPoint())
        event.accept()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self._pan_last = event.position().toPoint()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._pan_last is not None:
            here = event.position().toPoint()
            dx = here.x() - self._pan_last.x()
            dy = here.y() - self._pan_last.y()
            self._pan_last = here
            self.pan_requested.emit(dx, dy)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton and self._pan_last is not None:
            self._pan_last = None
            self.setCursor(Qt.ArrowCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def apply_zoom(self, factor: float, anchor_view_pos: QPointF | None) -> None:
        """Zoom by factor. If an anchor is given, keep the anchor stationary
        in the scene (like Qt's AnchorUnderMouse but explicit).
        """
        current_scale = self.transform().m11()
        new_scale = max(_MIN_SCALE, min(_MAX_SCALE, current_scale * factor))
        actual = new_scale / current_scale
        if abs(actual - 1.0) < 1e-6:
            return

        if anchor_view_pos is not None:
            scene_before = self.mapToScene(anchor_view_pos.toPoint())
        self.scale(actual, actual)
        if anchor_view_pos is not None:
            scene_after = self.mapToScene(anchor_view_pos.toPoint())
            delta = scene_after - scene_before
            self.translate(delta.x(), delta.y())

    def apply_pan(self, dx: int, dy: int) -> None:
        # Translate scene by view delta (convert pixels to scene units via
        # current scale).
        current_scale = self.transform().m11() or 1.0
        self.translate(dx / current_scale, dy / current_scale)


@dataclass
class _PaneCaption:
    title: str = ""
    subtitle: str = ""


class SyncedViewer(QWidget):
    """Two synchronised graphics panes with captions above each."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._left_caption = QLabel("")
        self._right_caption = QLabel("")
        self._left_caption.setStyleSheet("padding: 2px 4px; color: #ddd; background: #222;")
        self._right_caption.setStyleSheet("padding: 2px 4px; color: #ddd; background: #222;")
        self._left_view = _SyncedGraphicsView()
        self._right_view = _SyncedGraphicsView()

        for v in (self._left_view, self._right_view):
            v.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        left_box = QVBoxLayout()
        left_box.setContentsMargins(0, 0, 0, 0)
        left_box.setSpacing(0)
        left_box.addWidget(self._left_caption)
        left_box.addWidget(self._left_view)

        right_box = QVBoxLayout()
        right_box.setContentsMargins(0, 0, 0, 0)
        right_box.setSpacing(0)
        right_box.addWidget(self._right_caption)
        right_box.addWidget(self._right_view)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)
        outer.addLayout(left_box)
        outer.addLayout(right_box)

        self._left_view.zoom_requested.connect(self._on_zoom)
        self._right_view.zoom_requested.connect(self._on_zoom)
        self._left_view.pan_requested.connect(self._on_pan)
        self._right_view.pan_requested.connect(self._on_pan)

    def set_left(self, pix: QPixmap | None, caption: str = "") -> None:
        self._left_view.set_pixmap(pix)
        self._left_caption.setText(caption)

    def set_right(self, pix: QPixmap | None, caption: str = "") -> None:
        self._right_view.set_pixmap(pix)
        self._right_caption.setText(caption)

    def fit_to_window(self) -> None:
        for v in (self._left_view, self._right_view):
            if v._item is not None:
                v.resetTransform()
                v.fitInView(v.sceneRect(), Qt.KeepAspectRatio)

    def _on_zoom(self, factor: float, anchor: QPoint) -> None:
        sender = self.sender()
        anchor_left = anchor if sender is self._left_view else self._map_across(
            anchor, self._right_view, self._left_view,
        )
        anchor_right = anchor if sender is self._right_view else self._map_across(
            anchor, self._left_view, self._right_view,
        )
        self._left_view.apply_zoom(factor, QPointF(anchor_left))
        self._right_view.apply_zoom(factor, QPointF(anchor_right))

    def _on_pan(self, dx: int, dy: int) -> None:
        self._left_view.apply_pan(dx, dy)
        self._right_view.apply_pan(dx, dy)

    @staticmethod
    def _map_across(
        pt: QPoint, from_view: _SyncedGraphicsView, to_view: _SyncedGraphicsView,
    ) -> QPoint:
        # Approximate: if the two views have similar sizes, mirror the
        # point at the equivalent viewport position. Zoom anchors are a
        # hint; exact cross-view mapping isn't important for the UX.
        w_from = max(1, from_view.viewport().width())
        h_from = max(1, from_view.viewport().height())
        w_to = max(1, to_view.viewport().width())
        h_to = max(1, to_view.viewport().height())
        return QPoint(int(pt.x() * w_to / w_from), int(pt.y() * h_to / h_from))

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.ShowToParent or event.type() == QEvent.Resize:
            # If nothing is loaded yet, fit_to_window is a no-op.
            self.fit_to_window()
        return super().event(event)
