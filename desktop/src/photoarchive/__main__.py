from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from . import db
from .config import load as load_config
from .logging_setup import configure_logging

log = logging.getLogger(__name__)


def _resolve_env_path() -> Path:
    """Best-effort .env location for the settings dialog to write back to."""
    for c in (Path("desktop/.env"), Path(".env"),
              Path(__file__).resolve().parents[2] / ".env"):
        if c.exists():
            return c.resolve()
    return Path(".env").resolve()


def main() -> int:
    parser = argparse.ArgumentParser(prog="photoarchive")
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Open the window, close it after 1s, and exit 0. Used by smoke checks.",
    )
    args = parser.parse_args()

    log_path = configure_logging()
    log.info("Photo Archive starting; log at %s", log_path)

    app = QApplication(sys.argv)

    try:
        settings = load_config()
    except Exception as e:
        log.exception("Config load failed")
        QMessageBox.critical(None, "Config error", str(e))
        return 2

    try:
        db.init_pool(settings)
    except Exception as e:
        log.exception("DB pool failed")
        QMessageBox.critical(
            None, "Database error",
            f"Could not connect to {settings.DATABASE_URL}: {e}",
        )
        return 3

    from .app.main_window import MainWindow
    window = MainWindow(settings, env_path=_resolve_env_path())
    window.show()

    if args.smoke:
        QTimer.singleShot(1000, app.quit)

    rc = app.exec()
    db.close_pool()
    return rc


if __name__ == "__main__":
    sys.exit(main())
