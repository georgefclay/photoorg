from __future__ import annotations

import logging
import time
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ... import db
from ...config import load as load_config
from ...workers import BackgroundJob
from .review_grid import ReviewGridDialog
from .service import GuardRefused, IngestSummary, run_ingest

log = logging.getLogger(__name__)


class IngestPanel(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._settings = load_config()
        self._job: BackgroundJob | None = None
        self._t0: float | None = None

        outer = QVBoxLayout(self)

        outer.addWidget(QLabel("Ingest"))

        # Root selector
        roots_box = QGroupBox("Roots")
        rlay = QVBoxLayout(roots_box)
        self._root_checks: list[QCheckBox] = []
        for r in self._settings.master_roots:
            cb = QCheckBox(f"{r.label} ({r.kind}): {r.path}")
            cb.setChecked(True)
            self._root_checks.append(cb)
            rlay.addWidget(cb)
        outer.addWidget(roots_box)

        # Controls
        controls = QHBoxLayout()
        self._start = QPushButton("Start")
        self._start.clicked.connect(self._on_start)
        self._cancel = QPushButton("Cancel")
        self._cancel.setEnabled(False)
        self._cancel.clicked.connect(self._on_cancel)
        self._review = QPushButton("Review proposals…")
        self._review.clicked.connect(self._open_review)
        controls.addWidget(self._start)
        controls.addWidget(self._cancel)
        controls.addStretch(1)
        controls.addWidget(self._review)
        outer.addLayout(controls)

        # Progress + counts
        self._progress = QProgressBar()
        self._progress.setRange(0, 0)  # indeterminate until first update
        self._progress.setVisible(False)
        outer.addWidget(self._progress)

        self._rate = QLabel("")
        outer.addWidget(self._rate)

        self._counts = QLabel("")
        self._counts.setTextFormat(Qt.PlainText)
        self._counts.setStyleSheet("font-family: Consolas, monospace")
        outer.addWidget(self._counts)

        self._last = QLabel("")
        self._last.setStyleSheet("color: gray")
        outer.addWidget(self._last)

        outer.addStretch(1)

        # Refresh review-button label periodically to show pending count.
        self._tick = QTimer(self)
        self._tick.setInterval(1500)
        self._tick.timeout.connect(self._refresh_review_button)
        self._tick.start()
        self._refresh_review_button()

    def _selected_roots(self) -> list:
        picked_labels = {
            cb.text().split(" ", 1)[0]
            for cb, _ in zip(self._root_checks, self._settings.master_roots)
            if cb.isChecked()
        }
        return [r for r in self._settings.master_roots if r.label in picked_labels]

    def _on_start(self) -> None:
        roots = self._selected_roots()
        if not roots:
            QMessageBox.warning(self, "Ingest", "Select at least one root.")
            return
        self._start.setEnabled(False)
        self._cancel.setEnabled(True)
        self._progress.setVisible(True)
        self._t0 = time.time()
        self._counts.setText("running…")
        self._last.setText("")
        self._job = BackgroundJob(run_ingest, kwargs={
            "settings": self._settings, "roots": roots,
        })
        self._job.signals.progress.connect(self._on_progress)
        self._job.signals.finished.connect(self._on_finished)
        self._job.signals.failed.connect(self._on_failed)
        self._job.start()

    def _on_cancel(self) -> None:
        if self._job:
            self._cancel.setEnabled(False)
            self._counts.setText("cancelling — will stop after current file…")
            self._job.cancel()

    def _on_progress(self, payload: dict[str, Any]) -> None:
        if payload.get("kind") != "counts":
            return
        c = payload.get("counts") or {}
        seen = sum(int(c.get(k, 0)) for k in
                   ("new", "skipped_dupe", "skipped_video",
                    "backs_proposed", "rescans_proposed", "failed"))
        elapsed = max(time.time() - (self._t0 or time.time()), 0.001)
        rate = seen / elapsed
        self._rate.setText(f"{seen} files, {rate:.1f}/s over {elapsed:.0f}s")
        self._counts.setText(
            f"root={payload.get('root'):<8} new={c.get('new',0):>6} "
            f"dupe={c.get('skipped_dupe',0):>6} video={c.get('skipped_video',0):>4} "
            f"backs={c.get('backs_proposed',0):>4} rescans={c.get('rescans_proposed',0):>4} "
            f"failed={c.get('failed',0):>4}"
        )
        self._last.setText(str(payload.get("last") or ""))

    def _on_finished(self, summary_dict: dict) -> None:
        self._teardown_job()
        if summary_dict.get("cancelled"):
            self._counts.setText("cancelled.")
        else:
            totals = summary_dict.get("totals", {})
            elapsed = summary_dict.get("elapsed", 0.0)
            self._counts.setText(
                f"done in {elapsed:.1f}s: " + " ".join(
                    f"{k}={v}" for k, v in totals.items()
                )
            )
            QMessageBox.information(
                self, "Ingest complete",
                _summary_text(summary_dict),
            )
        self._refresh_review_button()

    def _on_failed(self, tb: str) -> None:
        self._teardown_job()
        # Distinguish GuardRefused (a helpful, non-scary message) from other
        # crashes.
        if "GuardRefused" in tb:
            QMessageBox.critical(self, "Masters guard",
                                 tb.splitlines()[-1] if tb else "Guard refused.")
        else:
            QMessageBox.critical(self, "Ingest failed", tb[-2000:])
        self._counts.setText("failed — see log for details.")

    def _teardown_job(self) -> None:
        self._job = None
        self._start.setEnabled(True)
        self._cancel.setEnabled(False)
        self._progress.setVisible(False)

    def _refresh_review_button(self) -> None:
        try:
            with db.connection() as conn:
                pairings = conn.execute(
                    "select count(*) from ingest_pairings where status = 'pending'"
                ).fetchone()[0]
                rescans = conn.execute(
                    "select count(*) from ingest_rescans where status = 'pending'"
                ).fetchone()[0]
            total = pairings + rescans
            self._review.setEnabled(total > 0)
            self._review.setText(
                f"Review proposals ({pairings} backs, {rescans} rescans)…"
                if total else "Review proposals…"
            )
        except Exception:
            self._review.setEnabled(False)

    def _open_review(self) -> None:
        dlg = ReviewGridDialog(self._settings, self)
        dlg.exec()
        self._refresh_review_button()


def _summary_text(s: dict) -> str:
    lines = [f"Elapsed: {s.get('elapsed', 0):.1f}s"]
    for label, c in (s.get("counts_by_root") or {}).items():
        lines.append(
            f"  {label}: new={c.get('new',0)} dupe={c.get('skipped_dupe',0)} "
            f"video={c.get('skipped_video',0)} backs={c.get('backs_proposed',0)} "
            f"rescans={c.get('rescans_proposed',0)} failed={c.get('failed',0)}"
        )
    return "\n".join(lines)
