"""Headless triage presort runner. Same code path as the UI's
'Compute hints' button; progress goes to stderr.

Usage:
    python -m photoarchive.tools.run_presort
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from .. import db
from ..config import load as load_config
from ..logging_setup import configure_logging
from ..modes.triage.presort import run_presort
from ..workers import CancelToken

log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(prog="photoarchive.tools.run_presort")
    parser.parse_args(argv)

    settings = load_config()
    db.init_pool(settings)
    cancel = CancelToken()
    last_print = [time.monotonic()]

    def on_progress(payload: dict) -> None:
        kind = payload.get("kind")
        if kind == "start":
            print(f"presort: {payload.get('total')} photos to classify",
                  file=sys.stderr, flush=True)
            return
        if kind == "progress":
            now = time.monotonic()
            if now - last_print[0] < 2.0:
                return
            last_print[0] = now
            print(f"  {payload.get('done')}/{payload.get('total')}",
                  file=sys.stderr, flush=True)
            return
        if kind == "done":
            print("classify pass done; running burst pass…",
                  file=sys.stderr, flush=True)

    try:
        summary = run_presort(progress_cb=on_progress, cancel_token=cancel)
    finally:
        db.close_pool()

    print(json.dumps(summary, default=str, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
