"""Jobs mode — the sidebar panel that owns the batch runner.

Per job: eligible / uploaded on mini / processed on mini / collected in DB,
plus mini ETA and blackout indicator. Run / Collect / Cancel buttons.
Health status (model name, green/red dot) is polled every 30 seconds via a
QTimer on its own worker so the GUI thread never blocks.
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...config import load as load_settings
from ...inference_client import QUEUE_ORDER, shared
from ...jobs import all_jobs, get_job
from ...jobs.base import JobContext, collect, hand_over, has_handover, reconcile_handovers
from ...workers import BackgroundJob, CancelToken
from . import stats as stats_mod

log = logging.getLogger(__name__)


HEALTH_POLL_MS = 30_000
COLLECT_POLL_MS = 5 * 60 * 1000
LOCAL_STATS_POLL_MS = 5_000


class JobsPanel(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._jobs = all_jobs()
        self._active_worker: BackgroundJob | None = None

        outer = QVBoxLayout(self)

        # Startup banner — flags the two common misconfigurations before a
        # single call goes out. Populated in _refresh_token_banner().
        self.banner = QLabel("")
        self.banner.setWordWrap(True)
        self.banner.setVisible(False)
        outer.addWidget(self.banner)
        self._refresh_token_banner()

        # Header row: health indicator + Run-all / Collect-now buttons.
        header = QHBoxLayout()
        self.health_dot = QLabel()
        self.health_dot.setFixedSize(14, 14)
        self.health_label = QLabel("Inference: unknown")
        header.addWidget(self.health_dot)
        header.addWidget(self.health_label)
        header.addStretch(1)

        self.run_all_btn = QPushButton("Hand over all jobs")
        self.run_all_btn.clicked.connect(self._run_all_clicked)
        self.collect_btn = QPushButton("Collect now")
        self.collect_btn.clicked.connect(self._collect_now_clicked)
        header.addWidget(self.run_all_btn)
        header.addWidget(self.collect_btn)
        outer.addLayout(header)

        # Model / prompt_version input row — for a re-run we may need to
        # override what the writer records. Default to whatever /health tells us.
        opts = QHBoxLayout()
        opts.addWidget(QLabel("Model:"))
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setMinimumWidth(240)
        opts.addWidget(self.model_combo)
        opts.addWidget(QLabel("Prompt version:"))
        self.prompt_combo = QComboBox()
        self.prompt_combo.setEditable(True)
        self.prompt_combo.setMinimumWidth(160)
        self.prompt_combo.addItems(["", "v1", "v2"])
        opts.addWidget(self.prompt_combo)
        self.sweep_check = QCheckBox("Sweep inbox after collect")
        self.sweep_check.setChecked(True)
        opts.addWidget(self.sweep_check)
        opts.addStretch(1)
        outer.addLayout(opts)

        # Table of jobs
        self.table = QTableWidget(len(self._jobs), 8)
        self.table.setHorizontalHeaderLabels([
            "Job", "Eligible", "Uploaded",
            "Processed on mini", "Collected", "ETA", "Blackout", "Actions",
        ])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        hh = self.table.horizontalHeader()
        for i in range(7):
            hh.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(7, QHeaderView.Stretch)
        outer.addWidget(self.table, 1)

        # Populate the job name column and action buttons.
        for row, job in enumerate(self._jobs):
            self.table.setItem(row, 0, QTableWidgetItem(job.name))
            actions = QWidget()
            hl = QHBoxLayout(actions)
            hl.setContentsMargins(0, 0, 0, 0)
            run_btn = QPushButton("Run")
            run_btn.clicked.connect(lambda _=None, n=job.name: self._run_one_clicked(n))
            collect_btn = QPushButton("Collect")
            collect_btn.clicked.connect(lambda _=None, n=job.name: self._collect_one_clicked(n))
            cancel_btn = QPushButton("Cancel")
            cancel_btn.clicked.connect(lambda _=None, n=job.name: self._cancel_one_clicked(n))
            hl.addWidget(run_btn)
            hl.addWidget(collect_btn)
            hl.addWidget(cancel_btn)
            hl.addStretch(1)
            self.table.setCellWidget(row, 7, actions)

        # Progress line at the bottom
        self.status_line = QLabel("")
        self.status_line.setWordWrap(True)
        outer.addWidget(self.status_line)

        # Timers
        self._health_timer = QTimer(self)
        self._health_timer.timeout.connect(self._refresh_health)
        self._health_timer.start(HEALTH_POLL_MS)

        self._collect_timer = QTimer(self)
        self._collect_timer.timeout.connect(self._auto_collect)
        self._collect_timer.start(COLLECT_POLL_MS)

        self._local_timer = QTimer(self)
        self._local_timer.timeout.connect(self._refresh_local_stats)
        self._local_timer.start(LOCAL_STATS_POLL_MS)

        # Fire once after the event loop is up.
        QTimer.singleShot(0, self._refresh_health)
        QTimer.singleShot(0, self._refresh_local_stats)
        QTimer.singleShot(200, self._reconcile_handovers_bg)
        QTimer.singleShot(500, self._auto_collect)

    # --- health --------------------------------------------------------------

    def _refresh_health(self) -> None:
        def _target(progress_cb, cancel_token):
            try:
                hs = shared.client().health()
                return {"ok": True, "health": hs}
            except Exception as e:
                return {"ok": False, "error": str(e)}

        job = BackgroundJob(_target)
        job.signals.finished.connect(self._on_health)
        job.signals.failed.connect(lambda _msg: self._paint_health(False, "unreachable"))
        job.start()
        # Keep a reference so it isn't garbage-collected mid-run
        self._health_worker = job

    def _on_health(self, summary: dict[str, Any]) -> None:
        if not summary.get("ok"):
            self._paint_health("red", summary.get("error", "down"))
            return
        hs = summary["health"]
        if not hs.token_ok:
            self._paint_health("amber", "bad token (401 on authenticated probe)")
            return
        name = hs.model_name or "unknown"
        blackout = " · BLACKOUT" if hs.blackout_active else ""
        self._paint_health("green", f"{name}{blackout}")
        if name and self.model_combo.findText(name) < 0:
            self.model_combo.addItem(name)
        if not self.model_combo.currentText():
            self.model_combo.setCurrentText(name)
        # Fold /health's per-job inbox counts into the table so "Uploaded"
        # reflects what's actually queued on the mini.
        for row, job in enumerate(self._jobs):
            pending = (hs.inbox or {}).get(job.name)
            if pending is None:
                continue
            self.table.setItem(row, 2, QTableWidgetItem(str(pending)))
            self.table.setItem(
                row, 6,
                QTableWidgetItem("BLACKOUT" if hs.blackout_active else "off"),
            )

    def _paint_health(self, state: str, label: str) -> None:
        """state ∈ 'green' (reachable + token accepted) | 'amber' (reachable
        but token rejected) | 'red' (unreachable / other error)."""
        colour = {
            "green": "#2fbf2f",
            "amber": "#e0a020",
            "red":   "#d43f3f",
        }.get(state, "#d43f3f")
        pm = QPixmap(14, 14)
        pm.fill(Qt.transparent)
        from PySide6.QtGui import QPainter
        p = QPainter(pm)
        p.setBrush(QColor(colour))
        p.setPen(Qt.NoPen)
        p.drawEllipse(0, 0, 14, 14)
        p.end()
        self.health_dot.setPixmap(pm)
        self.health_label.setText(f"Inference: {label}")

    def _reconcile_handovers_bg(self) -> None:
        def _target(progress_cb, cancel_token):
            try:
                return {"result": reconcile_handovers(shared.client())}
            except Exception as e:
                return {"error": str(e)}
        job = BackgroundJob(_target)
        def _on_done(summary):
            outcomes = summary.get("result") or {}
            promoted = [k for k, v in outcomes.items() if v == "promoted"]
            if promoted:
                log.info("reconciled hand-overs from mini summary: %s", promoted)
                self._refresh_local_stats()
        job.signals.finished.connect(_on_done)
        job.signals.failed.connect(lambda tb: log.debug("reconcile: %s", tb))
        self._reconcile_worker = job
        job.start()

    def _refresh_token_banner(self) -> None:
        try:
            settings = load_settings()
        except Exception as e:
            self.banner.setStyleSheet("color: white; background: #d43f3f; padding: 6px")
            self.banner.setText(f"Configuration error: {e}")
            self.banner.setVisible(True)
            return
        token = (settings.INFERENCE_TOKEN or "").strip()
        if not token or token == "CHANGEME":
            self.banner.setStyleSheet(
                "color: black; background: #ffe08a; padding: 6px; font-weight: bold"
            )
            self.banner.setText(
                "INFERENCE_TOKEN is not set (still 'CHANGEME'). "
                "Copy the value from the Mac mini's inference/.env into "
                "desktop/.env and restart the app."
            )
            self.banner.setVisible(True)
        else:
            self.banner.setVisible(False)

    # --- local stats ---------------------------------------------------------

    def _refresh_local_stats(self) -> None:
        try:
            stats = stats_mod.load_local_stats()
        except Exception as e:
            log.warning("jobs panel: local stats failed: %s", e)
            return
        for row, job in enumerate(self._jobs):
            s = stats.get(job.name)
            if s is None:
                continue
            self.table.setItem(row, 1, QTableWidgetItem(str(s.eligible)))
            uploaded = "-" if s.inbox_total is None else str(s.inbox_total)
            processed = "-" if s.processed_on_mini is None else str(s.processed_on_mini)
            eta = "-" if s.eta_seconds is None else _fmt_eta(s.eta_seconds)
            blackout = "-" if s.blackout_active is None else ("BLACKOUT" if s.blackout_active else "off")
            self.table.setItem(row, 2, QTableWidgetItem(uploaded))
            self.table.setItem(row, 3, QTableWidgetItem(processed))
            self.table.setItem(row, 4, QTableWidgetItem(str(s.done_in_db)))
            self.table.setItem(row, 5, QTableWidgetItem(eta))
            self.table.setItem(row, 6, QTableWidgetItem(blackout))

    # --- run all -------------------------------------------------------------

    def _run_all_clicked(self) -> None:
        if not self._confirm_service_reachable():
            return
        if self._active_worker is not None:
            QMessageBox.information(self, "Busy", "A job is already running.")
            return
        confirm = QMessageBox.question(
            self, "Hand over all jobs",
            "Hand over all five jobs in queue order (backs → faces → classify → "
            "describe → estimate_date)? Uploads happen sequentially; the mini "
            "processes serially thereafter.",
        )
        if confirm != QMessageBox.Yes:
            return

        ctx = self._make_context()

        def _target(progress_cb, cancel_token):
            summaries = []
            for name in QUEUE_ORDER:
                if cancel_token.is_set():
                    break
                job = get_job(name)
                s = hand_over(job, ctx, progress_cb=progress_cb)
                summaries.append({"job": name, "selected": s.selected,
                                  "uploaded": s.uploaded, "started": s.started})
            return {"summaries": summaries}

        self._start_worker(_target, "hand_over_all")

    def _run_one_clicked(self, name: str) -> None:
        if not self._confirm_service_reachable():
            return
        if self._active_worker is not None:
            QMessageBox.information(self, "Busy", "A job is already running.")
            return
        ctx = self._make_context()
        job = get_job(name)

        def _target(progress_cb, cancel_token):
            ctx.cancel = cancel_token
            s = hand_over(job, ctx, progress_cb=progress_cb)
            return {"job": name, "selected": s.selected, "uploaded": s.uploaded, "started": s.started}

        self._start_worker(_target, f"hand_over:{name}")

    def _collect_now_clicked(self) -> None:
        if self._active_worker is not None:
            QMessageBox.information(self, "Busy", "A job is already running.")
            return
        self._auto_collect(force=True)

    def _collect_one_clicked(self, name: str) -> None:
        if self._active_worker is not None:
            QMessageBox.information(self, "Busy", "A job is already running.")
            return
        ctx = self._make_context()
        job = get_job(name)
        sweep = self.sweep_check.isChecked()

        def _target(progress_cb, cancel_token):
            ctx.cancel = cancel_token
            s = collect(job, ctx, progress_cb=progress_cb, sweep_when_done=sweep)
            return {"job": name, "written": s.written, "skipped": s.skipped,
                    "failed": s.failed, "swept": s.swept}

        self._start_worker(_target, f"collect:{name}")

    def _auto_collect(self, force: bool = False) -> None:
        if self._active_worker is not None and not force:
            return
        if self._active_worker is not None:
            return
        ctx = self._make_context()
        sweep = self.sweep_check.isChecked()

        def _target(progress_cb, cancel_token):
            ctx.cancel = cancel_token
            summaries = []
            for name in QUEUE_ORDER:
                if cancel_token.is_set():
                    break
                if not has_handover(name):
                    log.debug("auto_collect: skipping %s (no hand-over yet)", name)
                    summaries.append({"job": name, "skipped": "no_handover"})
                    continue
                try:
                    job = get_job(name)
                    s = collect(job, ctx, progress_cb=progress_cb, sweep_when_done=sweep)
                    summaries.append({
                        "job": name, "written": s.written, "skipped": s.skipped,
                        "failed": s.failed, "swept": s.swept, "cursor": s.cursor_after,
                    })
                except Exception as e:
                    log.warning("auto_collect(%s) failed: %s", name, e)
                    summaries.append({"job": name, "error": str(e)})
            return {"summaries": summaries}

        self._start_worker(_target, "auto_collect")

    def _cancel_one_clicked(self, name: str) -> None:
        confirm = QMessageBox.question(
            self, "Cancel", f"Ask the mini to cancel {name!r}? Running item finishes.",
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            ok = shared.client().cancel(name)
            self.status_line.setText(f"{name}: cancel {'accepted' if ok else 'refused'}")
        except Exception as e:
            self.status_line.setText(f"{name}: cancel failed — {e}")

    # --- worker plumbing ----------------------------------------------------

    def _make_context(self) -> JobContext:
        model = self.model_combo.currentText().strip() or "unknown"
        prompt = self.prompt_combo.currentText().strip() or None
        return JobContext(client=shared.client(), model=model, prompt_version=prompt)

    def _start_worker(self, target, kind: str) -> None:
        worker = BackgroundJob(target)
        worker.signals.progress.connect(self._on_progress)
        worker.signals.finished.connect(lambda s: self._on_finished(kind, s))
        worker.signals.failed.connect(lambda tb: self._on_failed(kind, tb))
        self._active_worker = worker
        self.status_line.setText(f"{kind}: starting…")
        worker.start()

    def _on_progress(self, msg: dict[str, Any]) -> None:
        # Refresh the eligible/collected columns after every writer.
        if msg.get("kind") == "wrote_line":
            self._refresh_local_stats()
        kind = msg.get("kind", "?")
        job = msg.get("job_name", "")
        self.status_line.setText(f"{job} · {kind} · {msg}")

    def _on_finished(self, kind: str, summary: dict[str, Any]) -> None:
        self._active_worker = None
        self.status_line.setText(f"{kind}: done — {summary}")
        self._refresh_local_stats()

    def _on_failed(self, kind: str, tb: str) -> None:
        self._active_worker = None
        self.status_line.setText(f"{kind}: FAILED — see log dock")
        log.error("%s failed:\n%s", kind, tb)

    def _confirm_service_reachable(self) -> bool:
        # Best-effort: /health is fast and doesn't need a token.
        try:
            hs = shared.client().health()
            if hs.ok:
                return True
        except Exception:
            pass
        return QMessageBox.question(
            self, "Service unreachable",
            "Cannot reach the inference service. Proceed anyway?",
        ) == QMessageBox.Yes


def _fmt_eta(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds/60:.0f}m"
    if seconds < 3600 * 48:
        return f"{seconds/3600:.1f}h"
    return f"{seconds/86400:.1f}d"
