"""Sync mode: push laptop → web, pull web → laptop, plus a Groups
sub-panel for bulk assign/unassign.

Manual only. Nothing here runs on a schedule; the user clicks Push or
Pull. Long operations spin off a QThread; progress lands in a status
bar. See push.py, pull.py, bulk.py, and client.py for the innards.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QPushButton,
    QSplitter, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from ... import db
from ...config import Settings, load as load_settings
from .bulk import bulk_apply, group_summary, resolve_photo_ids, unfiled_ids
from .client import WebSyncClient, WebSyncError
from .pull import (
    PullProgress, pull_confirmed, pull_contributions, pull_groups,
)
from .push import PushProgress, push

log = logging.getLogger(__name__)


class _Worker(QObject):
    progress = Signal(str)
    finished = Signal(dict)
    failed = Signal(str)

    def __init__(self, fn: Callable[[Callable[[str], None]], dict]) -> None:
        super().__init__()
        self._fn = fn

    def run(self) -> None:
        try:
            out = self._fn(lambda msg: self.progress.emit(msg))
            self.finished.emit(out or {})
        except Exception as e:  # noqa: BLE001
            log.exception("sync worker failed")
            self.failed.emit(str(e))


class SyncPanel(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._settings: Settings | None = None
        self._client: WebSyncClient | None = None
        self._thread: QThread | None = None
        self._worker: _Worker | None = None

        root = QVBoxLayout(self)
        tabs = QTabWidget()
        root.addWidget(tabs)

        tabs.addTab(self._make_push_tab(), "Push")
        tabs.addTab(self._make_pull_tab(), "Pull")
        tabs.addTab(self._make_groups_tab(), "Groups")

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet("color: #333; padding: 4px;")
        root.addWidget(self._status)

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
            def prog(p: PushProgress) -> None:
                status(f"{p.stage}: {p.done}/{p.total} {p.detail}")
            stats = push(
                client,
                working_dir=Path(settings.WORKING_DIR),
                thumbs_dir=Path(settings.THUMBS_DIR),
                send_face_embeddings=_env_bool("SYNC_FACE_EMBEDDINGS"),
                files_only_for_grouped=files_only_for_grouped,
                progress=prog,
            )
            return {
                "photos": stats.photos_upserted,
                "files": stats.files_uploaded,
                "bytes": stats.bytes_uploaded,
                "tables": stats.tables,
            }

        self._run_worker(do, log_widget=self._push_log)

    # ------------------------------------------------------------------
    # Pull tab
    # ------------------------------------------------------------------
    def _make_pull_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.addWidget(QLabel(
            "Pull confirmed values + groups + approved contributions from the web. "
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
        self._run_worker(do, log_widget=self._pull_log)

    def _on_pull_confirmed(self) -> None:
        if self._busy(): return
        client = self._client_lazy()
        state_dir = Path(self._settings_lazy().WORKING_DIR).parent / "sync-state"
        def do(status):
            n = pull_confirmed(client, state_dir)
            status(f"applied {n} facts")
            return {"facts": n}
        self._run_worker(do, log_widget=self._pull_log)

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
        self._run_worker(do, log_widget=self._pull_log)

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

    def _busy(self) -> bool:
        if self._thread and self._thread.isRunning():
            self._status.setText("A sync operation is already running.")
            return True
        return False

    def _run_worker(self, fn: Callable[[Callable[[str], None]], dict], *, log_widget: QTextEdit) -> None:
        self._thread = QThread(self)
        self._worker = _Worker(fn)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(lambda msg: log_widget.append(msg))
        self._worker.finished.connect(lambda result: (
            log_widget.append(f"done: {result}"),
            self._status.setText(f"Done: {result}"),
            self._thread.quit(),
        ))
        self._worker.failed.connect(lambda err: (
            log_widget.append(f"FAILED: {err}"),
            self._status.setText(f"Failed: {err}"),
            self._thread.quit(),
        ))
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()


def _env_bool(name: str) -> bool:
    import os
    v = (os.environ.get(name) or "").strip().lower()
    return v in ("1", "true", "yes")
