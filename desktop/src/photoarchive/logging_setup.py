from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path


def log_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    d = Path(base) / "PhotoArchive" / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def configure_logging(level: int = logging.INFO) -> Path:
    """Root logger → stderr (INFO) + rotating file (5 MB × 3). Returns path."""
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    stream.setLevel(level)
    root.addHandler(stream)

    path = log_dir() / "photoarchive.log"
    fileh = logging.handlers.RotatingFileHandler(
        path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fileh.setFormatter(fmt)
    fileh.setLevel(level)
    root.addHandler(fileh)
    return path


# Kept open for the life of the process: faulthandler writes to the raw fd
# when the interpreter is already dying and cannot open anything.
_crash_file = None


def install_crash_handlers() -> Path:
    """Make every way this process can die leave its last words (Phase 13
    fix-up 1, item 6: a GUI push vanished with no traceback and no log line).

    * `sys.excepthook` / `threading.excepthook` — an uncaught Python error,
      including one raised inside a Qt slot, goes to the log with its stack.
    * a Qt message handler — Qt's own warnings, criticals and the fatal
      message it prints just before it aborts ("QThread: Destroyed while
      thread is still running") go to the log instead of a console nobody
      is watching.
    * `faulthandler` — a hard crash (access violation, abort) dumps every
      thread's Python stack to `crash.log` beside the log file; the logging
      module cannot run at that point.
    """
    import faulthandler
    import sys
    import threading

    global _crash_file
    log = logging.getLogger("photoarchive.crash")

    def excepthook(etype, value, tb):
        log.critical("uncaught exception", exc_info=(etype, value, tb))

    def thread_excepthook(args):
        log.critical("uncaught exception in thread %s", getattr(args.thread, "name", "?"),
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = excepthook
    threading.excepthook = thread_excepthook

    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler

        levels = {
            QtMsgType.QtDebugMsg: logging.DEBUG,
            QtMsgType.QtInfoMsg: logging.INFO,
            QtMsgType.QtWarningMsg: logging.WARNING,
            QtMsgType.QtCriticalMsg: logging.ERROR,
            QtMsgType.QtFatalMsg: logging.CRITICAL,
        }
        qt_log = logging.getLogger("qt")

        def qt_handler(mode, context, message):
            where = f" ({context.file}:{context.line})" if context and context.file else ""
            qt_log.log(levels.get(mode, logging.WARNING), "%s%s", message, where)
            if mode == QtMsgType.QtFatalMsg:
                for h in logging.getLogger().handlers:
                    h.flush()

        qInstallMessageHandler(qt_handler)
    except Exception:  # noqa: BLE001 — headless tools have no Qt to hook
        log.debug("Qt message handler not installed", exc_info=True)

    path = log_dir() / "crash.log"
    _crash_file = open(path, "a", encoding="utf-8")  # noqa: SIM115 — must stay open
    faulthandler.enable(file=_crash_file, all_threads=True)
    return path
