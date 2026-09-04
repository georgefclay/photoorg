"""Triage mode UI. Keyboard-first cull pass over 13k+ photos.

Grid view (default): virtualised QListView in IconMode, 6–8 thumbs across.
Single view (Enter): image fitted to pane, EXIF strip, hint badge, position.

Keys in both views:
    K   keep       J   junk       P   private
    U   undo last                 Space toggle selection
    Enter open/close single view  Esc go back to grid
    1-5 jump to hint filters      / focus the filter box
    Ctrl+A select all in filter   Arrows/PgUp/PgDn move cursor

Decisions run in a worker; UI advances optimistically. If the commit fails
we log and revert both the model and the undo stack.
"""
from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable

from PySide6.QtCore import (
    QAbstractListModel, QModelIndex, QObject, QSize, Qt, QThreadPool,
    QRunnable, Signal, Slot,
)
from PySide6.QtGui import (
    QAction, QImage, QKeyEvent, QKeySequence, QPixmap, QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFileDialog, QFrame, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QListView, QMessageBox, QProgressBar,
    QPushButton, QSizePolicy, QSpacerItem, QSplitter, QStackedWidget,
    QToolButton, QVBoxLayout, QWidget,
)

from ... import db
from ...config import Settings, load as load_config
from ...workers import BackgroundJob
from ..ingest.paths import thumb_path
from . import decisions, presort

log = logging.getLogger(__name__)


THUMB_TILE = 176   # px; ~7 across in a 1400-wide window
FONT_MONO = "Consolas, Menlo, monospace"


# ---------------------------------------------------------------------------
# Query model
# ---------------------------------------------------------------------------

STATUS_CHOICES = [
    ("untriaged", "Untriaged"),
    ("keep", "Keep"),
    ("junk", "Junk"),
    ("private", "Private"),
    ("all", "All (except junk)"),
]

HINT_CHOICES = [
    ("all", "any hint"),
    ("photo", "photo"),
    ("screenshot", "screenshot"),
    ("document", "document"),
    ("blank_or_dark", "blank/dark"),
    ("tiny", "tiny"),
    ("burst", "burst extra"),
    ("exact_dup_of", "exact dup"),
    ("no_hint", "(no hint row)"),
]

SORT_CHOICES = [
    ("id", "sequence (id)"),
    ("capture", "capture date"),
    ("hint", "hint"),
]


@dataclass(frozen=True)
class TriageQuery:
    status: str = "untriaged"
    hint: str = "all"
    source_root: str = "all"
    folder: str = "all"
    year: str = "all"
    sort: str = "id"

    def where_and_params(self) -> tuple[str, list[Any]]:
        where: list[str] = ["not p.is_deleted or p.triage_status = 'junk'"]
        params: list[Any] = []
        if self.status == "all":
            where.append("p.triage_status <> 'junk'")
        elif self.status == "junk":
            where = ["p.triage_status = 'junk'"]
        else:
            where.append("p.triage_status = %s")
            where.append("not p.is_deleted")
            params.append(self.status)
        if self.hint == "no_hint":
            where.append("h.photo_id is null")
        elif self.hint != "all":
            where.append("h.hint = %s")
            params.append(self.hint)
        if self.source_root != "all":
            where.append("p.source_root = %s")
            params.append(self.source_root)
        if self.folder != "all":
            where.append("p.source_folder = %s")
            params.append(self.folder)
        if self.year != "all":
            where.append("""(
                (p.capture_date is not null and extract(year from p.capture_date)::int = %s)
                or (p.source_folder like %s)
            )""")
            params.append(int(self.year))
            params.append(f"_{int(self.year):04d}-%")
        return " and ".join(f"({w})" for w in where), params

    def order_by(self) -> str:
        if self.sort == "capture":
            return "coalesce(p.capture_date, '1900-01-01'), p.id"
        if self.sort == "hint":
            return "coalesce(h.hint, 'photo'), p.id"
        return "p.id"


# ---------------------------------------------------------------------------
# Row records
# ---------------------------------------------------------------------------

@dataclass
class TriageRow:
    photo_id: int
    triage_status: str
    is_private: bool
    hint: str
    hint_confidence: float
    hint_details: dict
    source_root: str
    source_folder: str
    source_filename: str
    scan_batch: str | None
    scan_sequence: int | None
    capture_date: str | None
    exif_camera: str | None
    width: int | None
    height: int | None
    year_from: str | None


def _fetch_rows(query: TriageQuery) -> list[TriageRow]:
    where, params = query.where_and_params()
    order = query.order_by()
    sql = rf"""
        select p.id, p.triage_status, p.is_private,
               coalesce(h.hint, 'photo'), coalesce(h.confidence, 0.0),
               coalesce(h.details, '{{}}'::jsonb),
               p.source_root, p.source_folder, p.source_filename,
               p.scan_batch, p.scan_sequence,
               to_char(p.capture_date, 'YYYY-MM-DD'),
               p.exif_camera, p.width, p.height,
               case when p.capture_date is not null then 'exif'
                    when p.source_folder like '\_%%' then 'folder'
                    else null end
        from photos p
        left join triage_hints h on h.photo_id = p.id
        where {where}
        order by {order}
    """
    with db.connection() as conn:
        conn.autocommit = True
        rows = conn.execute(sql, params).fetchall()
    return [TriageRow(
        photo_id=r[0], triage_status=r[1], is_private=bool(r[2]),
        hint=r[3], hint_confidence=float(r[4]),
        hint_details=r[5] if isinstance(r[5], dict) else {},
        source_root=r[6], source_folder=r[7], source_filename=r[8],
        scan_batch=r[9], scan_sequence=r[10], capture_date=r[11],
        exif_camera=r[12], width=r[13], height=r[14], year_from=r[15],
    ) for r in rows]


def _fetch_filter_options() -> dict[str, list[Any]]:
    with db.connection() as conn:
        conn.autocommit = True
        roots = [r[0] for r in conn.execute(
            "select distinct source_root from photos where not is_deleted "
            "order by source_root"
        ).fetchall()]
        folders = [r[0] for r in conn.execute(
            "select distinct source_folder from photos where not is_deleted "
            "and source_folder <> '' order by source_folder"
        ).fetchall()]
        years = [str(int(r[0])) for r in conn.execute(
            """
            select distinct y from (
              select extract(year from capture_date)::int as y
              from photos where capture_date is not null and not is_deleted
              union
              select substring(source_folder from '_(\\d{4})')::int as y
              from photos where source_folder ~ '^_\\d{4}-\\d{2}$'
                and not is_deleted
            ) t where y between 1900 and 2100 order by y
            """
        ).fetchall() if r[0] is not None]
    return {"roots": roots, "folders": folders, "years": years}


# ---------------------------------------------------------------------------
# Thumbnail cache
# ---------------------------------------------------------------------------

class ThumbCache(QObject):
    ready = Signal(int)

    def __init__(self, settings: Settings, size: int = THUMB_TILE) -> None:
        super().__init__()
        self._settings = settings
        self._size = size
        self._cache: dict[int, QPixmap] = {}
        self._pending: set[int] = set()
        self._pool = QThreadPool.globalInstance()

    def get(self, photo_id: int) -> QPixmap | None:
        return self._cache.get(photo_id)

    def request(self, photo_id: int) -> None:
        if photo_id in self._cache or photo_id in self._pending:
            return
        self._pending.add(photo_id)
        runnable = _ThumbLoad(
            photo_id, thumb_path(self._settings, photo_id), self._size,
        )
        runnable.signals.done.connect(self._on_done)
        self._pool.start(runnable)

    @Slot(int, QImage)
    def _on_done(self, photo_id: int, image: QImage) -> None:
        self._pending.discard(photo_id)
        if not image.isNull():
            self._cache[photo_id] = QPixmap.fromImage(image)
            self.ready.emit(photo_id)

    def clear(self) -> None:
        self._cache.clear()
        self._pending.clear()


class _ThumbLoadSignals(QObject):
    done = Signal(int, QImage)


class _ThumbLoad(QRunnable):
    def __init__(self, photo_id: int, path: Path, size: int) -> None:
        super().__init__()
        self.signals = _ThumbLoadSignals()
        self._id = photo_id
        self._path = path
        self._size = size

    def run(self) -> None:
        img = QImage()
        if self._path.exists():
            if img.load(str(self._path)):
                img = img.scaled(
                    self._size, self._size,
                    Qt.KeepAspectRatio, Qt.SmoothTransformation,
                )
        self.signals.done.emit(self._id, img)


# ---------------------------------------------------------------------------
# Grid model
# ---------------------------------------------------------------------------

STATUS_ROLE = Qt.UserRole + 1
HINT_ROLE = Qt.UserRole + 2
PHOTO_ID_ROLE = Qt.UserRole + 3


class TriageGridModel(QAbstractListModel):
    def __init__(self, cache: ThumbCache, parent=None) -> None:
        super().__init__(parent)
        self._rows: list[TriageRow] = []
        self._cache = cache
        self._cache.ready.connect(self._on_thumb_ready)
        self._placeholder = _placeholder_pixmap(THUMB_TILE)

    def set_rows(self, rows: list[TriageRow]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def rows(self) -> list[TriageRow]:
        return self._rows

    def row_by_photo_id(self, photo_id: int) -> int:
        for i, r in enumerate(self._rows):
            if r.photo_id == photo_id:
                return i
        return -1

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        r = self._rows[index.row()]
        if role == Qt.DecorationRole:
            pm = self._cache.get(r.photo_id)
            if pm is None:
                self._cache.request(r.photo_id)
                return self._placeholder
            return pm
        if role == Qt.DisplayRole:
            tag = ""
            if r.triage_status == "keep":     tag = "K "
            elif r.triage_status == "junk":    tag = "J "
            elif r.triage_status == "private": tag = "P "
            hint = "" if r.hint == "photo" else f"·{r.hint}"
            return f"{tag}{r.photo_id}{hint}"
        if role == Qt.ToolTipRole:
            bits = [
                f"#{r.photo_id}  {r.triage_status}"
                + (" (private)" if r.is_private else ""),
                f"{r.source_root}/{r.source_folder}/{r.source_filename}",
            ]
            if r.scan_batch:
                bits.append(f"{r.scan_batch} #{r.scan_sequence or '?'}")
            if r.capture_date:
                bits.append(f"date: {r.capture_date}")
            if r.hint != "photo":
                bits.append(f"hint: {r.hint} (conf {r.hint_confidence:.2f})")
            return "\n".join(bits)
        if role == STATUS_ROLE:
            return r.triage_status
        if role == HINT_ROLE:
            return r.hint
        if role == PHOTO_ID_ROLE:
            return r.photo_id
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlags:
        if not index.isValid():
            return Qt.NoItemFlags
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemNeverHasChildren

    def _on_thumb_ready(self, photo_id: int) -> None:
        row = self.row_by_photo_id(photo_id)
        if row >= 0:
            ix = self.index(row, 0)
            self.dataChanged.emit(ix, ix, [Qt.DecorationRole])

    def update_status(self, photo_id: int, new_status: str,
                      is_private: bool) -> None:
        i = self.row_by_photo_id(photo_id)
        if i < 0:
            return
        self._rows[i] = replace(
            self._rows[i], triage_status=new_status, is_private=is_private,
        )
        ix = self.index(i, 0)
        self.dataChanged.emit(ix, ix, [Qt.DisplayRole, STATUS_ROLE])


def _placeholder_pixmap(size: int) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.darkGray)
    return pm


# ---------------------------------------------------------------------------
# Single view
# ---------------------------------------------------------------------------

class SingleView(QWidget):
    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._settings = settings
        self._current: TriageRow | None = None
        self._pixmap: QPixmap | None = None
        self._position = (0, 0)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self._pos_label = QLabel("")
        self._pos_label.setStyleSheet("color: gray; padding: 2px 6px")
        outer.addWidget(self._pos_label)

        self._image = QLabel()
        self._image.setAlignment(Qt.AlignCenter)
        self._image.setMinimumSize(400, 400)
        self._image.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._image.setStyleSheet("background: black")
        outer.addWidget(self._image, 1)

        self._exif = QLabel("")
        self._exif.setTextFormat(Qt.PlainText)
        self._exif.setStyleSheet(f"font-family: {FONT_MONO}; padding: 4px 6px")
        outer.addWidget(self._exif)

    def show_row(self, row: TriageRow, position: tuple[int, int]) -> None:
        self._current = row
        self._position = position
        thumb = thumb_path(self._settings, row.photo_id)
        pm = QPixmap()
        source = None
        with db.connection() as conn:
            conn.autocommit = True
            wp = conn.execute(
                "select working_path, quarantine_path from photos where id = %s",
                (row.photo_id,),
            ).fetchone()
        if wp:
            for candidate in (wp[0], wp[1]):
                if candidate and Path(candidate).exists():
                    if pm.load(candidate):
                        source = candidate
                        break
        if pm.isNull() and thumb.exists():
            pm.load(str(thumb))
        self._pixmap = pm
        self._paint()
        self._update_exif(source)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._paint()

    def _paint(self) -> None:
        if self._pixmap is None or self._pixmap.isNull():
            self._image.setText("(no image)")
            return
        target = self._image.size()
        self._image.setPixmap(self._pixmap.scaled(
            target, Qt.KeepAspectRatio, Qt.SmoothTransformation,
        ))
        i, n = self._position
        row = self._current
        if row is None:
            self._pos_label.setText("")
        else:
            tag = f"#{row.photo_id}  {row.triage_status}"
            if row.is_private:
                tag += "  private"
            if row.hint != "photo":
                tag += f"   hint: {row.hint} ({row.hint_confidence:.2f})"
            self._pos_label.setText(f"{i} / {n}    {tag}")

    def _update_exif(self, actual_source: str | None) -> None:
        row = self._current
        if row is None:
            self._exif.setText("")
            return
        year_source = f"  [year from {row.year_from}]" if row.year_from else ""
        lines = [
            f"date:   {row.capture_date or '—'}{year_source}",
            f"camera: {row.exif_camera or '—'}",
            f"size:   {row.width or '?'}×{row.height or '?'}",
            f"folder: {row.source_root}/{row.source_folder}",
            f"file:   {row.source_filename}",
        ]
        if row.scan_batch:
            lines.append(f"batch:  {row.scan_batch} #{row.scan_sequence or '?'}")
        if actual_source:
            lines.append(f"source: {actual_source}")
        self._exif.setText("\n".join(lines))


# ---------------------------------------------------------------------------
# Decision worker
# ---------------------------------------------------------------------------

class _DecisionSignals(QObject):
    done = Signal(int, object, object)


class _DecisionRunnable(QRunnable):
    def __init__(self, settings: Settings, photo_id: int, target: str,
                 hint: str | None) -> None:
        super().__init__()
        self.signals = _DecisionSignals()
        self._settings = settings
        self._photo_id = photo_id
        self._target = target
        self._hint = hint

    def run(self) -> None:
        try:
            r = decisions.apply_decision(
                self._settings, self._photo_id, self._target, hint=self._hint,
            )
            self.signals.done.emit(self._photo_id, r, None)
        except Exception as e:
            log.exception("decision failed for %s", self._photo_id)
            self.signals.done.emit(self._photo_id, None, repr(e))


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

HINT_KEY_MAP: dict[int, tuple[str, str]] = {
    Qt.Key_1: ("all", "untriaged"),
    Qt.Key_2: ("screenshot", "all_status"),
    Qt.Key_3: ("document", "all_status"),
    Qt.Key_4: ("blank_or_dark", "all_status"),
    Qt.Key_5: ("burst_or_tiny", "all_status"),
}


class TriagePanel(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._settings = load_config()
        self._cache = ThumbCache(self._settings, THUMB_TILE)
        self._pool = QThreadPool.globalInstance()
        self._undo: list[list[decisions.DecisionResult]] = []
        self._session_decisions = 0
        self._session_start: float | None = None
        self._presort_job: BackgroundJob | None = None

        outer = QVBoxLayout(self)

        outer.addLayout(self._build_filter_bar())
        outer.addLayout(self._build_action_bar())

        self._stack = QStackedWidget()
        self._grid_model = TriageGridModel(self._cache)
        self._grid = _TriageGridView()
        self._grid.setModel(self._grid_model)
        self._grid.setIconSize(QSize(THUMB_TILE, THUMB_TILE))
        self._grid.setGridSize(QSize(THUMB_TILE + 24, THUMB_TILE + 40))
        self._grid.setResizeMode(QListView.Adjust)
        self._grid.setViewMode(QListView.IconMode)
        self._grid.setUniformItemSizes(True)
        self._grid.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._grid.setLayoutMode(QListView.Batched)
        self._grid.setSpacing(4)
        self._grid.setWordWrap(False)
        self._grid.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._grid.setStyleSheet(_grid_style())
        self._grid.doubleClicked.connect(self._on_double_click)
        self._grid.activated.connect(self._on_double_click)
        self._grid.installEventFilter(self)

        self._single_view = SingleView(self._settings)
        self._single_view.installEventFilter(self)

        self._stack.addWidget(self._grid)
        self._stack.addWidget(self._single_view)
        outer.addWidget(self._stack, 1)

        outer.addLayout(self._build_status_strip())

        hints = QLabel(
            "  K keep    J junk    P private    U undo    Space select    "
            "Enter open    Esc back    1-5 hint filters    / focus filter    "
            "Ctrl+A select all"
        )
        hints.setStyleSheet(
            "color: #ccc; background: #333; padding: 4px 8px;"
            f"font-family: {FONT_MONO};"
        )
        outer.addWidget(hints)

        for combo in (self._f_status, self._f_hint, self._f_root,
                      self._f_folder, self._f_year, self._f_sort):
            combo.currentIndexChanged.connect(self._on_filter_changed)
        self._f_search.textChanged.connect(self._on_filter_changed)

        self._populate_filter_options()
        self._refresh_rows()
        self._refresh_presort_button()

    def _build_filter_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Status"))
        self._f_status = _combo(STATUS_CHOICES)
        bar.addWidget(self._f_status)
        bar.addWidget(QLabel("Hint"))
        self._f_hint = _combo(HINT_CHOICES)
        bar.addWidget(self._f_hint)
        bar.addWidget(QLabel("Root"))
        self._f_root = _combo([("all", "any root")])
        bar.addWidget(self._f_root)
        bar.addWidget(QLabel("Folder"))
        self._f_folder = _combo([("all", "any folder")])
        self._f_folder.setMinimumWidth(220)
        bar.addWidget(self._f_folder)
        bar.addWidget(QLabel("Year"))
        self._f_year = _combo([("all", "any")])
        bar.addWidget(self._f_year)
        bar.addWidget(QLabel("Sort"))
        self._f_sort = _combo(SORT_CHOICES)
        bar.addWidget(self._f_sort)
        bar.addStretch(1)
        bar.addWidget(QLabel("Search"))
        self._f_search = QLineEdit()
        self._f_search.setPlaceholderText("filename / folder …")
        self._f_search.setMaximumWidth(240)
        bar.addWidget(self._f_search)
        return bar

    def _build_action_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        self._presort_btn = QPushButton("Compute hints…")
        self._presort_btn.clicked.connect(self._on_presort)
        bar.addWidget(self._presort_btn)

        self._apply_btn = QPushButton("Apply hints (select junk-worthy)")
        self._apply_btn.clicked.connect(self._on_apply_hints)
        bar.addWidget(self._apply_btn)

        self._quar_btn = QPushButton("Quarantine browser…")
        self._quar_btn.clicked.connect(self._open_quarantine)
        bar.addWidget(self._quar_btn)

        bar.addStretch(1)

        self._presort_progress = QProgressBar()
        self._presort_progress.setVisible(False)
        self._presort_progress.setMaximumWidth(280)
        bar.addWidget(self._presort_progress)
        return bar

    def _build_status_strip(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        self._counts_lbl = QLabel("")
        self._counts_lbl.setStyleSheet(f"font-family: {FONT_MONO}")
        bar.addWidget(self._counts_lbl)
        bar.addStretch(1)
        self._session_lbl = QLabel("")
        self._session_lbl.setStyleSheet(f"font-family: {FONT_MONO}")
        bar.addWidget(self._session_lbl)
        return bar

    def _populate_filter_options(self) -> None:
        try:
            opts = _fetch_filter_options()
        except Exception:
            log.exception("filter option load failed")
            return
        _refill_combo(self._f_root, [("all", "any root")]
                       + [(r, r) for r in opts["roots"]])
        _refill_combo(self._f_folder, [("all", "any folder")]
                       + [(f, f) for f in opts["folders"]])
        _refill_combo(self._f_year, [("all", "any")]
                       + [(y, y) for y in opts["years"]])

    def _current_query(self) -> TriageQuery:
        return TriageQuery(
            status=self._f_status.currentData(),
            hint=self._f_hint.currentData(),
            source_root=self._f_root.currentData(),
            folder=self._f_folder.currentData(),
            year=self._f_year.currentData(),
            sort=self._f_sort.currentData(),
        )

    def _refresh_rows(self) -> None:
        query = self._current_query()
        try:
            rows = _fetch_rows(query)
        except Exception:
            log.exception("triage query failed")
            rows = []
        needle = (self._f_search.text() or "").strip().lower()
        if needle:
            rows = [r for r in rows if
                    needle in (r.source_filename or "").lower()
                    or needle in (r.source_folder or "").lower()]
        self._grid_model.set_rows(rows)
        if rows:
            self._grid.setCurrentIndex(self._grid_model.index(0, 0))
        self._refresh_counts()

    def _refresh_counts(self) -> None:
        with db.connection() as conn:
            conn.autocommit = True
            rows = conn.execute(
                """
                select triage_status, count(*)
                from photos
                where triage_status <> 'junk' or is_deleted
                group by triage_status
                """
            ).fetchall()
        by_status = {r[0]: int(r[1]) for r in rows}
        u = by_status.get("untriaged", 0)
        k = by_status.get("keep", 0)
        j = by_status.get("junk", 0)
        p = by_status.get("private", 0)
        self._counts_lbl.setText(
            f"untriaged {u:>6}   keep {k:>6}   junk {j:>6}   private {p:>4}   "
            f"showing {self._grid_model.rowCount()}"
        )
        self._refresh_session_stats()

    def _refresh_session_stats(self) -> None:
        if self._session_decisions == 0 or self._session_start is None:
            self._session_lbl.setText("session: 0 decisions")
            return
        elapsed_min = max((time.time() - self._session_start) / 60.0, 1 / 60)
        rate = self._session_decisions / elapsed_min
        self._session_lbl.setText(
            f"session: {self._session_decisions} decisions · {rate:.1f} / min"
        )

    def _refresh_presort_button(self) -> None:
        try:
            n = presort.count_photos_needing_hints()
        except Exception:
            n = 0
        if n:
            self._presort_btn.setText(f"Compute hints  ({n} pending)")
            self._presort_btn.setEnabled(True)
        else:
            self._presort_btn.setText("Compute hints  (all up to date)")
            self._presort_btn.setEnabled(True)

    def _on_filter_changed(self) -> None:
        self._refresh_rows()

    def _selected_photo_ids(self) -> list[int]:
        return [
            self._grid_model.rows()[ix.row()].photo_id
            for ix in self._grid.selectedIndexes()
        ]

    def _current_photo_id(self) -> int | None:
        ix = self._grid.currentIndex()
        if not ix.isValid():
            return None
        return self._grid_model.rows()[ix.row()].photo_id

    def _apply_to_selection(self, target: str) -> None:
        photo_ids = self._selected_photo_ids()
        if not photo_ids:
            pid = self._current_photo_id()
            if pid is None:
                return
            photo_ids = [pid]

        hint_by_pid = {r.photo_id: r.hint for r in self._grid_model.rows()}
        prev_snapshots: dict[int, dict[str, Any]] = {}
        for pid in photo_ids:
            i = self._grid_model.row_by_photo_id(pid)
            if i < 0:
                continue
            r = self._grid_model.rows()[i]
            prev_snapshots[pid] = {
                "triage_status": r.triage_status,
                "is_private": r.is_private,
            }
            is_private = (target == "private")
            self._grid_model.update_status(pid, target, is_private)

        this_group: list[decisions.DecisionResult] = []
        pending = {pid for pid in photo_ids}

        def _handle(photo_id: int, result: decisions.DecisionResult | None,
                    err: str | None) -> None:
            pending.discard(photo_id)
            if err:
                log.error("decision revert for %s: %s", photo_id, err)
                snap = prev_snapshots.get(photo_id) or {
                    "triage_status": "untriaged", "is_private": False,
                }
                self._grid_model.update_status(
                    photo_id, snap["triage_status"], snap["is_private"],
                )
                if not getattr(self, "_shown_err", False):
                    self._shown_err = True
                    QMessageBox.warning(
                        self, "Triage",
                        f"A commit failed and was reverted: {err}. "
                        "See log dock for details.",
                    )
            elif result is not None:
                this_group.append(result)
            if not pending:
                if this_group:
                    self._undo.append(this_group)
                self._session_decisions += len(this_group)
                if self._session_start is None:
                    self._session_start = time.time()
                self._refresh_counts()
                self._advance_cursor(photo_ids[-1])

        for pid in photo_ids:
            runnable = _DecisionRunnable(
                self._settings, pid, target, hint_by_pid.get(pid),
            )
            runnable.signals.done.connect(_handle)
            self._pool.start(runnable)

    def _advance_cursor(self, from_photo_id: int) -> None:
        i = self._grid_model.row_by_photo_id(from_photo_id)
        if i < 0:
            return
        n = self._grid_model.rowCount()
        if n == 0:
            return
        nxt = min(i + 1, n - 1)
        idx = self._grid_model.index(nxt, 0)
        self._grid.setCurrentIndex(idx)
        self._grid.scrollTo(idx, QListView.PositionAtCenter)
        if self._stack.currentIndex() == 1:
            self._show_single_at_row(nxt)

    def _do_undo(self) -> None:
        if not self._undo:
            return
        group = self._undo.pop()
        for r in reversed(group):
            try:
                new = decisions.undo(self._settings, r)
                self._grid_model.update_status(
                    r.photo_id, new.new["triage_status"], new.new["is_private"],
                )
                self._session_decisions = max(self._session_decisions - 1, 0)
            except Exception:
                log.exception("undo failed for %s", r.photo_id)
        self._refresh_counts()

    def _on_apply_hints(self) -> None:
        target_hints = {"screenshot", "document", "blank_or_dark",
                        "tiny", "burst"}
        sel = self._grid.selectionModel()
        sel.clearSelection()
        count = 0
        for i, r in enumerate(self._grid_model.rows()):
            if r.hint in target_hints and r.triage_status == "untriaged":
                sel.select(self._grid_model.index(i, 0),
                           sel.SelectionFlag.Select)
                count += 1
        self._counts_lbl.setText(
            self._counts_lbl.text() + f"    · selected {count} for review"
        )

    def _on_presort(self) -> None:
        if self._presort_job is not None:
            return
        self._presort_btn.setEnabled(False)
        self._presort_progress.setVisible(True)
        self._presort_progress.setRange(0, 0)
        self._presort_job = BackgroundJob(
            lambda progress_cb, cancel_token: presort.run_presort(
                progress_cb=progress_cb, cancel_token=cancel_token,
            )
        )
        self._presort_job.signals.progress.connect(self._on_presort_progress)
        self._presort_job.signals.finished.connect(self._on_presort_finished)
        self._presort_job.signals.failed.connect(self._on_presort_failed)
        self._presort_job.start()

    def _on_presort_progress(self, payload: dict) -> None:
        if payload.get("kind") == "progress":
            total = int(payload.get("total") or 1)
            done = int(payload.get("done") or 0)
            self._presort_progress.setRange(0, total)
            self._presort_progress.setValue(done)

    def _on_presort_finished(self, payload: dict) -> None:
        self._presort_progress.setVisible(False)
        self._presort_job = None
        summary = payload if payload else {}
        dist = summary.get("distribution", {})
        lines = [f"Elapsed: {summary.get('elapsed_s', 0):.1f}s",
                 f"Processed: {summary.get('processed', 0)} photos",
                 f"Burst extras: {summary.get('burst_extras', 0)}",
                 f"Untriaged without hints: "
                 f"{summary.get('untriaged_without_hints_after', 0)}",
                 "",
                 "Distribution:"]
        for h in ("photo", "screenshot", "document", "blank_or_dark",
                  "tiny", "burst", "exact_dup_of"):
            lines.append(f"  {h:<14} {dist.get(h, 0)}")
        QMessageBox.information(self, "Presort complete", "\n".join(lines))
        self._refresh_presort_button()
        self._refresh_rows()

    def _on_presort_failed(self, tb: str) -> None:
        self._presort_progress.setVisible(False)
        self._presort_job = None
        self._presort_btn.setEnabled(True)
        QMessageBox.critical(self, "Presort failed", tb[-2000:])

    def _open_quarantine(self) -> None:
        dlg = QuarantineBrowser(self._settings, self)
        dlg.exec()
        self._refresh_rows()

    def _on_double_click(self, index) -> None:
        if not index.isValid():
            return
        self._show_single_at_row(index.row())
        self._stack.setCurrentIndex(1)

    def _show_single_at_row(self, row: int) -> None:
        rows = self._grid_model.rows()
        if row < 0 or row >= len(rows):
            return
        self._single_view.show_row(rows[row], (row + 1, len(rows)))

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if event.type() == event.Type.KeyPress:
            key = event.key()
            handled = self._handle_key(key, event)
            if handled:
                return True
        return super().eventFilter(obj, event)

    def _handle_key(self, key: int, event: QKeyEvent) -> bool:
        if self._f_search.hasFocus() and key not in (Qt.Key_Escape,):
            return False
        if key == Qt.Key_K:
            self._apply_to_selection("keep"); return True
        if key == Qt.Key_J:
            self._apply_to_selection("junk"); return True
        if key == Qt.Key_P:
            self._apply_to_selection("private"); return True
        if key == Qt.Key_U:
            self._do_undo(); return True
        if key == Qt.Key_Slash:
            self._f_search.setFocus(); self._f_search.selectAll(); return True
        if key in HINT_KEY_MAP:
            self._apply_hint_key(key); return True
        if key == Qt.Key_Escape:
            if self._stack.currentIndex() == 1:
                self._stack.setCurrentIndex(0)
                self._grid.setFocus()
                return True
            if self._f_search.hasFocus():
                self._f_search.clearFocus()
                self._grid.setFocus()
                return True
        if key == Qt.Key_Return or key == Qt.Key_Enter:
            if self._stack.currentIndex() == 0:
                ix = self._grid.currentIndex()
                if ix.isValid():
                    self._show_single_at_row(ix.row())
                    self._stack.setCurrentIndex(1)
                return True
            self._stack.setCurrentIndex(0)
            self._grid.setFocus()
            return True
        if key == Qt.Key_Space and self._stack.currentIndex() == 0:
            ix = self._grid.currentIndex()
            if ix.isValid():
                sel = self._grid.selectionModel()
                cmd = (sel.SelectionFlag.Toggle
                       | sel.SelectionFlag.Rows)
                sel.select(ix, cmd)
            return True
        if self._stack.currentIndex() == 1 and key in (
            Qt.Key_Left, Qt.Key_Right, Qt.Key_PageUp, Qt.Key_PageDown,
            Qt.Key_Home, Qt.Key_End, Qt.Key_Up, Qt.Key_Down,
        ):
            self._navigate_single(key)
            return True
        return False

    def _apply_hint_key(self, key: int) -> None:
        if key == Qt.Key_1:
            _select_by_data(self._f_status, "untriaged")
            _select_by_data(self._f_hint, "all")
        elif key == Qt.Key_2:
            _select_by_data(self._f_hint, "screenshot")
        elif key == Qt.Key_3:
            _select_by_data(self._f_hint, "document")
        elif key == Qt.Key_4:
            _select_by_data(self._f_hint, "blank_or_dark")
        elif key == Qt.Key_5:
            current = self._f_hint.currentData()
            _select_by_data(self._f_hint, "burst" if current == "tiny" else "tiny")

    def _navigate_single(self, key: int) -> None:
        ix = self._grid.currentIndex()
        row = ix.row() if ix.isValid() else 0
        n = self._grid_model.rowCount()
        if n == 0:
            return
        step = 1
        if key in (Qt.Key_Left, Qt.Key_Up):
            step = -1
        elif key == Qt.Key_PageUp:
            step = -10
        elif key == Qt.Key_PageDown:
            step = 10
        elif key == Qt.Key_Home:
            row = 0; step = 0
        elif key == Qt.Key_End:
            row = n - 1; step = 0
        new_row = max(0, min(row + step, n - 1))
        idx = self._grid_model.index(new_row, 0)
        self._grid.setCurrentIndex(idx)
        self._grid.scrollTo(idx, QListView.PositionAtCenter)
        self._show_single_at_row(new_row)


class _TriageGridView(QListView):
    def keyPressEvent(self, event) -> None:  # noqa: N802
        if (event.matches(QKeySequence.SelectAll)
                and self.model() is not None):
            m = self.model()
            first = m.index(0, 0)
            last = m.index(m.rowCount() - 1, 0)
            self.selectionModel().select(
                first,
                self.selectionModel().SelectionFlag.ClearAndSelect,
            )
            for i in range(m.rowCount()):
                self.selectionModel().select(
                    m.index(i, 0),
                    self.selectionModel().SelectionFlag.Select,
                )
            event.accept()
            return
        super().keyPressEvent(event)


class QuarantineBrowser(QDialog):
    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Quarantine browser")
        self.resize(1100, 700)
        self._settings = settings
        self._cache = ThumbCache(settings, THUMB_TILE)
        self._model = TriageGridModel(self._cache)

        outer = QVBoxLayout(self)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Show"))
        self._show = _combo([
            ("junk", "Junk (soft-deleted)"),
            ("private", "Private"),
        ])
        bar.addWidget(self._show)
        bar.addWidget(QLabel("Search"))
        self._search = QLineEdit()
        self._search.setPlaceholderText("filename / folder …")
        bar.addWidget(self._search, 1)
        self._restore = QPushButton("Restore selected")
        self._restore.clicked.connect(self._on_restore)
        bar.addWidget(self._restore)
        outer.addLayout(bar)

        self._view = QListView()
        self._view.setModel(self._model)
        self._view.setViewMode(QListView.IconMode)
        self._view.setResizeMode(QListView.Adjust)
        self._view.setSpacing(4)
        self._view.setUniformItemSizes(True)
        self._view.setIconSize(QSize(THUMB_TILE, THUMB_TILE))
        self._view.setGridSize(QSize(THUMB_TILE + 24, THUMB_TILE + 40))
        self._view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        outer.addWidget(self._view, 1)

        self._show.currentIndexChanged.connect(self._refresh)
        self._search.textChanged.connect(self._refresh)
        self._refresh()

    def _refresh(self) -> None:
        target = self._show.currentData()
        rows = _fetch_rows(TriageQuery(status=target))
        needle = (self._search.text() or "").strip().lower()
        if needle:
            rows = [r for r in rows if
                    needle in (r.source_filename or "").lower()
                    or needle in (r.source_folder or "").lower()]
        self._model.set_rows(rows)

    def _on_restore(self) -> None:
        selected = [
            self._model.rows()[ix.row()].photo_id
            for ix in self._view.selectedIndexes()
        ]
        if not selected:
            return
        target = self._show.currentData()
        for pid in selected:
            try:
                if target == "junk":
                    decisions.restore_from_quarantine(self._settings, pid)
                else:
                    decisions.unprivate(self._settings, pid)
            except Exception:
                log.exception("restore failed for %s", pid)
        self._refresh()


def _combo(items: list[tuple[str, str]]) -> QComboBox:
    c = QComboBox()
    for value, label in items:
        c.addItem(label, value)
    return c


def _refill_combo(c: QComboBox, items: list[tuple[str, str]]) -> None:
    current = c.currentData()
    c.blockSignals(True)
    c.clear()
    for value, label in items:
        c.addItem(label, value)
    if current is not None:
        i = c.findData(current)
        if i >= 0:
            c.setCurrentIndex(i)
    c.blockSignals(False)


def _select_by_data(c: QComboBox, value: str) -> None:
    i = c.findData(value)
    if i >= 0:
        c.setCurrentIndex(i)


def _grid_style() -> str:
    return """
        QListView { background: #1e1e1e; color: white; }
        QListView::item { padding: 6px; border: 2px solid transparent; }
        QListView::item:selected { border: 2px solid #4ea1ff; }
    """
