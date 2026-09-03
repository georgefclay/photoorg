"""Headless ingest runner for verification and cron. Same code path as the
UI's Start button; progress goes to stderr instead of Qt signals.

Usage:
    python -m photoarchive.tools.run_ingest [--only <label> ...]
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
from ..modes.ingest.service import GuardRefused, run_ingest
from ..workers import CancelToken

log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(prog="photoarchive.tools.run_ingest")
    parser.add_argument("--only", action="append", default=[],
                        help="root label(s) to include; default = all")
    args = parser.parse_args(argv)

    settings = load_config()
    all_roots = settings.master_roots
    if args.only:
        picked = [r for r in all_roots if r.label in args.only]
        missing = set(args.only) - {r.label for r in picked}
        if missing:
            print(f"unknown root labels: {sorted(missing)}", file=sys.stderr)
            return 2
        roots = picked
    else:
        roots = all_roots

    db.init_pool(settings)
    cancel = CancelToken()

    last_print = [time.monotonic()]

    def on_progress(payload: dict) -> None:
        if payload.get("kind") != "counts":
            return
        now = time.monotonic()
        if now - last_print[0] < 2.0:
            return
        last_print[0] = now
        c = payload.get("counts") or {}
        parts = " ".join(f"{k}={v}" for k, v in c.items())
        print(f"[{payload.get('root')}] {parts}  last={payload.get('last')}",
              file=sys.stderr, flush=True)

    try:
        summary = run_ingest(
            settings=settings, roots=roots,
            progress_cb=on_progress, cancel_token=cancel,
        )
    except GuardRefused as e:
        print(str(e), file=sys.stderr)
        return 3
    finally:
        db.close_pool()

    print(json.dumps(summary.as_dict(), default=str, indent=2))
    return 0 if not summary.cancelled else 130


if __name__ == "__main__":
    sys.exit(main())
