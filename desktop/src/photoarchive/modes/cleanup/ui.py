"""Cleanup mode UI.

Run analysis over the scan scope, then walk the proposals in
`scan_batch`/`scan_sequence` order with before and after side by side.

Keys:
    A       accept the ticked ops (or the split)
    R       reject → copy to MANUAL_FIX_DIR, park as `manual`
    S       skip (stays pending, comes back at the tail of this session)
    E       send to remote enhance
    Z       undo the last accept / split (this session)
    T       toggle A/B in the left pane
    F       fit both panes to the window
    Left    previous proposal
    Right   next proposal
    1..4    toggle op 1..4

Worker threads never touch a widget (the Phase 3 lesson): every background
step is a `BackgroundJob` whose `progress`/`finished`/`failed` signals are
Qt-queued onto the GUI thread, and the panel does all the rendering.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image
from PySide6.QtCore import QSettings, QTimer, Qt, Signal
from PySide6.QtGui import QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QProgressBar, QPushButton, QSizePolicy,
    QVBoxLayout, QWidget,
)

from ... import db
from ...config import load as load_config
from ...workers import BackgroundJob, Cancelled
from ...app.widgets.synced_viewer import SyncedViewer
from ..ingest.paths import thumb_path
from . import accept as accept_mod
from . import analyse as analyse_mod
from . import job as job_mod
from . import paths as cpaths
from . import remote_enhance as remote_mod
from . import render as render_mod
from . import report as report_mod
from . import repo
from . import split as split_mod
from .geometry import Transform
from .remote import build_provider

log = logging.getLogger(__name__)

FILMSTRIP_THUMB = 88
#: QSettings key for the review queue's `Show:` filter (fix-up 4).
QUEUE_FILTER_KEY = "cleanup/queue_filter"
# Zoom at which a real full-resolution crop replaces the stretched preview.
DETAIL_SCALE = 0.9
# Cap on one detail tile so a full-screen 1:1 view of a 93 MP scan stays sane.
DETAIL_MAX_PX = 2600
OP_LABELS = {
    "deskew": "Deskew", "crop": "Crop", "colour": "Colour cast",
    "levels": "Levels / fade", "split": "Split", "remote_enhance": "Remote",
}


@dataclass
class _UndoEntry:
    kind: str           # 'accept' | 'split'
    photo_id: int
    proposal_id: int


class CleanupPanel(QWidget):
    """The whole Cleanup mode."""

    _status = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._settings = load_config()
        self._provider = build_provider(self._settings)
        self._proposal_ids: list[int] = []
        self._cursor = 0
        self._proposal: repo.Proposal | None = None
        self._ticked: set[str] = set()
        self._undo_stack: list[_UndoEntry] = []
        self._job: BackgroundJob | None = None
        self._view_job: BackgroundJob | None = None
        self._detail_job: BackgroundJob | None = None
        # A cancelled BackgroundJob keeps running until its target next checks
        # the token. Dropping the last Python reference to a live QThread makes
        # Qt abort ("Destroyed while thread is still running"), so every job
        # stays in here until its own `finished` signal says it has stopped.
        self._live_jobs: list[BackgroundJob] = []
        self._showing_after_left = False
        self._before_path: Path | None = None
        self._after_path: Path | None = None
        self._before_dims: tuple[int, int] = (0, 0)
        self._after_dims: tuple[int, int] = (0, 0)
        self._manual_mode = False
        self._session_spend = 0.0
        # The status bar has two halves. The left is the last *decision* and
        # persists until the next one (the Phase 3 lesson: no flash messages —
        # George must be able to look away and still see what he just did).
        # The right is context: which photo, what was measured, what a job is
        # doing. A preview finishing must never wipe out what George just did.
        self._last_action = ""
        self._context = "Ready."


        self._build_ui()
        self._install_shortcuts()
        self._status.connect(self._set_status)
        # Defer the first DB read until the event loop is up, so the panel can
        # be constructed before init_pool in tests.
        QTimer.singleShot(0, self._refresh_queue)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self._header = QLabel("Cleanup — press Run analysis")
        self._header.setStyleSheet("font-weight: bold; padding: 4px;")

        self._batch_box = QComboBox()
        self._batch_box.addItem("All batches", None)

        # Fix-up 4: George reviews the 17 splits, not the 981 minimal crops.
        self._filter_box = QComboBox()
        for key, label in repo.QUEUE_FILTERS:
            self._filter_box.addItem(label, key)
        self._filter_box.setToolTip(
            "Which pending proposals the queue and filmstrip show. "
            "The choice is remembered between sessions."
        )
        idx = self._filter_box.findData(self._load_queue_filter())
        self._filter_box.setCurrentIndex(max(0, idx))
        self._filter_box.currentIndexChanged.connect(self._on_queue_filter_changed)

        self._reanalyse = QCheckBox("Re-analyse")
        self._reanalyse.setToolTip(
            "Re-measure photos that already have a proposal. Off by default, "
            "so a killed run resumes where it stopped."
        )
        self._run_btn = QPushButton("Run analysis")
        self._run_btn.clicked.connect(self._start_analysis)
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self._cancel_job)
        self._cancel_btn.setEnabled(False)
        self._report_btn = QPushButton("Write report")
        self._report_btn.clicked.connect(self._write_report)

        run_row = QHBoxLayout()
        run_row.addWidget(QLabel("Batch:"))
        run_row.addWidget(self._batch_box)
        run_row.addWidget(QLabel("Show:"))
        run_row.addWidget(self._filter_box)
        run_row.addWidget(self._reanalyse)
        run_row.addWidget(self._run_btn)
        run_row.addWidget(self._cancel_btn)
        run_row.addWidget(self._report_btn)
        run_row.addStretch(1)

        self._accept_btn = QPushButton("Accept (A)")
        self._accept_btn.clicked.connect(self._accept)
        self._reject_btn = QPushButton("Reject → manual (R)")
        self._reject_btn.clicked.connect(self._reject)
        self._skip_btn = QPushButton("Skip (S)")
        self._skip_btn.clicked.connect(self._skip)
        self._remote_btn = QPushButton("Remote enhance (E)")
        self._remote_btn.clicked.connect(self._remote)
        self._undo_btn = QPushButton("Undo (Z)")
        self._undo_btn.clicked.connect(self._undo)
        self._manual_btn = QPushButton("Manual queue")
        self._manual_btn.setCheckable(True)
        self._manual_btn.toggled.connect(self._toggle_manual)

        if not self._provider.available:
            self._remote_btn.setEnabled(False)
            self._remote_btn.setToolTip(
                self._provider.unavailable_reason() or "Remote enhance is off."
            )

        act_row = QHBoxLayout()
        act_row.addWidget(self._accept_btn)
        act_row.addWidget(self._reject_btn)
        act_row.addWidget(self._skip_btn)
        act_row.addWidget(self._remote_btn)
        act_row.addWidget(self._undo_btn)
        act_row.addStretch(1)
        act_row.addWidget(self._manual_btn)

        self._bulk_batch_btn = QPushButton("Accept all geometric-only in this batch…")
        self._bulk_batch_btn.clicked.connect(lambda: self._bulk_accept(this_batch=True))
        self._bulk_all_btn = QPushButton("…in the whole queue…")
        self._bulk_all_btn.clicked.connect(lambda: self._bulk_accept(this_batch=False))
        bulk_row = QHBoxLayout()
        bulk_row.addWidget(self._bulk_batch_btn)
        bulk_row.addWidget(self._bulk_all_btn)
        bulk_row.addStretch(1)

        self._progress = QProgressBar()
        self._progress.setVisible(False)

        self._filmstrip = QListWidget()
        self._filmstrip.setFlow(QListWidget.LeftToRight)
        self._filmstrip.setWrapping(False)
        self._filmstrip.setFixedHeight(FILMSTRIP_THUMB + 46)
        self._filmstrip.setIconSize(
            self._filmstrip.iconSize().__class__(FILMSTRIP_THUMB, FILMSTRIP_THUMB)
        )
        self._filmstrip.setSelectionMode(QListWidget.SingleSelection)
        self._filmstrip.itemSelectionChanged.connect(self._on_filmstrip_pick)

        self._viewer = SyncedViewer()
        self._viewer.view_changed.connect(self._schedule_detail)
        self._detail_timer = QTimer(self)
        self._detail_timer.setSingleShot(True)
        self._detail_timer.setInterval(180)
        self._detail_timer.timeout.connect(self._load_detail)

        self._ops_row = QHBoxLayout()
        self._op_boxes: dict[str, QCheckBox] = {}
        ops_frame = QFrame()
        ops_frame.setLayout(self._ops_row)

        self._facts = QLabel("")
        self._facts.setTextFormat(Qt.RichText)
        self._facts.setWordWrap(True)
        self._facts.setStyleSheet(
            "font-family: Consolas, Menlo, monospace; padding: 4px; "
            "background: #111; color: #ddd;"
        )

        self._status_bar = QLabel("Ready.")
        self._status_bar.setStyleSheet(
            "padding: 4px; background: #1b1e23; color: #cfd6de;"
        )
        self._status_bar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.addWidget(self._header)
        outer.addLayout(run_row)
        outer.addLayout(act_row)
        outer.addLayout(bulk_row)
        outer.addWidget(self._progress)
        outer.addWidget(self._filmstrip)
        outer.addWidget(self._viewer, stretch=1)
        outer.addWidget(ops_frame)
        outer.addWidget(self._facts)
        outer.addWidget(self._status_bar)

    def _install_shortcuts(self) -> None:
        def _sc(seq: str, fn) -> None:
            s = QShortcut(QKeySequence(seq), self)
            s.setContext(Qt.WidgetWithChildrenShortcut)
            s.activated.connect(fn)

        _sc("A", self._accept)
        _sc("R", self._reject)
        _sc("S", self._skip)
        _sc("E", self._remote)
        _sc("Z", self._undo)
        _sc("T", self._toggle_ab)
        _sc("F", self._viewer.fit_to_window)
        _sc("Left", self._prev)
        _sc("Right", self._next)
        for i in range(1, 5):
            _sc(str(i), lambda pos=i - 1: self._toggle_op_at(pos))

    # ------------------------------------------------------------------
    # Background jobs
    # ------------------------------------------------------------------

    def _track(self, job: BackgroundJob) -> BackgroundJob:
        """Hold a reference to a running job until the thread really ends."""
        self._live_jobs.append(job)
        job.finished.connect(lambda j=job: self._untrack(j))
        return job

    def _untrack(self, job: BackgroundJob) -> None:
        try:
            self._live_jobs.remove(job)
        except ValueError:
            pass

    # ------------------------------------------------------------------
    # Status bar (persistent — never a flash message)
    # ------------------------------------------------------------------

    def _set_status(self, _ignored: str = "") -> None:
        parts = [part for part in (self._last_action, self._context) if part]
        parts.append(f"remote spend: session ${self._session_spend:.2f}")
        self._status_bar.setText("  |  ".join(parts))

    def _say(self, text: str) -> None:
        """Context: replaced freely as the view changes."""
        self._context = text
        self._status.emit(text)

    def _say_action(self, text: str) -> None:
        """A decision. Stays put until the next one."""
        self._last_action = text
        self._status.emit(text)

    # ------------------------------------------------------------------
    # Queue
    # ------------------------------------------------------------------

    def _refresh_queue(self) -> None:
        try:
            with db.connection() as conn:
                conn.autocommit = True
                labels = repo.batch_labels(conn)
                counts = repo.status_counts(conn)
                scope = repo.scope_count(conn)
                spend = repo.spend_total(conn)
                by_filter = repo.pending_counts_by_filter(
                    conn, batches=self._selected_batches())
                if self._manual_mode:
                    ids = [p.id for p in repo.manual_queue(conn)]
                else:
                    ids = repo.pending_ids(conn,
                                           batches=self._selected_batches(),
                                           queue_filter=self._queue_filter())
        except Exception as e:
            self._say(f"Database not ready: {e}")
            return

        self._reload_batches(labels)
        self._relabel_filters(by_filter)
        self._proposal_ids = ids
        if self._cursor >= len(ids):
            self._cursor = max(0, len(ids) - 1)
        counts_text = "  ".join(f"{k} {v}" for k, v in sorted(counts.items()))
        # The count the header leads with is the queue actually in front of
        # George, which is the point of the filter — the archive-wide totals
        # follow it.
        shown = (f"showing {len(ids)} {self._queue_filter_label().lower()}"
                 if not self._manual_mode
                 else f"showing {len(ids)} sent to manual fix")
        self._header.setText(
            f"Cleanup — {shown}; scope {scope} scans; "
            f"{counts_text or 'no proposals yet'}"
            + (f"; total remote spend ${spend:.2f}" if spend else "")
        )
        if not ids:
            self._render_empty()
        else:
            self._load_current()

    def _reload_batches(self, labels: list[str]) -> None:
        current = self._batch_box.currentData()
        if [self._batch_box.itemData(i) for i in range(self._batch_box.count())] == \
                [None] + labels:
            return
        self._batch_box.blockSignals(True)
        self._batch_box.clear()
        self._batch_box.addItem("All batches", None)
        for label in labels:
            self._batch_box.addItem(label, label)
        idx = self._batch_box.findData(current)
        self._batch_box.setCurrentIndex(max(0, idx))
        self._batch_box.blockSignals(False)

    def _selected_batches(self) -> list[str] | None:
        data = self._batch_box.currentData()
        return [data] if data else None

    # -- the Show: filter ----------------------------------------------

    def _queue_filter(self) -> str:
        return self._filter_box.currentData() or "all"

    def _queue_filter_label(self) -> str:
        key = self._queue_filter()
        return dict(repo.QUEUE_FILTERS).get(key, "All pending")

    @staticmethod
    def _qsettings() -> QSettings:
        return QSettings("PhotoArchive", "PhotoArchive")

    def _load_queue_filter(self) -> str:
        """The stored choice, validated — a key removed from `QUEUE_FILTERS`
        in a later version must not leave the queue showing nothing."""
        stored = self._qsettings().value(QUEUE_FILTER_KEY, "all")
        valid = {key for key, _label in repo.QUEUE_FILTERS}
        return stored if stored in valid else "all"

    def _on_queue_filter_changed(self) -> None:
        self._qsettings().setValue(QUEUE_FILTER_KEY, self._queue_filter())
        # A different queue means a different photo: start at its top rather
        # than keeping an index into the list that just went away.
        self._cursor = 0
        self._refresh_queue()

    def _relabel_filters(self, counts: dict[str, int]) -> None:
        """Put each filter's size in its label, so the choice is informed."""
        self._filter_box.blockSignals(True)
        for i in range(self._filter_box.count()):
            key = self._filter_box.itemData(i)
            label = dict(repo.QUEUE_FILTERS).get(key, key)
            n = counts.get(key)
            self._filter_box.setItemText(
                i, label if n is None else f"{label} ({n})")
        self._filter_box.blockSignals(False)

    def _load_current(self) -> None:
        if not self._proposal_ids:
            self._render_empty()
            return
        pid = self._proposal_ids[self._cursor]
        with db.connection() as conn:
            conn.autocommit = True
            proposal = repo.load_proposal(conn, pid)
        if proposal is None:
            self._proposal_ids.pop(self._cursor)
            self._load_current()
            return
        self._proposal = proposal
        self._ticked = set(render_mod.default_ticked(
            proposal.operations, self._settings))
        if proposal.needs_manual:
            # Answer 7: geometric ops are not offered on a scan the analyser
            # could not read; the tonal ones still are.
            self._ticked -= {"deskew", "crop"}
        self._showing_after_left = False
        self._render()

    def _prev(self) -> None:
        if self._proposal_ids and self._cursor > 0:
            self._cursor -= 1
            self._load_current()

    def _next(self) -> None:
        if self._proposal_ids and self._cursor < len(self._proposal_ids) - 1:
            self._cursor += 1
            self._load_current()

    def _skip(self) -> None:
        if not self._proposal_ids:
            return
        pid = self._proposal_ids.pop(self._cursor)
        self._proposal_ids.append(pid)
        if self._cursor >= len(self._proposal_ids):
            self._cursor = 0
        self._say_action(f"Skipped proposal {pid}; it returns at the end of this pass.")
        self._load_current()

    def _advance_past_current(self) -> None:
        if not self._proposal_ids:
            self._refresh_queue()
            return
        del self._proposal_ids[self._cursor]
        if self._cursor >= len(self._proposal_ids):
            self._cursor = max(0, len(self._proposal_ids) - 1)
        if not self._proposal_ids:
            self._refresh_queue()
        else:
            self._load_current()

    def _on_filmstrip_pick(self) -> None:
        item = self._filmstrip.currentItem()
        if item is None:
            return
        pid = item.data(Qt.UserRole)
        if pid in self._proposal_ids:
            self._cursor = self._proposal_ids.index(pid)
            self._load_current()

    def _toggle_manual(self, on: bool) -> None:
        self._manual_mode = bool(on)
        self._cursor = 0
        for b in (self._accept_btn, self._reject_btn, self._remote_btn,
                  self._bulk_batch_btn, self._bulk_all_btn):
            b.setEnabled(not on)
        self._refresh_queue()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render_empty(self) -> None:
        which = "manual" if self._manual_mode else "pending"
        self._proposal = None
        self._filmstrip.clear()
        self._viewer.set_left_base(None, 0, 0, caption="")
        self._viewer.set_right_base(None, 0, 0, caption="")
        self._facts.setText("")
        self._clear_op_boxes()
        for b in (self._accept_btn, self._reject_btn, self._skip_btn,
                  self._remote_btn):
            b.setEnabled(False)
        self._say(f"No {which} proposals. Run analysis to build the queue.")

    def _render(self) -> None:
        p = self._proposal
        if p is None:
            return
        n = len(self._proposal_ids)
        loc = f"{p.scan_batch or '-'}#{p.scan_sequence if p.scan_sequence is not None else '?'}"
        # "3 of 17" means nothing without saying 17 of what: the count is the
        # filtered queue, so the filter has to be named beside it.
        if self._manual_mode:
            scope = " sent to manual fix"
        elif self._queue_filter() != "all":
            scope = f" {self._queue_filter_label().lower()}"
        else:
            scope = " pending"
        self._header.setText(
            f"Cleanup — showing {self._cursor + 1} / {n}{scope}  ·  "
            f"photo #{p.photo_id}  ·  {loc}  ·  {p.source_filename}"
        )
        enabled = not self._manual_mode
        for b in (self._accept_btn, self._reject_btn, self._remote_btn):
            b.setEnabled(enabled and not (b is self._remote_btn
                                          and not self._provider.available))
        self._skip_btn.setEnabled(True)

        self._rebuild_filmstrip()
        self._rebuild_op_boxes()
        self._facts.setText(self._facts_html(p))
        self._request_views()

    def _rebuild_filmstrip(self) -> None:
        self._filmstrip.blockSignals(True)
        self._filmstrip.clear()
        lo = max(0, self._cursor - 12)
        hi = min(len(self._proposal_ids), self._cursor + 13)
        with db.connection() as conn:
            conn.autocommit = True
            for pid in self._proposal_ids[lo:hi]:
                prop = repo.load_proposal(conn, pid)
                if prop is None:
                    continue
                item = QListWidgetItem()
                mark = "SPLIT " if prop.is_split else ""
                mark += "MANUAL " if prop.needs_manual else ""
                item.setText(f"{mark}#{prop.photo_id}\n{','.join(prop.op_names) or '-'}")
                item.setData(Qt.UserRole, pid)
                tp = thumb_path(self._settings, prop.photo_id)
                if tp.exists():
                    pix = QPixmap(str(tp))
                    if not pix.isNull():
                        item.setIcon(pix)
                self._filmstrip.addItem(item)
                if pid == self._proposal_ids[self._cursor]:
                    self._filmstrip.setCurrentRow(self._filmstrip.count() - 1)
        self._filmstrip.blockSignals(False)

    def _clear_op_boxes(self) -> None:
        for box in list(self._op_boxes.values()):
            self._ops_row.removeWidget(box)
            box.deleteLater()
        self._op_boxes = {}

    def _rebuild_op_boxes(self) -> None:
        self._clear_op_boxes()
        p = self._proposal
        if p is None:
            return
        ops = (p.operations.get("ops") or {})
        for i, name in enumerate(
            [n for n in ("deskew", "crop", "colour", "levels",
                         "split", "remote_enhance") if n in ops]
        ):
            box = QCheckBox(f"{i + 1}. {OP_LABELS.get(name, name)}")
            box.setChecked(name in self._ticked or name in ("split", "remote_enhance"))
            if name in ("split", "remote_enhance"):
                box.setEnabled(False)
                box.setToolTip(
                    "Splits and remote results are all-or-nothing: accept or skip."
                )
            elif p.needs_manual and name in ("deskew", "crop"):
                box.setEnabled(False)
                box.setToolTip(
                    f"Geometry is not offered: {p.manual_reason or 'needs manual work'}."
                )
            else:
                box.toggled.connect(
                    lambda checked, n=name: self._on_op_toggled(n, checked))
            self._ops_row.addWidget(box)
            self._op_boxes[name] = box
        self._ops_row.addStretch(1)

    def _toggle_op_at(self, pos: int) -> None:
        names = list(self._op_boxes.keys())
        if 0 <= pos < len(names):
            box = self._op_boxes[names[pos]]
            if box.isEnabled():
                box.setChecked(not box.isChecked())

    def _on_op_toggled(self, name: str, checked: bool) -> None:
        if checked:
            self._ticked.add(name)
        else:
            self._ticked.discard(name)
        self._request_views()

    def _facts_html(self, p: repo.Proposal) -> str:
        o = p.operations or {}
        caption = analyse_mod.caption_for(o)
        rows = [f"<b>{caption}</b>"]
        rect = o.get("print_rect") or {}
        bed = o.get("bed") or {}
        analysis = o.get("analysis") or {}
        rows.append(
            "<pre>"
            f"print {o.get('print_frac', '?')} of the scan, rectangularity "
            f"{o.get('rectangularity', '?')}, bed {bed.get('kind', '?')} "
            f"({bed.get('grey', '?')})\n"
            f"source {analysis.get('src_w', '?')}×{analysis.get('src_h', '?')}"
            f"  dpi {analysis.get('dpi') or 'unknown'}"
            f"  inset {analysis.get('inset_px', '?')} px"
            f"  analysed in {p.analysis_ms or '?'} ms\n"
            f"faces {p.face_count} ({p.labelled_face_count} labelled)"
            f"  file_version {p.file_version} → {p.file_version + 1}"
            "</pre>"
        )
        if p.needs_manual:
            reason = {
                "print_too_small": "the print covers too little of the scan",
                "implausible_aspect": "the detected rectangle is not a print shape",
                "skew_too_large": "the skew is too large to be a scanner slip",
                "has_back": "has a back — split by hand",
                "no_print_found": "no print rectangle found against the bed",
            }.get(p.manual_reason or "", p.manual_reason or "needs manual work")
            rows.append(f"<b style='color:#e6b800'>needs manual: {reason}</b>")
        if p.is_split and p.split_regions:
            bits = ", ".join(
                f"#{r['index']} {r['area_frac']:.0%}" for r in p.split_regions
            )
            rows.append(
                f"<b style='color:#7fd1ff'>split into {len(p.split_regions)} "
                f"prints: {bits} — the parent is quarantined as split_parent</b>"
            )
        tone = (o.get("tone") or {}).get("chroma") or {}
        if o.get("colour_skipped"):
            rows.append(
                f"<pre>colour op skipped ({o['colour_skipped']}): p95 chroma "
                f"{tone.get('p95_chroma', '?')}, hue variance "
                f"{tone.get('hue_variance', '?')}</pre>"
            )
        return "<br>".join(rows)

    # ------------------------------------------------------------------
    # Preview rendering (background)
    # ------------------------------------------------------------------

    def _request_views(self) -> None:
        p = self._proposal
        if p is None:
            return
        if self._view_job is not None:
            self._view_job.cancel()
        self._viewer.clear_details()
        self._say(f"Rendering preview for photo {p.photo_id}…")
        job = self._track(BackgroundJob(
            target=_views_target,
            kwargs={"proposal_id": p.id, "ticked": sorted(self._ticked)},
        ))
        job.signals.finished.connect(self._on_views_ready)
        job.signals.failed.connect(self._on_views_failed)
        self._view_job = job
        job.start()

    def _on_views_ready(self, payload: dict) -> None:
        self._view_job = None
        p = self._proposal
        if p is None or payload.get("proposal_id") != p.id:
            return
        self._before_path = Path(payload["before"]) if payload.get("before") else None
        self._after_path = Path(payload["after"]) if payload.get("after") else None
        self._before_dims = tuple(payload.get("before_dims") or (0, 0))  # type: ignore
        self._after_dims = tuple(payload.get("after_dims") or (0, 0))    # type: ignore
        self._paint_bases()
        caption = analyse_mod.caption_for(p.operations)
        self._say(f"photo {p.photo_id}: {caption}. "
                  f"A accept · R manual · S skip · E remote · Z undo · T A/B")

    def _on_views_failed(self, tb: str) -> None:
        self._view_job = None
        log.error("cleanup: preview render failed\n%s", tb)
        self._say("Preview render failed — see the log.")

    def _paint_bases(self) -> None:
        before = QPixmap(str(self._before_path)) if self._before_path else QPixmap()
        after = QPixmap(str(self._after_path)) if self._after_path else QPixmap()
        bw, bh = self._before_dims
        aw, ah = self._after_dims
        left_is_after = self._showing_after_left
        left_pix, left_dims, left_label = (
            (after, (aw, ah), "after (T toggles)") if left_is_after
            else (before, (bw, bh), "before")
        )
        self._viewer.set_left_base(left_pix, left_dims[0], left_dims[1],
                                  caption=f"{left_label}  {left_dims[0]}×{left_dims[1]}")
        self._viewer.set_right_base(after, aw, ah,
                                    caption=f"after  {aw}×{ah}")
        self._viewer.fit_to_window()

    def _toggle_ab(self) -> None:
        self._showing_after_left = not self._showing_after_left
        self._paint_bases()

    # ------------------------------------------------------------------
    # 1:1 detail tiles
    # ------------------------------------------------------------------

    def _schedule_detail(self) -> None:
        self._detail_timer.start()

    def _load_detail(self) -> None:
        p = self._proposal
        if p is None or self._detail_job is not None:
            return
        if self._viewer.scale_factor() < DETAIL_SCALE:
            self._viewer.clear_details()
            return
        left_rect, right_rect = self._viewer.visible_scene_rects()
        job = self._track(BackgroundJob(
            target=_detail_target,
            kwargs={
                "proposal_id": p.id,
                "ticked": sorted(self._ticked),
                "left_box": _int_box(left_rect),
                "right_box": _int_box(right_rect),
                "left_is_after": self._showing_after_left,
            },
        ))
        job.signals.finished.connect(self._on_detail_ready)
        job.signals.failed.connect(self._on_detail_failed)
        self._detail_job = job
        job.start()

    def _on_detail_ready(self, payload: dict) -> None:
        self._detail_job = None
        p = self._proposal
        if p is None or payload.get("proposal_id") != p.id:
            return
        left = payload.get("left")
        right = payload.get("right")
        if left:
            pix = QPixmap(str(left["path"]))
            self._viewer.set_left_detail(pix, left["x"], left["y"])
        if right:
            pix = QPixmap(str(right["path"]))
            self._viewer.set_right_detail(pix, right["x"], right["y"])

    def _on_detail_failed(self, tb: str) -> None:
        self._detail_job = None
        log.warning("cleanup: detail tile failed\n%s", tb)

    # ------------------------------------------------------------------
    # Decisions
    # ------------------------------------------------------------------

    def _accept(self) -> None:
        p = self._proposal
        if p is None or self._manual_mode:
            return
        try:
            if p.is_split:
                if not self._confirm_split(p):
                    return
                res = split_mod.accept_split(self._settings, p.id)
                self._undo_stack.append(_UndoEntry("split", p.photo_id, p.id))
                self._say_action(res.summary())
            else:
                res = accept_mod.accept_proposal(
                    self._settings, p.id, ticked=sorted(self._ticked))
                self._undo_stack.append(_UndoEntry("accept", p.photo_id, p.id))
                self._say_action(res.summary())
        except Exception as e:
            log.exception("cleanup: accept failed")
            QMessageBox.critical(self, "Accept failed", str(e))
            self._say_action(f"Accept failed for photo {p.photo_id}: {e}")
            return
        self._advance_past_current()

    def _confirm_split(self, p: repo.Proposal) -> bool:
        n = len(p.split_regions or [])
        answer = QMessageBox.question(
            self, "Split this scan?",
            f"Photo #{p.photo_id} becomes {n} new photos.\n\n"
            f"The scan itself goes to quarantine as split_parent (restorable). "
            f"Album and group memberships are copied to every child; face "
            f"boxes follow the print they sit on, and any box straddling a cut "
            f"is soft-deleted.\n\nProceed?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        return answer == QMessageBox.Yes

    def _reject(self) -> None:
        p = self._proposal
        if p is None or self._manual_mode:
            return
        try:
            res = accept_mod.reject_proposal(self._settings, p.id)
        except Exception as e:
            log.exception("cleanup: reject failed")
            QMessageBox.critical(self, "Reject failed", str(e))
            return
        self._say_action(f"photo {p.photo_id} copied to {res.manual_path} "
                         f"and parked in the manual queue.")
        self._advance_past_current()

    def _remote(self) -> None:
        p = self._proposal
        if p is None or self._manual_mode:
            return
        if not self._provider.available:
            self._say(self._provider.unavailable_reason() or "Remote enhance is off.")
            return
        estimate = self._provider.cost_estimate(1)
        answer = QMessageBox.question(
            self, "Send to remote enhance?",
            f"Send photo #{p.photo_id} to {self._provider.name}?\n\n"
            f"Estimated cost ${estimate:.2f}. The result comes back as a new "
            f"proposal for review — nothing is accepted automatically.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self._say(f"Sending photo {p.photo_id} to {self._provider.name}…")
        self._job = self._track(BackgroundJob(
            target=_remote_target, kwargs={"proposal_id": p.id}))
        self._job.signals.finished.connect(self._on_remote_done)
        self._job.signals.failed.connect(self._on_remote_failed)
        self._progress.setVisible(True)
        self._progress.setRange(0, 0)
        self._job.start()

    def _on_remote_done(self, payload: dict) -> None:
        self._job = None
        self._progress.setVisible(False)
        self._session_spend += float(payload.get("cost_estimate_usd") or 0.0)
        self._say_action(payload.get("summary") or "Remote enhance returned.")
        self._refresh_queue()

    def _on_remote_failed(self, tb: str) -> None:
        self._job = None
        self._progress.setVisible(False)
        log.error("cleanup: remote enhance failed\n%s", tb)
        self._say("Remote enhance failed — see the log.")

    def _undo(self) -> None:
        if not self._undo_stack:
            self._say_action("Nothing to undo in this session. "
                             "See GC.md for the cross-restart recipe.")
            return
        entry = self._undo_stack.pop()
        try:
            if entry.kind == "split":
                res = split_mod.undo_split(self._settings, entry.photo_id)
                self._say_action(f"Split of photo {entry.photo_id} undone: "
                                 f"{len(res.children)} children soft-deleted, "
                                 f"{res.faces_returned} face boxes returned, "
                                 f"parent restored to {res.parent_status}.")
            else:
                res = accept_mod.undo_accept(self._settings, entry.photo_id)
                self._say_action(f"photo {entry.photo_id} restored to the pixels of "
                                 f"v{res.restored_version} (now v{res.new_version}); "
                                 f"{res.faces_restored} face boxes reverted.")
        except Exception as e:
            log.exception("cleanup: undo failed")
            QMessageBox.critical(self, "Undo failed", str(e))
            return
        self._refresh_queue()

    # ------------------------------------------------------------------
    # Bulk accept
    # ------------------------------------------------------------------

    def _bulk_accept(self, *, this_batch: bool) -> None:
        batches = self._selected_batches() if this_batch else None
        if this_batch and not batches and self._proposal is not None:
            batches = [self._proposal.scan_batch] if self._proposal.scan_batch else None
        ids = job_mod.geometric_only_ids(self._settings, batches=batches)
        scope = f"batch {batches[0]}" if batches else "the whole queue"
        if not ids:
            self._say(f"No geometric-only proposals in {scope}.")
            return
        answer = QMessageBox.question(
            self, "Bulk accept",
            f"Accept {len(ids)} geometric-only proposals in {scope}?\n\n"
            f"Deskew and crop only — nothing with a colour or levels op, no "
            f"splits, nothing flagged needs_manual. Each writes a new file "
            f"version; the previous one is kept in _versions/.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self._progress.setVisible(True)
        self._progress.setRange(0, len(ids))
        self._progress.setValue(0)
        self._cancel_btn.setEnabled(True)
        self._job = self._track(
            BackgroundJob(target=_bulk_target, kwargs={"ids": ids}))
        self._job.signals.progress.connect(self._on_job_progress)
        self._job.signals.finished.connect(self._on_bulk_done)
        self._job.signals.failed.connect(self._on_job_failed)
        self._job.start()

    def _on_bulk_done(self, payload: dict) -> None:
        self._job = None
        self._progress.setVisible(False)
        self._cancel_btn.setEnabled(False)
        self._say_action(payload.get("report") or "Bulk accept finished.")
        self._undo_stack.clear()
        self._refresh_queue()

    # ------------------------------------------------------------------
    # Analysis job
    # ------------------------------------------------------------------

    def _start_analysis(self) -> None:
        if self._job is not None:
            return
        batches = self._selected_batches()
        self._progress.setVisible(True)
        self._progress.setRange(0, 0)
        self._run_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)
        self._say("Probing the master roots, then analysing…")
        self._job = self._track(BackgroundJob(
            target=_analyse_target,
            kwargs={"reanalyse": self._reanalyse.isChecked(), "batches": batches},
        ))
        self._job.signals.progress.connect(self._on_job_progress)
        self._job.signals.finished.connect(self._on_analysis_done)
        self._job.signals.failed.connect(self._on_job_failed)
        self._job.start()

    def _on_job_progress(self, payload: dict) -> None:
        kind = payload.get("kind")
        if kind == "start":
            total = int(payload.get("total") or 0)
            self._progress.setRange(0, max(1, total))
            self._progress.setValue(0)
            self._say(f"Analysing {total} scans…")
        elif kind == "item":
            done = int(payload.get("done") or 0)
            self._progress.setValue(done)
            pid = payload.get("photo_id") or payload.get("proposal_id")
            status = payload.get("status")
            extra = payload.get("caption")
            ms = payload.get("ms")
            line = f"[{done}/{payload.get('total', '?')}] photo {pid}: {status}"
            if ms:
                line += f" ({ms} ms)"
            if extra:
                line += f" — {extra}"
            self._say(line)

    def _on_analysis_done(self, payload: dict) -> None:
        self._job = None
        self._progress.setVisible(False)
        self._run_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        self._say_action(payload.get("report") or "Analysis finished.")
        self._refresh_queue()

    def _on_job_failed(self, tb: str) -> None:
        self._job = None
        self._progress.setVisible(False)
        self._run_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        log.error("cleanup: job failed\n%s", tb)
        first = tb.strip().splitlines()[-1] if tb.strip() else "unknown error"
        if "MastersWritable" in tb:
            QMessageBox.critical(self, "Masters are writable", tb.split("\n\n", 1)[-1])
            self._say("Refused to run: a master root is writable. "
                      "Apply the icacls deny and try again.")
            return
        self._say(f"Job failed: {first}")

    def _cancel_job(self) -> None:
        if self._job is not None:
            self._job.cancel()
            self._say("Cancelling after the current photo…")

    def closeEvent(self, event) -> None:  # noqa: N802 — Qt naming
        """Cancel and join every background job before the panel goes away."""
        self._detail_timer.stop()
        for job in list(self._live_jobs):
            job.cancel()
        for job in list(self._live_jobs):
            job.wait(5000)
        self._live_jobs.clear()
        self._job = self._view_job = self._detail_job = None
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------

    def _write_report(self) -> None:
        self._say("Writing the report…")
        self._job = self._track(BackgroundJob(
            target=_report_target,
            kwargs={"batches": self._selected_batches()},
        ))
        self._job.signals.finished.connect(self._on_report_done)
        self._job.signals.failed.connect(self._on_job_failed)
        self._progress.setVisible(True)
        self._progress.setRange(0, 0)
        self._job.start()

    def _on_report_done(self, payload: dict) -> None:
        self._job = None
        self._progress.setVisible(False)
        self._say(f"Report written to {payload.get('directory')} "
                  f"(open contact-sheet.html).")


# --------------------------------------------------------------------------
# Worker targets — no widget touched in here
# --------------------------------------------------------------------------

def _int_box(rect) -> list[int]:
    return [int(rect.x()), int(rect.y()),
            max(1, int(rect.width())), max(1, int(rect.height()))]


def _clamp_box(box: list[int], w: int, h: int) -> tuple[int, int, int, int]:
    x, y, bw, bh = box
    if bw > DETAIL_MAX_PX:
        x += (bw - DETAIL_MAX_PX) // 2
        bw = DETAIL_MAX_PX
    if bh > DETAIL_MAX_PX:
        y += (bh - DETAIL_MAX_PX) // 2
        bh = DETAIL_MAX_PX
    x = max(0, min(x, max(0, w - 1)))
    y = max(0, min(y, max(0, h - 1)))
    return x, y, max(1, min(bw, w - x)), max(1, min(bh, h - y))


def _views_target(progress_cb, cancel_token, *, proposal_id: int,
                  ticked: list[str], **_) -> dict:
    settings = load_config()
    with db.connection() as conn:
        conn.autocommit = True
        p = repo.load_proposal(conn, proposal_id)
    if p is None:
        return {"proposal_id": proposal_id}
    src = p.resolved_path(settings)
    if src is None or not src.exists():
        return {"proposal_id": proposal_id}

    cpaths.ensure_dirs(settings)
    edge = settings.CLEANUP_ANALYSE_EDGE
    before = cpaths.preview_path(settings, p.photo_id, p.id).with_name(
        f"{p.photo_id:08d}_p{p.id}_before.jpg")
    from PIL import ImageOps
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        src_w, src_h = im.size
        thumb = im.copy()
        thumb.thumbnail((edge, edge), Image.LANCZOS)
        before.parent.mkdir(parents=True, exist_ok=True)
        thumb.save(before, "JPEG", quality=88, optimize=True)

    if p.is_split and p.split_regions:
        region = p.split_regions[0]
        plan = render_mod.Plan(transform=Transform.from_json(region["transform"]))
    elif "remote_enhance" in (p.operations.get("ops") or {}):
        op = (p.operations.get("ops") or {})["remote_enhance"]
        after = cpaths.preview_path(settings, p.photo_id, p.id)
        with Image.open(op["image_path"]) as im:
            im = im.convert("RGB")
            aw, ah = im.size
            im.thumbnail((edge, edge), Image.LANCZOS)
            im.save(after, "JPEG", quality=88, optimize=True)
        return {"proposal_id": proposal_id, "before": str(before),
                "after": str(after), "before_dims": [src_w, src_h],
                "after_dims": [aw, ah]}
    else:
        plan = render_mod.plan_from(p.operations, ticked, settings=settings,
                                   src_w=src_w, src_h=src_h)

    after = cpaths.preview_path(settings, p.photo_id, p.id)
    render_mod.render_preview(src, plan, after, edge=edge,
                              operations=p.operations)
    return {"proposal_id": proposal_id, "before": str(before), "after": str(after),
            "before_dims": [src_w, src_h],
            "after_dims": [plan.transform.out_w, plan.transform.out_h]}


def _detail_target(progress_cb, cancel_token, *, proposal_id: int,
                   ticked: list[str], left_box: list[int], right_box: list[int],
                   left_is_after: bool, **_) -> dict:
    """True 1:1 crops of the visible region — answer 5, no separate loupe."""
    settings = load_config()
    with db.connection() as conn:
        conn.autocommit = True
        p = repo.load_proposal(conn, proposal_id)
    if p is None:
        return {"proposal_id": proposal_id}
    src = p.resolved_path(settings)
    if src is None or not src.exists():
        return {"proposal_id": proposal_id}

    analysis = p.operations.get("analysis") or {}
    src_w = int(analysis.get("src_w") or p.width or 0)
    src_h = int(analysis.get("src_h") or p.height or 0)
    if p.is_split and p.split_regions:
        plan = render_mod.Plan(
            transform=Transform.from_json(p.split_regions[0]["transform"]))
    elif "remote_enhance" in (p.operations.get("ops") or {}):
        return {"proposal_id": proposal_id}
    else:
        plan = render_mod.plan_from(p.operations, ticked, settings=settings,
                                    src_w=src_w, src_h=src_h)

    out_dir = settings.CLEANUP_DIR / cpaths.PREVIEW_SUBDIR
    out_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"proposal_id": proposal_id}

    def _after_tile(box: list[int], name: str) -> dict:
        x, y, w, h = _clamp_box(box, plan.transform.out_w, plan.transform.out_h)
        img = render_mod.crop_at_full_res(src, plan, box=(x, y, w, h),
                                         operations=p.operations)
        path = out_dir / name
        img.save(path, "JPEG", quality=92)
        return {"path": str(path), "x": x, "y": y}

    def _before_tile(box: list[int], name: str) -> dict:
        x, y, w, h = _clamp_box(box, src_w, src_h)
        from PIL import ImageOps
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            img = im.crop((x, y, x + w, y + h))
        path = out_dir / name
        img.save(path, "JPEG", quality=92)
        return {"path": str(path), "x": x, "y": y}

    stem = f"{p.photo_id:08d}_p{p.id}"
    payload["right"] = _after_tile(right_box, f"{stem}_detail_right.jpg")
    payload["left"] = (_after_tile(left_box, f"{stem}_detail_left.jpg")
                       if left_is_after
                       else _before_tile(left_box, f"{stem}_detail_left.jpg"))
    return payload


def _analyse_target(progress_cb, cancel_token, *, reanalyse: bool,
                    batches: list[str] | None, **_) -> dict:
    settings = load_config()
    try:
        stats = job_mod.run_cleanup_analyse(
            settings, reanalyse=reanalyse, batches=batches,
            progress_cb=progress_cb, cancel_token=cancel_token,
        )
    except Cancelled:
        return {"report": "Analysis cancelled; re-run to continue where it stopped."}
    return {"report": stats.report(), "stats": stats.to_dict()}


def _bulk_target(progress_cb, cancel_token, *, ids: list[int], **_) -> dict:
    settings = load_config()
    try:
        stats = job_mod.bulk_accept_geometric(
            settings, ids, progress_cb=progress_cb, cancel_token=cancel_token,
        )
    except Cancelled:
        return {"report": "Bulk accept cancelled."}
    return {"report": stats.report()}


def _remote_target(progress_cb, cancel_token, *, proposal_id: int, **_) -> dict:
    settings = load_config()
    res = remote_mod.send_to_remote(settings, proposal_id)
    return {"summary": res.summary(),
            "cost_estimate_usd": res.cost_estimate_usd}


def _report_target(progress_cb, cancel_token, *, batches: list[str] | None,
                   **_) -> dict:
    settings = load_config()
    paths = report_mod.write_report(settings, batches=batches)
    return {"directory": str(paths.directory),
            "markdown": str(paths.markdown),
            "contact_sheet": str(paths.contact_sheet)}
