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
