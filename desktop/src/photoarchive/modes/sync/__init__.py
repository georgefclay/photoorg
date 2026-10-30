"""Sync mode: push laptop → web, pull web → laptop, plus a Groups
sub-panel for bulk assign/unassign.

Manual only. Nothing here runs on a schedule; the user clicks Push or
Pull. Long operations spin off a QThread; progress lands in a status
bar. See push.py, pull.py, bulk.py, and client.py for the innards.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QPushButton,
    QSplitter, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from ... import db
from ...config import Settings, load as load_settings
from ...memory import rss_text
from .bulk import bulk_apply, group_summary, resolve_photo_ids, unfiled_ids
from .client import WebSyncClient, WebSyncError
from .pull import (
    PullProgress, pull_confirmed, pull_contributions, pull_groups,
)
from .push import PushProgress, push
from ..metadata.ui import MetadataRunner, count_due_job, due_text

log = logging.getLogger(__name__)


class _Cancelled(Exception):
    """Raised from the progress callback once the app is closing. Push is
    resumable: a second Push after a stopped one carries on."""


class _Worker(QObject):
    """Runs one sync job on a QThread.

    **Every signal goes to a bound method of the panel, never a lambda**
    (Phase 13 fix-up 1, item 6). A lambda has no receiver, so PySide6 runs
    it on the thread that emitted, this worker's, and the old `finished`
    lambda called `setVisible`, `setText` and `append` on widgets from
    there. Touching a widget off the GUI thread kills the process without a
    Python traceback, which is how the app vanished the moment George's
    first GUI push finished (2026-10-29). A bound slot of a QObject living
    on the GUI thread is delivered queued, on the GUI thread.
    """
    progress = Signal(str)
    finished = Signal(dict)
    failed = Signal(str)

    def __init__(self, name: str, fn: Callable[[Callable[[str], None]], dict]) -> None:
        super().__init__()
        self.name = name
        self._fn = fn
        self.cancel_requested = False

    def _status(self, msg: str) -> None:
        if self.cancel_requested:
            raise _Cancelled()
        self.progress.emit(msg)

    def run(self) -> None:
        t0 = time.monotonic()
        log.info("sync %s: start (%s)", self.name, rss_text())
        try:
            out = self._fn(self._status) or {}
            log.info("sync %s: done in %.1f s: %s (%s)", self.name, time.monotonic() - t0,
                     {k: v for k, v in out.items() if k != "metadata_due"}, rss_text())
            self.finished.emit(out)
        except _Cancelled:
            log.warning("sync %s: stopped at a safe point because the app is closing", self.name)
            self.finished.emit({"cancelled": True})
        except Exception as e:  # noqa: BLE001
            log.exception("sync %s: failed after %.1f s (%s)", self.name,
                          time.monotonic() - t0, rss_text())
            self.failed.emit(str(e))


class _StageLog:
    """Writes a push's stage starts and ends, with counts and memory, to the
    log file as well as the pane: the GUI twin of `run_push.py`'s output.
    Per-item progress stays in the pane only."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._stage: str | None = None
        self._last = ""
        self._t0 = time.monotonic()

    def update(self, stage: str, line: str) -> None:
        if stage != self._stage:
            self.close()
            self._stage, self._t0 = stage, time.monotonic()
            log.info("sync %s: stage %s start: %s (%s)", self._name, stage, line, rss_text())
        self._last = line

    def close(self) -> None:
        if self._stage is not None:
            log.info("sync %s: stage %s end after %.1f s: %s (%s)", self._name, self._stage,
                     time.monotonic() - self._t0, self._last, rss_text())
            self._stage = None


class SyncPanel(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._settings: Settings | None = None
        self._client: WebSyncClient | None = None
        # One sync job at a time. `_job` is the running one; `_live_jobs`
        # holds every (thread, worker) until the thread's own `finished`
        # fires, so the last Python reference to a running QThread is never
        # dropped (CLAUDE.md, Cleanup, "two UI rules").
        self._job: _Worker | None = None
        self._job_log: QTextEdit | None = None
        self._live_jobs: list[tuple[QThread, _Worker]] = []

        root = QVBoxLayout(self)
        tabs = QTabWidget()
        root.addWidget(tabs)

        tabs.addTab(self._make_push_tab(), "Push")
        tabs.addTab(self._make_pull_tab(), "Pull")
        tabs.addTab(self._make_groups_tab(), "Groups")

        # Phase 13: after every pull or push, offer to write the metadata of
        # the photos whose facts changed. Never automatic — George presses it.
        meta = QHBoxLayout()
        self._meta_due = QLabel("")
        self._meta_due.setWordWrap(True)
        meta.addWidget(self._meta_due, stretch=1)
        self._btn_meta = QPushButton("Write metadata")
        self._btn_meta.clicked.connect(self._on_write_metadata)
        self._btn_meta.setVisible(False)
        meta.addWidget(self._btn_meta)
        root.addLayout(meta)
        self._meta_runner = MetadataRunner.shared()
        self._meta_runner.finished.connect(self._on_metadata_finished)

        status_row = QHBoxLayout()
        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet("color: #333; padding: 4px;")
        status_row.addWidget(self._status, stretch=1)
        # Resident memory while a job runs: low memory is the usual killer.
        self._rss = QLabel("")
        self._rss.setStyleSheet("color: #666; padding: 4px;")
        status_row.addWidget(self._rss)
        root.addLayout(status_row)
        self._rss_timer = QTimer(self)
        self._rss_timer.setInterval(2000)
        self._rss_timer.timeout.connect(self._update_rss)

        self._work_buttons = [
            self._btn_push, self._btn_pull_groups, self._btn_pull_confirmed,
            self._btn_pull_contribs, self._btn_add, self._btn_remove, self._btn_meta,
        ]

    # ------------------------------------------------------------------
    # Push tab
    # ------------------------------------------------------------------
    def _make_push_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(QLabel(
            "Push the keep-set to the web archive. Private and junk photos "
            "are skipped automatically. Resumable: a second Push after a "
            "successful first sends 0 files."
        ))
        self._chk_files_only_grouped = QCheckBox(
            "Files: only photos in a group  (recommended while cleanup / AI jobs "
            "are still bumping file_version — metadata still pushes for everything)"
        )
        self._chk_files_only_grouped.setChecked(True)
        v.addWidget(self._chk_files_only_grouped)
        self._btn_push = QPushButton("Push now")
        self._btn_push.clicked.connect(self._on_push)
        v.addWidget(self._btn_push, alignment=Qt.AlignLeft)
        self._push_log = QTextEdit()
        self._push_log.setReadOnly(True)
        v.addWidget(self._push_log)
        return w

    def _on_push(self) -> None:
        if self._busy():
            return
        self._push_log.clear()
        settings = self._settings_lazy()
        client = self._client_lazy()

        files_only_for_grouped = bool(self._chk_files_only_grouped.isChecked())

        def do(status: Callable[[str], None]) -> dict:
            stages = _StageLog("push")

            def prog(p: PushProgress) -> None:
                line = f"{p.stage}: {p.done}/{p.total} {p.detail}"
                stages.update(p.stage, line)
                status(line)
            try:
                stats = push(
                    client,
                    working_dir=Path(settings.WORKING_DIR),
                    thumbs_dir=Path(settings.THUMBS_DIR),
                    state_dir=Path(settings.WORKING_DIR).parent / "sync-state",
                    send_face_embeddings=_env_bool("SYNC_FACE_EMBEDDINGS"),
                    files_only_for_grouped=files_only_for_grouped,
                    progress=prog,
                )
            finally:
                stages.close()
            return {
                "photos": stats.photos_upserted,
                "files": stats.files_uploaded,
                "bytes": stats.bytes_uploaded,
                "tables": stats.tables,
            }

        self._run_worker("push", do, log_widget=self._push_log)

    # ------------------------------------------------------------------
    # Pull tab
    # ------------------------------------------------------------------
    def _make_pull_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(QLabel(
            "Pull confirmed values + groups + approved contributions from the web. "
            "Push always pulls groups + confirmed values first. "
            "Contributions land in the CONTRIB_ROOT master root under _incoming/, "
            "then rename into <uploader>/<contribution_id>/ after a manifest check."
        ))
        row = QHBoxLayout()
        self._btn_pull_groups = QPushButton("Pull groups")
        self._btn_pull_groups.clicked.connect(self._on_pull_groups)
        row.addWidget(self._btn_pull_groups)
        self._btn_pull_confirmed = QPushButton("Pull confirmed values")
        self._btn_pull_confirmed.clicked.connect(self._on_pull_confirmed)
        row.addWidget(self._btn_pull_confirmed)
        self._btn_pull_contribs = QPushButton("Pull approved contributions")
        self._btn_pull_contribs.clicked.connect(self._on_pull_contribs)
        row.addWidget(self._btn_pull_contribs)
        row.addStretch()
        v.addLayout(row)
        self._pull_log = QTextEdit()
        self._pull_log.setReadOnly(True)
        v.addWidget(self._pull_log)
        return w

    def _on_pull_groups(self) -> None:
        if self._busy(): return
        client = self._client_lazy()
        state_dir = Path(self._settings_lazy().WORKING_DIR).parent / "sync-state"
        def do(status):
            g, m, pg = pull_groups(client, state_dir)
            status(f"groups {g}, members {m}, photo_groups {pg}")
            return {"groups": g, "members": m, "photo_groups": pg}
        self._run_worker("pull groups", do, log_widget=self._pull_log)

    def _on_pull_confirmed(self) -> None:
        if self._busy(): return
        client = self._client_lazy()
        settings = self._settings_lazy()
        state_dir = Path(settings.WORKING_DIR).parent / "sync-state"
        def do(status):
            n = pull_confirmed(client, state_dir,
                               working_dir=Path(settings.WORKING_DIR),
                               thumbs_dir=Path(settings.THUMBS_DIR))
            status(f"applied {n} facts")
            return {"facts": n}
        self._run_worker("pull confirmed", do, log_widget=self._pull_log)

    def _on_pull_contribs(self) -> None:
        if self._busy(): return
        settings = self._settings_lazy()
        contrib_root = None
        for root in settings.master_roots:
            if root.kind == "contrib":
                contrib_root = Path(root.path)
                break
        if contrib_root is None:
            self._status.setText(
                "No master root with kind=contrib configured. Add one to "
                "MASTER_ROOTS in desktop/.env, e.g. contrib=D:\\Contributed|contrib"
            )
            return
        client = self._client_lazy()
        def do(status):
            c, f = pull_contributions(client, contrib_root)
            status(f"pulled {c} contributions, {f} files")
            return {"contributions": c, "files": f}
        self._run_worker("pull contributions", do, log_widget=self._pull_log)

    # ------------------------------------------------------------------
    # Groups tab: bulk assign/unassign + counts
    # ------------------------------------------------------------------
    def _make_groups_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(QLabel("Groups on this laptop:"))
        self._group_list = QListWidget()
        v.addWidget(self._group_list, stretch=1)

        v.addWidget(QLabel("Bulk assign — enter a scan_batch, album id, person id, year, or folder:"))
        row = QHBoxLayout()
        self._filter_kind = QComboBox()
        self._filter_kind.addItems(["scan_batch", "album_id", "person_id", "year", "decade", "source_folder"])
        row.addWidget(self._filter_kind)
        self._filter_value = QLineEdit()
        row.addWidget(self._filter_value)
        self._add_group = QComboBox()
        self._add_group.setEditable(False)
        row.addWidget(QLabel(" → group:"))
        row.addWidget(self._add_group)
        row.addStretch()
        v.addLayout(row)

        btn_row = QHBoxLayout()
        self._btn_refresh_groups = QPushButton("Refresh")
        self._btn_refresh_groups.clicked.connect(self._refresh_groups)
        btn_row.addWidget(self._btn_refresh_groups)
        self._btn_add = QPushButton("Assign")
        self._btn_add.clicked.connect(lambda: self._on_bulk("add"))
        btn_row.addWidget(self._btn_add)
        self._btn_remove = QPushButton("Unassign")
        self._btn_remove.clicked.connect(lambda: self._on_bulk("remove"))
        btn_row.addWidget(self._btn_remove)
        btn_row.addStretch()
        v.addLayout(btn_row)

        self._groups_log = QTextEdit()
        self._groups_log.setReadOnly(True)
        v.addWidget(self._groups_log)

        return w

    def _refresh_groups(self) -> None:
        try:
            with db.connection() as conn:
                conn.autocommit = True
                items = group_summary(conn)
                unfiled = len(unfiled_ids(conn))
            self._group_list.clear()
            self._add_group.clear()
            for r in items:
                self._group_list.addItem(f"{r['name']}   photos={r['photo_count']}   members={r['member_count']}")
                self._add_group.addItem(f"{r['name']} ({r['id']})", r['id'])
            self._group_list.addItem(f"(unfiled)   photos={unfiled}")
            self._status.setText(f"Groups: {len(items)}. Unfiled: {unfiled} photos.")
        except Exception as e:
            self._status.setText(f"Refresh failed: {e}")

    def _on_bulk(self, direction: str) -> None:
        if self._busy():
            return
        try:
            kind = self._filter_kind.currentText()
            raw = self._filter_value.text().strip()
            if not raw:
                self._status.setText("Enter a filter value.")
                return
            gid = self._add_group.currentData()
            if not gid:
                self._status.setText("Pick a group.")
                return
            kwargs = {}
            if kind in ("album_id", "person_id", "year", "decade"):
                kwargs[kind] = int(raw)
            else:
                kwargs[kind] = raw
            with db.connection() as conn:
                conn.autocommit = False
                try:
                    photo_ids = resolve_photo_ids(conn, **kwargs)
                    add = [gid] if direction == "add" else []
                    remove = [gid] if direction == "remove" else []
                    stats = bulk_apply(conn, photo_ids, add=add, remove=remove)
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            self._groups_log.append(
                f"{direction} group {gid}: photos={stats['photos']} added={stats['added']} removed={stats['removed']}"
            )
            self._refresh_groups()
        except Exception as e:
            log.exception("bulk apply failed")
            self._status.setText(f"Bulk apply failed: {e}")

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------
    def _settings_lazy(self) -> Settings:
        if self._settings is None:
            self._settings = load_settings()
        return self._settings

    def _client_lazy(self) -> WebSyncClient:
        if self._client is None:
            s = self._settings_lazy()
            self._client = WebSyncClient(s.WEB_API_URL, s.WEB_API_TOKEN)
        return self._client

    def running_job(self) -> str | None:
        """The name of the sync job running now, for the main window's close."""
        return self._job.name if self._job is not None else None

    def _busy(self) -> bool:
        if self._job is not None:
            self._status.setText(
                f"{self._job.name.capitalize()} is running; one sync job at a time.")
            return True
        if self._meta_runner.is_running():
            # Pushing files that are being swapped would send half a version.
            self._status.setText("The metadata writer is running; sync waits for it.")
            return True
        return False

    def _set_buttons(self) -> None:
        """Every button that starts work shares the one slot."""
        free = self._job is None and not self._meta_runner.is_running()
        for b in self._work_buttons:
            b.setEnabled(free)

    # ------------------------------------------------------------------
    # Metadata (Phase 13)
    # ------------------------------------------------------------------
    def _show_metadata_due(self, due: dict | None) -> None:
        if not due:
            return
        n, g = int(due.get("due", 0)), int(due.get("in_group", 0))
        self._meta_due.setText(due_text(n, g))
        self._btn_meta.setText(f"Write metadata for {n} changed photos")
        self._btn_meta.setVisible(n > 0)
        self._set_buttons()

    def _on_write_metadata(self) -> None:
        if self._busy():
            return
        log_widget = self._pull_log
        log_widget.append("--- metadata writer ---")
        self._meta_runner.line.connect(log_widget.append)
        if not self._meta_runner.start(dry_run=False):
            self._status.setText("A metadata run is already going.")
        self._set_buttons()

    def _on_metadata_finished(self, code: int, summary: str) -> None:
        try:
            self._meta_runner.line.disconnect(self._pull_log.append)
        except (RuntimeError, TypeError):
            pass
        self._status.setText(("Metadata written. " if code == 0 else f"Metadata writer exited {code}. ")
                             + (summary.splitlines()[-1] if summary else ""))
        self._set_buttons()
        try:
            self._show_metadata_due(count_due_job())
        except Exception:  # noqa: BLE001
            log.exception("metadata due count failed")

    # ------------------------------------------------------------------
    # The one job slot
    # ------------------------------------------------------------------
    def _run_worker(self, name: str, fn: Callable[[Callable[[str], None]], dict], *,
                    log_widget: QTextEdit) -> None:
        def with_metadata_due(status: Callable[[str], None]) -> dict:
            out = fn(status) or {}
            try:
                out["metadata_due"] = count_due_job()
            except Exception:  # noqa: BLE001 — the count is advice, never a failure
                log.exception("metadata due count failed")
            return out

        # No Qt parent: a parented QThread is destroyed with the panel even
        # while it runs. `_live_jobs` owns it until its `finished` fires.
        thread = QThread()
        worker = _Worker(name, with_metadata_due)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_job_progress)
        worker.finished.connect(self._on_job_finished)
        worker.failed.connect(self._on_job_failed)
        # Direct: QThread.quit is thread-safe, and a queued quit would wait
        # for a GUI thread that `shutdown()` is blocking in `wait()`.
        worker.finished.connect(thread.quit, Qt.DirectConnection)
        worker.failed.connect(thread.quit, Qt.DirectConnection)
        # The worker is deleted by its own thread's last event; the thread
        # object (GUI-owned) in `_on_thread_finished`.
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._on_thread_finished)

        self._job, self._job_log = worker, log_widget
        self._live_jobs.append((thread, worker))
        self._status.setText(f"{name.capitalize()} running...")
        self._set_buttons()
        self._update_rss()
        self._rss_timer.start()
        thread.start()

    @Slot(str)
    def _on_job_progress(self, msg: str) -> None:
        if self._job_log is not None:
            self._job_log.append(msg)

    @Slot(dict)
    def _on_job_finished(self, result: dict) -> None:
        name = self._job.name if self._job else "sync"
        self._show_metadata_due(result.pop("metadata_due", None))
        if self._job_log is not None:
            self._job_log.append(f"done: {result}")
        self._status.setText(f"{name.capitalize()} done: {result}")

    @Slot(str)
    def _on_job_failed(self, err: str) -> None:
        name = self._job.name if self._job else "sync"
        if self._job_log is not None:
            self._job_log.append(f"FAILED: {err}")
        self._status.setText(f"{name.capitalize()} failed: {err}")

    @Slot()
    def _on_thread_finished(self) -> None:
        for pair in list(self._live_jobs):
            thread, worker = pair
            if thread.isFinished():
                self._live_jobs.remove(pair)
                if worker is self._job:
                    self._job = None
                thread.deleteLater()
        if self._job is None:
            self._rss_timer.stop()
            self._update_rss()
        self._set_buttons()

    def _update_rss(self) -> None:
        self._rss.setText(rss_text())

    def shutdown(self, timeout_ms: int = 30_000) -> None:
        """Called by the main window as it closes: ask the running job to stop
        at its next safe point, then join every live thread."""
        for thread, worker in list(self._live_jobs):
            worker.cancel_requested = True
            if not thread.wait(timeout_ms):
                log.error("sync %s did not stop within %d ms of closing", worker.name, timeout_ms)
        self._rss_timer.stop()


def _env_bool(name: str) -> bool:
    import os
    v = (os.environ.get(name) or "").strip().lower()
    return v in ("1", "true", "yes")
