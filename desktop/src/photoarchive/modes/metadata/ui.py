"""Metadata mode: Run / Dry run, progress, the last run, and "What's embedded".

The run itself is the CLI (`python -m photoarchive.tools.write_metadata`)
started as a QProcess, so the panel and the command line are one code path
and the chunks still get their fresh processes. There is one runner for the
whole app (`MetadataRunner.shared()`): the Sync tab's "Write metadata for N
changed photos" button uses the same one, so two writers can never race on
the same files, and Sync refuses to push while it is running.

Two Phase 7 UI rules hold here too: a running job is never dropped (the
runner outlives the panel and is killed and waited for on quit), and the
status line has two halves — the left is the last result and stays until
the next one, the right is context.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QProcess, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QProgressBar, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem,
    QTextEdit, QVBoxLayout, QWidget,
)

from ... import db
from ...config import Settings, load as load_settings
from ...workers import BackgroundJob
from . import exiftool as et_mod
from . import repo
from .run import JOB_NAME
from ..ingest.paths import resolve_working_path

log = logging.getLogger(__name__)

_DUE_RE = re.compile(r"(\d+) photos in scope; (\d+) due")
_PROGRESS_RE = re.compile(r"^progress: (\d+)/(\d+)")
_ITEM_RE = re.compile(r"^\s*(\[\d+/\d+\] )?photo \d+")


# --------------------------------------------------------------------------
# The one runner
# --------------------------------------------------------------------------

class MetadataRunner(QObject):
    line = Signal(str)
    progress = Signal(int, int)      # done, total
    finished = Signal(int, str)      # exit code, last summary text
    started = Signal(bool)           # dry_run

    _instance: "MetadataRunner | None" = None

    @classmethod
    def shared(cls) -> "MetadataRunner":
        if cls._instance is None:
            cls._instance = cls()
            app = QApplication.instance()
            if app is not None:
                app.aboutToQuit.connect(cls._instance.stop_and_wait)
        return cls._instance

    def __init__(self) -> None:
        super().__init__()
        self._proc: QProcess | None = None
        self._buf = ""
        self._total = 0
        self._done = 0
        self._tail: list[str] = []

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.state() != QProcess.NotRunning

    def start(self, *, dry_run: bool, batch: str | None = None,
              limit: int | None = None, photo_ids: list[int] | None = None) -> bool:
        if self.is_running():
            return False
        args = ["-u", "-m", "photoarchive.tools.write_metadata"]
        if dry_run:
            args.append("--dry-run")
        if batch:
            args += ["--batch", batch]
        if limit:
            args += ["--limit", str(limit)]
        for pid in photo_ids or []:
            args += ["--photo", str(pid)]
        self._buf, self._total, self._done, self._tail = "", 0, 0, []
        proc = QProcess(self)
        proc.setWorkingDirectory(os.getcwd())
        proc.setProcessChannelMode(QProcess.MergedChannels)
        proc.readyReadStandardOutput.connect(self._read)
        proc.finished.connect(self._done_cb)
        self._proc = proc
        proc.start(sys.executable, args)
        self.started.emit(dry_run)
        return True

    def stop_and_wait(self) -> None:
        if self.is_running():
            assert self._proc is not None
            self._proc.kill()
            self._proc.waitForFinished(10_000)

    def _read(self) -> None:
        assert self._proc is not None
        self._consume(bytes(self._proc.readAllStandardOutput()).decode("utf-8", "replace"))

    def _consume(self, text: str) -> None:
        self._buf += text
        *lines, self._buf = self._buf.split("\n")
        for raw in lines:
            ln = raw.rstrip("\r")
            self._tail = (self._tail + [ln])[-8:]
            self.line.emit(ln)
            m = _DUE_RE.search(ln)
            if m:
                self._total = int(m.group(2))
                self.progress.emit(0, self._total)
                continue
            m = _PROGRESS_RE.match(ln)
            if m:
                self._done, self._total = int(m.group(1)), int(m.group(2))
                self.progress.emit(self._done, self._total)
                continue
            if _ITEM_RE.match(ln) and self._total:
                self._done = min(self._total, self._done + 1)
                self.progress.emit(self._done, self._total)

    def _done_cb(self, code: int, _status: Any = None) -> None:
        if self._buf:
            self.line.emit(self._buf)
            self._tail.append(self._buf)
            self._buf = ""
        summary = "\n".join(t for t in self._tail if t.strip())
        self._proc = None
        self.finished.emit(int(code), summary)


def due_text(n: int, in_group: int) -> str:
    if n == 0:
        return "Metadata: every working copy is up to date."
    return (f"Metadata: {n} photos changed since their last write. Writing them "
            f"gives each a new file_version, so {n} files will re-push — "
            f"{in_group} on the next push (they are in a group), the rest when "
            "their group is assigned.")


def count_due_job(progress_cb=None, cancel_token=None) -> dict[str, Any]:
    with db.connection() as conn:
        conn.autocommit = True
        n, g = repo.count_due(conn)
    return {"due": n, "in_group": g}


# --------------------------------------------------------------------------
# The panel
# --------------------------------------------------------------------------

class MetadataPanel(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._settings: Settings | None = None
        self._runner = MetadataRunner.shared()
        self._jobs: set[BackgroundJob] = set()

        root = QVBoxLayout(self)
        intro = QLabel(
            "Writes the settled truth — confirmed date, people and their face "
            "regions, place, the back of the print, the scan locator and the "
            "archive identity — into each working copy. Facts only; masters are "
            "never touched. Only photos whose facts changed (or whose file was "
            "replaced) are written; every write keeps the previous file in "
            "_versions/ and bumps file_version, so sync re-pushes it."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        row = QHBoxLayout()
        row.addWidget(QLabel("Batch:"))
        self._batch = QLineEdit()
        self._batch.setPlaceholderText("all (or e.g. Batch 00012)")
        row.addWidget(self._batch)
        row.addWidget(QLabel("Limit:"))
        self._limit = QSpinBox()
        self._limit.setRange(0, 100_000)
        self._limit.setSpecialValueText("none")
        row.addWidget(self._limit)
        self._btn_dry = QPushButton("Dry run")
        self._btn_dry.clicked.connect(lambda: self._start(dry_run=True))
        row.addWidget(self._btn_dry)
        self._btn_run = QPushButton("Run")
        self._btn_run.clicked.connect(lambda: self._start(dry_run=False))
        row.addWidget(self._btn_run)
        self._btn_stop = QPushButton("Stop")
        self._btn_stop.clicked.connect(self._runner.stop_and_wait)
        row.addWidget(self._btn_stop)
        root.addLayout(row)

        due_row = QHBoxLayout()
        self._due = QLabel("")
        self._due.setWordWrap(True)
        due_row.addWidget(self._due, stretch=1)
        b = QPushButton("Refresh count")
        b.clicked.connect(self._refresh_due)
        due_row.addWidget(b)
        root.addLayout(due_row)

        self._bar = QProgressBar()
        root.addWidget(self._bar)
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.document().setMaximumBlockCount(5000)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.Monospace)
        self._log.setFont(mono)
        root.addWidget(self._log, stretch=2)

        last = QHBoxLayout()
        self._last = QLabel("")
        self._last.setWordWrap(True)
        last.addWidget(self._last, stretch=1)
        self._btn_report = QPushButton("Open report folder")
        self._btn_report.clicked.connect(self._open_report)
        last.addWidget(self._btn_report)
        root.addLayout(last)
        self._report_dir: str | None = None

        root.addWidget(self._make_viewer(), stretch=2)

        status = QHBoxLayout()
        self._status_left = QLabel("")
        self._status_right = QLabel("")
        self._status_right.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        status.addWidget(self._status_left, stretch=1)
        status.addWidget(self._status_right, stretch=1)
        root.addLayout(status)

        self._runner.line.connect(self._log.append)
        self._runner.progress.connect(self._on_progress)
        self._runner.finished.connect(self._on_finished)
        self._runner.started.connect(lambda dry: self._sync_buttons())
        self._sync_buttons()
        self._load_last_run()
        self._refresh_due()

    # ---- run ---------------------------------------------------------------

    def _start(self, *, dry_run: bool) -> None:
        try:
            et_mod.find_exiftool(self._settings_lazy().EXIFTOOL_PATH)
        except et_mod.ExifToolMissing as e:
            self._status_left.setText(str(e))
            return
        self._log.clear()
        self._bar.setRange(0, 0)
        ok = self._runner.start(dry_run=dry_run, batch=self._batch.text().strip() or None,
                                limit=self._limit.value() or None)
        if not ok:
            self._status_right.setText("A metadata run is already going.")
            return
        self._status_right.setText("Dry run…" if dry_run else "Writing…")

    def _on_progress(self, done: int, total: int) -> None:
        if total:
            self._bar.setRange(0, total)
            self._bar.setValue(done)
            self._status_right.setText(f"{done} / {total}")

    def _on_finished(self, code: int, summary: str) -> None:
        self._bar.setRange(0, 1)
        self._bar.setValue(1)
        self._status_left.setText(("Finished" if code == 0 else f"Exited {code}")
                                  + " — " + (summary.splitlines()[-1] if summary else ""))
        self._status_right.setText("")
        self._sync_buttons()
        self._load_last_run()
        self._refresh_due()

    def _sync_buttons(self) -> None:
        running = self._runner.is_running()
        self._btn_dry.setEnabled(not running)
        self._btn_run.setEnabled(not running)
        self._btn_stop.setEnabled(running)

    def _refresh_due(self) -> None:
        job = BackgroundJob(count_due_job)
        self._jobs.add(job)
        job.signals.finished.connect(
            lambda r: self._due.setText(due_text(r.get("due", 0), r.get("in_group", 0))))
        job.signals.failed.connect(lambda tb: self._due.setText("Could not count: "
                                                                + tb.splitlines()[-1]))
        job.finished.connect(lambda: self._jobs.discard(job))
        job.start()

    def _load_last_run(self) -> None:
        try:
            with db.connection() as conn:
                conn.autocommit = True
                row = conn.execute("""
                    select finished_at, status, params from job_runs
                     where job_name = %s order by id desc limit 1
                """, (JOB_NAME,)).fetchone()
        except Exception as e:  # noqa: BLE001
            self._last.setText(f"Last run: unavailable ({e})")
            return
        if not row:
            self._last.setText("Last run: never.")
            return
        finished, status, params = row
        params = params if isinstance(params, dict) else json.loads(params or "{}")
        st = params.get("stats") or {}
        self._report_dir = params.get("report_dir")
        kind = "dry run" if params.get("dry_run") else "run"
        when = finished.strftime("%Y-%m-%d %H:%M") if finished else "still running"
        done = st.get("would_write" if params.get("dry_run") else "written", 0)
        self._last.setText(
            f"Last {kind}: {when} ({status}) — {st.get('due', params.get('due', 0))} due, "
            f"{done} {'would be written' if params.get('dry_run') else 'written'}, "
            f"{st.get('skipped', 0)} skipped, {st.get('failed', 0)} failed.")

    def _open_report(self) -> None:
        if self._report_dir and Path(self._report_dir).exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._report_dir))

    # ---- what's embedded ---------------------------------------------------

    def _make_viewer(self) -> QWidget:
        box = QGroupBox("What's embedded")
        v = QVBoxLayout(box)
        row = QHBoxLayout()
        row.addWidget(QLabel("Photo #"))
        self._pid = QSpinBox()
        self._pid.setRange(1, 2_000_000_000)
        row.addWidget(self._pid)
        b = QPushButton("Read file")
        b.clicked.connect(self._read_embedded)
        row.addWidget(b)
        self._only_ours = QPushButton("Show all tags")
        self._only_ours.setCheckable(True)
        self._only_ours.toggled.connect(lambda _: self._read_embedded())
        row.addWidget(self._only_ours)
        row.addStretch()
        v.addLayout(row)
        self._table = QTableWidget(0, 2)
        self._table.setHorizontalHeaderLabels(["Tag", "Value"])
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self._table.verticalHeader().setVisible(False)
        v.addWidget(self._table)
        return box

    def _read_embedded(self) -> None:
        pid = int(self._pid.value())
        try:
            settings = self._settings_lazy()
            with db.connection() as conn:
                conn.autocommit = True
                row = conn.execute("select working_path, file_version, metadata_file_version "
                                   "from photos where id = %s", (pid,)).fetchone()
            if not row:
                self._status_right.setText(f"No photo #{pid}.")
                return
            path = resolve_working_path(settings.WORKING_DIR, row[0])
            if path is None or not path.is_file():
                self._status_right.setText(f"#{pid}: no working file at {path}.")
                return
            with et_mod.ExifTool(et_mod.find_exiftool(settings.EXIFTOOL_PATH)) as et:
                tags = et.read(path, None if self._only_ours.isChecked() else et_mod.READ_TAGS)
        except Exception as e:  # noqa: BLE001
            self._status_right.setText(f"Could not read #{pid}: {e}")
            return
        owned = set(et_mod.OWNED_TAGS)
        items = sorted(tags.items(), key=lambda kv: (kv[0] not in owned, kv[0]))
        self._table.setRowCount(len(items))
        bold = QFont()
        bold.setBold(True)
        for i, (tag, val) in enumerate(items):
            text = val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
            t = QTableWidgetItem(tag)
            if tag in owned:
                t.setFont(bold)
            self._table.setItem(i, 0, t)
            self._table.setItem(i, 1, QTableWidgetItem(text))
        self._table.resizeRowsToContents()
        state = ("current" if row[2] is not None and row[1] == row[2]
                 else "never written" if row[2] is None else "stale (file replaced)")
        self._status_right.setText(f"#{pid}: {path.name}, metadata {state}. "
                                   "Bold tags are the archive's own.")

    def _settings_lazy(self) -> Settings:
        if self._settings is None:
            self._settings = load_settings()
        return self._settings
