from __future__ import annotations

import logging
import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, Signal

log = logging.getLogger(__name__)


class WorkerSignals(QObject):
    progress = Signal(dict)   # {kind, ...}: incremental updates for the UI
    finished = Signal(dict)   # summary dict from the target callable
    failed = Signal(str)      # traceback string


class Cancelled(Exception):
    """Raised inside a worker's target when the run has been cancelled."""


class CancelToken:
    """Passed into worker targets. Target must check .is_set() at safe points
    (between files, not mid-copy) and either exit cleanly or raise Cancelled."""

    def __init__(self) -> None:
        self._set = False

    def set(self) -> None:
        self._set = True

    def is_set(self) -> bool:
        return self._set


class BackgroundJob(QThread):
    """Run a callable on a QThread with progress/finished/failed signals.

    target signature:  target(progress_cb, cancel_token, **kwargs) -> dict
      progress_cb(dict): call for progress updates
      cancel_token: check .is_set() at safe points

    The main window keeps a reference until finished/failed fires.
    """

    def __init__(
        self,
        target: Callable[..., dict[str, Any]],
        kwargs: dict[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self.signals = WorkerSignals()
        self._target = target
        self._kwargs = kwargs or {}
        self._cancel = CancelToken()

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:  # noqa: D401 — QThread contract
        try:
            summary = self._target(
                progress_cb=self.signals.progress.emit,
                cancel_token=self._cancel,
                **self._kwargs,
            )
            self.signals.finished.emit(summary or {})
        except Cancelled:
            self.signals.finished.emit({"cancelled": True})
        except Exception:
            tb = traceback.format_exc()
            log.exception("BackgroundJob failed")
            self.signals.failed.emit(tb)
