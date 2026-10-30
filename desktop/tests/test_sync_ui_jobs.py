"""Phase 13 fix-up 1, item 6: the Sync tab's job slot, offscreen.

The app vanished the moment George's first GUI push finished. The old
`finished` handler was a lambda; PySide6 runs a receiver-less lambda on the
emitting thread, so it touched widgets from the worker thread. These pin:

* every result reaches the panel **on the GUI thread**;
* one job at a time — a second click while one runs is refused and every
  work button is disabled until the thread has finished;
* the (thread, worker) pair stays referenced until the thread's `finished`;
* `shutdown()` stops a running job at its next safe point and joins it;
* stage starts and ends go to the log file, with memory.
"""
from __future__ import annotations

import logging
import os
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from photoarchive.modes import sync as sync_mod
from photoarchive.modes.sync import PushProgress

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


@pytest.fixture
def panel(monkeypatch):
    _app()
    # No database here: the due count is advice and its failure is logged.
    monkeypatch.setattr(sync_mod, "count_due_job", lambda: {"due": 2, "in_group": 1})
    p = sync_mod.SyncPanel()
    yield p
    p.shutdown(5_000)


def _pump_until(cond, timeout=10.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        _app().processEvents()
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached")


def test_results_land_on_the_gui_thread_and_the_slot_frees(panel):
    gui = threading.get_ident()

    # The real slots are observed through what they touch, never patched:
    # a plain function patched over a @Slot name — on the instance or even
    # on the class — is run by PySide6 on the *emitting* thread, which is
    # the bug itself and would make this test meaningless.
    class _Log:
        def __init__(self):
            self.lines: list[tuple[str, int]] = []

        def append(self, msg):
            self.lines.append((msg, threading.get_ident()))

    log_widget = _Log()

    def job(status):
        assert threading.get_ident() != gui
        status("halfway")
        return {"n": 1}

    panel._run_worker("pull groups", job, log_widget=log_widget)
    assert all(not b.isEnabled() for b in panel._work_buttons)
    assert len(panel._live_jobs) == 1
    _pump_until(lambda: panel._job is None)

    assert log_widget.lines == [("halfway", gui), ("done: {'n': 1}", gui)]
    assert "Pull groups done: {'n': 1}" == panel._status.text()
    # The metadata offer, set from the result — on the GUI thread.
    assert panel._btn_meta.text() == "Write metadata for 2 changed photos"
    assert panel._live_jobs == []
    assert all(b.isEnabled() for b in panel._work_buttons)
    assert panel._rss.text().startswith("memory ")


def test_one_job_at_a_time(panel):
    gate = threading.Event()

    def slow(status):
        gate.wait(10)
        return {}

    panel._run_worker("push", slow, log_widget=panel._push_log)
    started = []
    panel._run_worker_orig = panel._run_worker
    panel._on_pull_groups()           # a second click while the push runs
    assert panel._status.text() == "Push is running; one sync job at a time."
    assert len(panel._live_jobs) == 1 and panel.running_job() == "push"
    gate.set()
    _pump_until(lambda: panel._job is None)
    assert panel.running_job() is None
    assert not started


def test_a_failure_is_shown_and_frees_the_slot(panel):
    def boom(status):
        raise RuntimeError("web said 500")

    panel._run_worker("push", boom, log_widget=panel._push_log)
    _pump_until(lambda: panel._job is None)
    assert "FAILED: web said 500" in panel._push_log.toPlainText()
    assert panel._status.text() == "Push failed: web said 500"


def test_shutdown_stops_a_running_job_at_a_safe_point(panel, caplog):
    def endless(status):
        while True:
            status("tick")
            time.sleep(0.01)

    panel._run_worker("push", endless, log_widget=panel._push_log)
    time.sleep(0.1)
    with caplog.at_level(logging.INFO, logger="photoarchive.modes.sync"):
        panel.shutdown(5_000)
    thread, _worker = panel._live_jobs[0]
    assert thread.isFinished()
    assert any("stopped at a safe point" in r.message for r in caplog.records)
    _pump_until(lambda: panel._job is None)


def test_push_stages_are_logged_with_memory(panel, monkeypatch, caplog):
    def fake_push(client, *, progress, **kw):
        for stage, total in (("photos", 3), ("files", 2)):
            for i in range(1, total + 1):
                progress(PushProgress(stage, i, total))

        class S:
            photos_upserted, files_uploaded, bytes_uploaded, tables = 3, 2, 10, {}
        return S()

    monkeypatch.setattr(sync_mod, "push", fake_push)
    monkeypatch.setattr(panel, "_client_lazy", lambda: object())

    class _S:
        WORKING_DIR = "C:/w"
        THUMBS_DIR = "C:/t"
    monkeypatch.setattr(panel, "_settings_lazy", lambda: _S())

    with caplog.at_level(logging.INFO, logger="photoarchive.modes.sync"):
        panel._on_push()
        _pump_until(lambda: panel._job is None)
    msgs = [r.getMessage() for r in caplog.records]
    assert any("sync push: start" in m and "memory" in m for m in msgs)
    assert any("stage photos start" in m for m in msgs)
    assert any("stage photos end" in m and "photos: 3/3" in m for m in msgs)
    assert any("stage files end" in m and "files: 2/2" in m for m in msgs)
    assert any("sync push: done" in m and "'files': 2" in m for m in msgs)
