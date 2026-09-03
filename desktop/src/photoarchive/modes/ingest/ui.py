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
        self._recompute = QPushButton("Recompute scan order…")
        self._recompute.clicked.connect(self._on_recompute)
        self._rebuild = QPushButton("Rebuild back proposals…")
        self._rebuild.clicked.connect(self._on_rebuild)
        self._contact = QPushButton("Contact sheet")
        self._contact.clicked.connect(self._on_contact_sheet)
        self._review = QPushButton("Review proposals…")
        self._review.clicked.connect(self._open_review)
        controls.addWidget(self._start)
        controls.addWidget(self._cancel)
        controls.addStretch(1)
        controls.addWidget(self._recompute)
        controls.addWidget(self._rebuild)
        controls.addWidget(self._contact)
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

    def _on_recompute(self) -> None:
        roots = self._selected_roots()
        if not any(r.kind == "scan" for r in roots):
            QMessageBox.warning(
                self, "Recompute", "Select at least one scan-kind root.",
            )
            return
        reply = QMessageBox.question(
            self, "Recompute scan order",
            "Rewrite scan_sequence for every scan folder using file mtime\n"
            "(with natsort filename as tie-break, and falling back to\n"
            "natsort when mtimes are identical or absurdly wide).\n\n"
            "Also updates photo_backs and pending ingest_pairings.\n"
            "Accepted/rejected proposals are untouched.\n\n"
            "You'll usually want to rebuild back proposals afterwards.",
            QMessageBox.Ok | QMessageBox.Cancel,
        )
        if reply != QMessageBox.Ok:
            return
        from .recompute_order import recompute_scan_order

        self._start.setEnabled(False)
        self._rebuild.setEnabled(False)
        self._recompute.setEnabled(False)
        self._cancel.setEnabled(True)
        self._progress.setVisible(True)
        self._t0 = time.time()
        self._counts.setText("recomputing scan order…")
        self._last.setText("")

        def target(*, progress_cb, cancel_token, **_ignored):
            s = recompute_scan_order(
                settings=self._settings, roots=roots,
                progress_cb=progress_cb, cancel_token=cancel_token,
            )
            return s.as_dict()

        self._job = BackgroundJob(target)
        self._job.signals.progress.connect(self._on_recompute_progress)
        self._job.signals.finished.connect(self._on_recompute_finished)
        self._job.signals.failed.connect(self._on_failed)
        self._job.start()

    def _on_recompute_progress(self, payload: dict[str, Any]) -> None:
        if payload.get("kind") == "folder_done":
            self._last.setText(
                f"{payload.get('root')}/{payload.get('folder')}"
                + ("  (fallback)" if payload.get('fallback') else "")
            )

    def _on_recompute_finished(self, summary: dict) -> None:
        self._teardown_job()
        self._recompute.setEnabled(True)
        self._rebuild.setEnabled(True)
        text = (
            f"Folders processed: {summary.get('folders_processed', 0)}\n"
            f"  natsort fallback: {summary.get('folders_fallback', 0)}\n"
            f"  no change:        {summary.get('folders_unchanged', 0)}\n"
            f"Photos updated:            {summary.get('photos_updated', 0)}\n"
            f"Photo_backs updated:       {summary.get('photo_backs_updated', 0)}\n"
            f"Pending pairings updated:  {summary.get('ingest_pairings_updated', 0)}"
        )
        if summary.get("fallback_folders"):
            text += "\n\nFallback folders:\n  " + "\n  ".join(
                summary["fallback_folders"][:20]
            )
        self._counts.setText("scan order recomputed")
        QMessageBox.information(self, "Recompute complete", text)

    def _on_rebuild(self) -> None:
        roots = self._selected_roots()
        if not any(r.kind == "scan" for r in roots):
            QMessageBox.warning(
                self, "Rebuild",
                "Select at least one scan-kind root.",
            )
            return
        reply = QMessageBox.question(
            self, "Rebuild back proposals",
            "Re-score every scan file and refresh the pending pairing set.\n\n"
            "Existing accepted/rejected proposals are untouched. Rescan\n"
            "proposals are untouched. This may take several minutes on the\n"
            "full archive.",
            QMessageBox.Ok | QMessageBox.Cancel,
        )
        if reply != QMessageBox.Ok:
            return
        from .rebuild import rebuild_back_proposals

        self._start.setEnabled(False)
        self._rebuild.setEnabled(False)
        self._cancel.setEnabled(True)
        self._progress.setVisible(True)
        self._t0 = time.time()
        self._counts.setText("rebuilding…")
        self._last.setText("")

        def target(*, progress_cb, cancel_token, **_ignored):
            summary = rebuild_back_proposals(
                settings=self._settings, roots=roots,
                progress_cb=progress_cb, cancel_token=cancel_token,
            )
            return summary.as_dict()

        self._job = BackgroundJob(target)
        self._job.signals.progress.connect(self._on_rebuild_progress)
        self._job.signals.finished.connect(self._on_rebuild_finished)
        self._job.signals.failed.connect(self._on_failed)
        self._job.start()

    def _on_rebuild_progress(self, payload: dict[str, Any]) -> None:
        if payload.get("kind") == "scored":
            n = int(payload.get("n", 0))
            elapsed = max(time.time() - (self._t0 or time.time()), 0.001)
            self._rate.setText(f"{n} scored, {n / elapsed:.1f}/s over {elapsed:.0f}s")

    def _on_rebuild_finished(self, summary: dict) -> None:
        self._teardown_job()
        self._rebuild.setEnabled(True)
        text = _rebuild_summary_text(summary)
        self._counts.setText("rebuild complete")
        QMessageBox.information(self, "Rebuild complete", text)
        self._refresh_review_button()

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

    def _on_contact_sheet(self) -> None:
        from ...tools import contact_sheet as cs
        try:
            rows = cs._rows(self._settings)
        except Exception as e:
            log.exception("contact sheet load failed")
            QMessageBox.critical(self, "Contact sheet failed", str(e))
            return
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_path = cs.reports_dir() / f"backs-{ts}.html"
        cs.render_html(rows, out_path)
        import webbrowser
        webbrowser.open(out_path.as_uri())
        QMessageBox.information(
            self, "Contact sheet written",
            f"{len(rows)} proposals rendered to\n{out_path}\n\n"
            "Click thumbnails to mark 'not a back'. Save marked as JSON,\n"
            "then run:\n"
            "  python -m photoarchive.tools.reject_from_contact <that.json>"
        )

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


def _rebuild_summary_text(s: dict) -> str:
    lines = [
        f"Files scored: {s.get('scan_files_scored', 0)}",
        f"Pending kept:   {s.get('pending_kept', 0)}",
        f"Pending dropped: {s.get('pending_dropped', 0)}",
        f"Photo-as-back proposed: {s.get('photo_as_back_proposed', 0)}",
        f"  (skipped-rejected {s.get('photo_as_back_skipped_rejected', 0)},"
        f" no-predecessor {s.get('photo_as_back_skipped_no_predecessor', 0)},"
        f" predecessor-is-back {s.get('photo_as_back_skipped_predecessor_is_back', 0)},"
        f" aspect {s.get('photo_as_back_skipped_aspect', 0)})",
        f"Total pending after: {s.get('total_pending_after', 0)}",
        "",
        "Score histogram (0.1 buckets, all scored files):",
    ]
    for bucket, n in (s.get("histogram") or {}).items():
        lines.append(f"  {bucket}  {'█' * min(n, 60)}  ({n})")
    return "\n".join(lines)


def _summary_text(s: dict) -> str:
    lines = [f"Elapsed: {s.get('elapsed', 0):.1f}s"]
    for label, c in (s.get("counts_by_root") or {}).items():
        lines.append(
            f"  {label}: new={c.get('new',0)} dupe={c.get('skipped_dupe',0)} "
            f"video={c.get('skipped_video',0)} backs={c.get('backs_proposed',0)} "
            f"rescans={c.get('rescans_proposed',0)} failed={c.get('failed',0)}"
        )
    return "\n".join(lines)
