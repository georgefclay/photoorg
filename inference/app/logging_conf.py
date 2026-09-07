"""Structured JSON logging, one line per request, rotated daily."""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
import time
from typing import Any

from .config import get_settings

LOGGER_NAME = "inference"
_configured = False


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging() -> logging.Logger:
    global _configured
    log = logging.getLogger(LOGGER_NAME)
    if _configured:
        return log

    settings = get_settings()
    settings.log_dir.mkdir(parents=True, exist_ok=True)

    log.setLevel(logging.INFO)
    log.propagate = False

    file_handler = logging.handlers.TimedRotatingFileHandler(
        settings.log_dir / "inference.log", when="midnight", backupCount=30, utc=True
    )
    file_handler.suffix = "%Y-%m-%d"
    file_handler.setFormatter(JsonFormatter())
    log.addHandler(file_handler)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(JsonFormatter())
    log.addHandler(stream)

    _configured = True
    return log


def get_logger() -> logging.Logger:
    return setup_logging()


def log_event(msg: str, level: int = logging.INFO, **fields: Any) -> None:
    get_logger().log(level, msg, extra={"fields": fields})
