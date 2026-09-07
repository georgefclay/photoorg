"""Dedupe mode UI. Walks the pending groups, shows the pre-selected
keeper vs. a chosen peer at full resolution with synchronised zoom/pan,
and applies keyboard-driven decisions.

Keys:
    A       accept keeper (quarantine the rest)
    1-9     pick that filmstrip position as keeper (then A to commit)
    N       not duplicates (adds exclusions, closes the group)
    S       skip (stays pending, returns on the next run)
    F       fit both panes to window
    Z       undo the most recent resolve/not-duplicates (session stack)
    Left    previous group in queue
    Right   next group in queue
    R       rescan (runs dedupe_scan in the background)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QKeyEvent, QPixmap, QShortcut, QKeySequence
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QMessageBox, QProgressBar, QPushButton, QSizePolicy, QSplitter,
    QToolButton, QVBoxLayout, QWidget,
)

from ... import db
from ...config import load as load_config
from ...workers import BackgroundJob
from ..ingest.paths import thumb_path
from ...app.widgets.synced_viewer import SyncedViewer
from . import exclusions as excl_mod
from . import queue as q
from . import resolve as resolve_mod
from . import scan as scan_mod
from . import undo as undo_mod

log = logging.getLogger(__name__)


FILMSTRIP_THUMB_SIZE = 96


@dataclass
class _UndoEntry:
    """One entry on the session undo stack."""
    kind: str          # 'resolve' | 'not_duplicates'
    group_id: int


class DedupePanel(QWidget):
    """The whole Dedupe mode."""

    def __init__(self) -> None:
        super().__init__()
        self._settings = load_config()
        self._group_ids: list[int] = []
        self._cursor: int = 0
        self._group: q.Group | None = None
        self._chosen_keeper_id: int | None = None
        self._focused_peer_id: int | None = None
        self._undo_stack: list[_UndoEntry] = []
        self._scan_job: BackgroundJob | None = None

        self._header = QLabel("Dedupe — press R to scan")
        self._header.setStyleSheet("font-weight: bold; padding: 4px;")

        self._rescan_btn = QPushButton("Rescan (R)")
        self._rescan_btn.clicked.connect(self._start_scan)
        self._accept_btn = QPushButton("Accept keeper (A)")
        self._accept_btn.clicked.connect(self._accept_keeper)
        self._not_dup_btn = QPushButton("Not duplicates (N)")
        self._not_dup_btn.clicked.connect(self._mark_not_duplicates)
        self._skip_btn = QPushButton("Skip (S)")
        self._skip_btn.clicked.connect(self._skip)
        self._undo_btn = QPushButton("Undo (Z)")
        self._undo_btn.clicked.connect(self._undo)

        controls = QHBoxLayout()
        controls.addWidget(self._rescan_btn)
        controls.addStretch(1)
        controls.addWidget(self._accept_btn)
        controls.addWidget(self._not_dup_btn)
        controls.addWidget(self._skip_btn)
        controls.addWidget(self._undo_btn)

        self._progress = QProgressBar()
        self._progress.setVisible(False)

        self._filmstrip = QListWidget()
        self._filmstrip.setFlow(QListWidget.LeftToRight)
        self._filmstrip.setWrapping(False)
        self._filmstrip.setFixedHeight(FILMSTRIP_THUMB_SIZE + 44)
        self._filmstrip.setIconSize(
            self._filmstrip.iconSize().__class__(
                FILMSTRIP_THUMB_SIZE, FILMSTRIP_THUMB_SIZE,
            )
        )
        self._filmstrip.setSelectionMode(QListWidget.SingleSelection)
        self._filmstrip.itemSelectionChanged.connect(self._on_filmstrip_pick)

        self._viewer = SyncedViewer()

        self._facts = QLabel("")
        self._facts.setTextFormat(Qt.RichText)
        self._facts.setStyleSheet(
            "font-family: Consolas, Menlo, monospace; padding: 4px; "
            "background: #111; color: #ddd;"
        )
        self._facts.setWordWrap(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.addWidget(self._header)
        outer.addLayout(controls)
        outer.addWidget(self._progress)
        outer.addWidget(self._filmstrip)
        outer.addWidget(self._viewer, stretch=1)
        outer.addWidget(self._facts)

        self._install_shortcuts()
        # Defer the first DB read until after the event loop starts so
        # the panel can be constructed before init_pool in tests.
        QTimer.singleShot(0, self._refresh_queue)

    # ------------------------------------------------------------------
    # Shortcuts
    # ------------------------------------------------------------------

    def _install_shortcuts(self) -> None:
        def _sc(seq: str, fn) -> None:
            s = QShortcut(QKeySequence(seq), self)
            s.setContext(Qt.WidgetWithChildrenShortcut)
            s.activated.connect(fn)

        _sc("A", self._accept_keeper)
        _sc("N", self._mark_not_duplicates)
        _sc("S", self._skip)
        _sc("Z", self._undo)
        _sc("R", self._start_scan)
        _sc("F", self._viewer.fit_to_window)
        _sc("Left", self._prev_group)
        _sc("Right", self._next_group)
        for i in range(1, 10):
            _sc(str(i), lambda pos=i - 1: self._pick_filmstrip_position(pos))

    # ------------------------------------------------------------------
    # Queue navigation
    # ------------------------------------------------------------------

    def _refresh_queue(self) -> None:
        self._group_ids = q.pending_ids()
        if not self._group_ids:
            self._cursor = 0
            self._group = None
            self._render_empty()
            return
        if self._cursor >= len(self._group_ids):
            self._cursor = len(self._group_ids) - 1
        self._load_current()

    def _load_current(self) -> None:
        if not self._group_ids:
            self._render_empty()
            return
        gid = self._group_ids[self._cursor]
        self._group = q.load_group(gid)
        if self._group is None:
            # Group was resolved elsewhere; drop and refresh.
            self._group_ids.pop(self._cursor)
            self._load_current()
            return
        self._chosen_keeper_id = next(
            (m.photo_id for m in self._group.members if m.is_keeper),
            self._group.members[0].photo_id,
        )
        self._focused_peer_id = next(
            (m.photo_id for m in self._group.members
             if m.photo_id != self._chosen_keeper_id),
            None,
        )
        self._render()

    def _prev_group(self) -> None:
        if not self._group_ids:
            return
        if self._cursor > 0:
            self._cursor -= 1
            self._load_current()

    def _next_group(self) -> None:
        if not self._group_ids:
            return
        if self._cursor < len(self._group_ids) - 1:
            self._cursor += 1
            self._load_current()

    def _skip(self) -> None:
        # Skip = move to tail of session queue, stays pending.
        if not self._group_ids or self._group is None:
            return
        gid = self._group_ids.pop(self._cursor)
        self._group_ids.append(gid)
        if self._cursor >= len(self._group_ids):
            self._cursor = 0
        self._load_current()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render_empty(self) -> None:
        self._header.setText("Dedupe — no pending groups. Press R to (re)scan.")
        self._filmstrip.clear()
        self._viewer.set_left(None)
        self._viewer.set_right(None)
        self._facts.setText("")
        for b in (self._accept_btn, self._not_dup_btn, self._skip_btn):
            b.setEnabled(False)

    def _render(self) -> None:
        g = self._group
        assert g is not None
        n = len(self._group_ids)
        i = self._cursor + 1
        self._header.setText(
            f"Dedupe — group {i} / {n}, size {len(g.members)}, "
            f"min distance {g.min_distance}"
        )
        for b in (self._accept_btn, self._not_dup_btn, self._skip_btn):
            b.setEnabled(True)

        self._filmstrip.blockSignals(True)
        self._filmstrip.clear()
        for pos, m in enumerate(g.members):
            item = QListWidgetItem()
            label = f"{pos + 1}: id {m.photo_id}"
            if m.photo_id == self._chosen_keeper_id:
                label += "  (KEEPER)"
            if m.burst_hint:
                label += "  [burst]"
            item.setText(label)
            item.setData(Qt.UserRole, m.photo_id)
            pix = _load_thumb(self._settings, m.photo_id)
            if pix is not None:
                item.setIcon(_pixmap_icon(pix))
            self._filmstrip.addItem(item)
            if m.photo_id == self._focused_peer_id:
                self._filmstrip.setCurrentRow(pos)
        self._filmstrip.blockSignals(False)

        keeper = self._member(self._chosen_keeper_id)
        peer = self._member(self._focused_peer_id) if self._focused_peer_id else None
        self._viewer.set_left(
            _load_full(self._settings, keeper),
            caption=f"KEEPER  #{keeper.photo_id}  {keeper.keeper_reason or ''}",
        )
        if peer is not None:
            self._viewer.set_right(
                _load_full(self._settings, peer),
                caption=(
                    f"peer  #{peer.photo_id}  transform={peer.transform}  "
                    f"dist={peer.distance_to_keeper}"
                ),
            )
        else:
            self._viewer.set_right(None, caption="(no peer)")

        self._facts.setText(_render_facts(g, self._chosen_keeper_id))
        self._viewer.fit_to_window()

    def _member(self, photo_id: int | None) -> q.GroupMember | None:
        if photo_id is None or self._group is None:
            return None
        for m in self._group.members:
            if m.photo_id == photo_id:
                return m
        return None

    def _on_filmstrip_pick(self) -> None:
        item = self._filmstrip.currentItem()
        if item is None:
            return
        pid = item.data(Qt.UserRole)
        if self._group is None:
            return
        # A single click focuses that member as the right pane. Number
        # keys promote to keeper (see _pick_filmstrip_position).
        if pid != self._chosen_keeper_id:
            self._focused_peer_id = pid
        else:
            # Clicking the keeper — pick a different peer.
            self._focused_peer_id = next(
                (m.photo_id for m in self._group.members
                 if m.photo_id != pid),
                None,
            )
        self._render()

    def _pick_filmstrip_position(self, pos: int) -> None:
        if self._group is None or pos < 0 or pos >= len(self._group.members):
            return
        self._chosen_keeper_id = self._group.members[pos].photo_id
        # Ensure the peer pane still shows something.
        if self._focused_peer_id == self._chosen_keeper_id:
            self._focused_peer_id = next(
                (m.photo_id for m in self._group.members
                 if m.photo_id != self._chosen_keeper_id),
                None,
            )
        self._render()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _accept_keeper(self) -> None:
        if self._group is None or self._chosen_keeper_id is None:
            return
        gid = self._group.group_id
        keeper_id = self._chosen_keeper_id
        try:
            resolve_mod.resolve_group(
                self._settings, gid,
                chosen_keeper_id=keeper_id, actor="desktop",
            )
        except Exception as e:
            log.exception("dedupe: resolve failed")
            QMessageBox.critical(self, "Resolve failed", str(e))
            return
        self._undo_stack.append(_UndoEntry(kind="resolve", group_id=gid))
        self._advance_past_current()

    def _mark_not_duplicates(self) -> None:
        if self._group is None:
            return
        gid = self._group.group_id
        try:
            excl_mod.mark_group_not_duplicates(
                self._settings, gid, actor="desktop",
            )
        except Exception as e:
            log.exception("dedupe: not-duplicates failed")
            QMessageBox.critical(self, "Not duplicates failed", str(e))
            return
        self._undo_stack.append(_UndoEntry(kind="not_duplicates", group_id=gid))
        self._advance_past_current()

    def _advance_past_current(self) -> None:
        if not self._group_ids:
            self._refresh_queue()
            return
        del self._group_ids[self._cursor]
        if self._cursor >= len(self._group_ids):
            self._cursor = max(0, len(self._group_ids) - 1)
        if not self._group_ids:
            self._render_empty()
        else:
            self._load_current()

    def _undo(self) -> None:
        if not self._undo_stack:
            return
        entry = self._undo_stack.pop()
        try:
            if entry.kind == "resolve":
                undo_mod.undo_resolve(
                    self._settings, entry.group_id, actor="desktop",
                )
            elif entry.kind == "not_duplicates":
                _undo_not_duplicates(self._settings, entry.group_id)
        except Exception as e:
            log.exception("dedupe: undo failed")
            QMessageBox.critical(self, "Undo failed", str(e))
            return
        # Put the group back into the session queue and jump to it.
        if entry.group_id not in self._group_ids:
            self._group_ids.insert(self._cursor, entry.group_id)
        self._load_current()

    # ------------------------------------------------------------------
    # Scan
    # ------------------------------------------------------------------

    def _start_scan(self) -> None:
        if self._scan_job is not None:
            return
        self._progress.setVisible(True)
        self._progress.setRange(0, 0)  # busy
        self._rescan_btn.setEnabled(False)
        self._scan_job = BackgroundJob(target=_scan_target, kwargs={})
        self._scan_job.signals.finished.connect(self._on_scan_finished)
        self._scan_job.signals.failed.connect(self._on_scan_failed)
        self._scan_job.start()

    def _on_scan_finished(self, summary: dict) -> None:
        self._progress.setVisible(False)
        self._rescan_btn.setEnabled(True)
        self._scan_job = None
        stats = summary.get("stats") or {}
        QMessageBox.information(
            self, "Dedupe scan complete",
            _format_scan_stats(stats),
        )
        self._refresh_queue()

    def _on_scan_failed(self, tb: str) -> None:
        self._progress.setVisible(False)
        self._rescan_btn.setEnabled(True)
        self._scan_job = None
        QMessageBox.critical(self, "Scan failed", tb)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        # Swallow arrow keys so QListWidget doesn't consume them first.
        super().keyPressEvent(event)


def _undo_not_duplicates(settings, group_id: int) -> None:
    """Reopen a not-duplicates group and drop the exclusion rows it wrote.

    We look up the most recent dedupe.not_duplicates audit row for this
    group to know which pairs were inserted THIS time, so undoing does
    not remove exclusions inserted by earlier reviewer decisions.
    """
    import json
    with db.connection() as conn:
        conn.autocommit = False
        try:
            row = conn.execute("""
                select id, new_value from audit_log
                where action = 'dedupe.not_duplicates'
                  and entity_type = 'dedupe_group'
                  and entity_id = %s
                order by id desc
                limit 1
            """, (group_id,)).fetchone()
            if row is None:
                raise ValueError(
                    f"no dedupe.not_duplicates audit row for group {group_id}"
                )
            _audit_id, payload = row
            if isinstance(payload, str):
                payload = json.loads(payload)
            members = payload.get("members", [])
            from itertools import combinations
            for a, b in combinations(sorted(members), 2):
                conn.execute("""
                    delete from dedupe_exclusions
                    where photo_a = %s and photo_b = %s
                """, (a, b))
            conn.execute("""
                update dedupe_groups
                set status = 'pending',
                    resolved_at = null,
                    resolved_by = null
                where id = %s
            """, (group_id,))
            conn.execute("""
                insert into audit_log
                  (actor, action, entity_type, entity_id, new_value)
                values ('desktop', 'dedupe.undo', 'dedupe_group', %s, %s::jsonb)
            """, (group_id, json.dumps({
                "reversed": "not_duplicates", "group_id": group_id,
            })))
            conn.commit()
        except Exception:
            conn.rollback()
            raise


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _load_thumb(settings, photo_id: int) -> QPixmap | None:
    p = thumb_path(settings, photo_id)
    if not p.exists():
        return None
    pix = QPixmap(str(p))
    return pix if not pix.isNull() else None


def _pixmap_icon(pix: QPixmap):
    from PySide6.QtGui import QIcon
    return QIcon(pix)


def _load_full(settings, member) -> QPixmap | None:
    """Load the working copy at full resolution. Falls back to the
    quarantine copy if the working path is missing (should never happen
    for a pending dedupe member, since only keep/private are indexed).
    """
    if member is None:
        return None
    for candidate in (member.working_path, member.quarantine_path):
        if not candidate:
            continue
        try:
            if Path(candidate).exists():
                pix = QPixmap(candidate)
                if not pix.isNull():
                    return pix
        except OSError:
            pass
    # Last resort — the thumbnail.
    return _load_thumb(settings, member.photo_id)


def _render_facts(g: q.Group, chosen_keeper_id: int | None) -> str:
    rows: list[str] = []
    rows.append(
        f"<b>group {g.group_id}</b> — {len(g.members)} members, "
        f"min distance {g.min_distance}"
    )
    header = (
        "<pre>#  photo_id  role      folder / batch                   filename                        "
        "dims       size       mime         exif date            camera        transform  ph  dh</pre>"
    )
    rows.append(header)
    for pos, m in enumerate(g.members, start=1):
        role = "KEEPER" if m.photo_id == chosen_keeper_id else "loser "
        dims = (
            f"{m.width or '?'}x{m.height or '?'}"
        )
        size = _fmt_size(m.file_size)
        exif_date = str(m.exif_taken_at)[:19] if m.exif_taken_at else "-"
        camera = (m.exif_camera or "-")[:12]
        loc = m.scan_batch or m.source_folder
        row = (
            f"<pre>{pos}  {m.photo_id:>8}  {role}  "
            f"{loc[:30]:30}  {m.source_filename[:30]:30}  "
            f"{dims:>10}  {size:>9}  {m.mime[:12]:12}  "
            f"{exif_date:<19}  {camera:<12}  "
            f"{m.transform:>10}  {m.phash_dist if m.phash_dist is not None else '-':>3}  "
            f"{m.dhash_dist if m.dhash_dist is not None else '-':>3}"
            f"{'  [burst]' if m.burst_hint else ''}"
            f"{'  [PRIVATE]' if m.is_private else ''}"
            f"</pre>"
        )
        rows.append(row)
    return "\n".join(rows)


def _fmt_size(n: int | None) -> str:
    if n is None:
        return "-"
    if n < 1024:
        return f"{n} B"
    for suffix, div in (("KB", 1024), ("MB", 1024 ** 2), ("GB", 1024 ** 3)):
        val = n / div
        if val < 1024:
            return f"{val:.1f} {suffix}"
    return f"{n / (1024 ** 4):.1f} TB"


def _format_scan_stats(stats: dict) -> str:
    lines: list[str] = []
    lines.append(f"Photos scanned: {stats.get('photos_scanned', 0)}")
    lines.append(f"Thumbs missing: {stats.get('thumbs_missing', 0)}")
    lines.append(f"Variant errors: {stats.get('variant_errors', 0)}")
    lines.append(f"Candidate pairs: {stats.get('candidate_pairs', 0)}")
    lines.append(f"Groups created: {stats.get('groups_created', 0)}")
    lines.append(f"Elapsed: {stats.get('elapsed_seconds', 0):.2f} s")
    gs = stats.get("groups_by_size") or {}
    if gs:
        lines.append("Group size histogram:")
        for size, count in sorted(gs.items()):
            lines.append(f"  size {size}: {count}")
    dh = stats.get("min_distance_histogram") or {}
    if dh:
        lines.append("Min-distance histogram:")
        for dist in sorted(dh):
            lines.append(f"  distance {dist}: {dh[dist]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Worker target
# ---------------------------------------------------------------------------


def _scan_target(progress_cb, cancel_token, **_) -> dict:
    settings = load_config()
    stats = scan_mod.run_dedupe_scan(settings)
    return {"stats": stats.to_dict()}
