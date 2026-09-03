"""Headless CLI for the Recompute-scan-order action.

Usage: python -m photoarchive.tools.recompute_order
"""
from __future__ import annotations

import json
import logging
import sys

from .. import db
from ..config import load as load_config
from ..logging_setup import configure_logging
from ..modes.ingest.recompute_order import recompute_scan_order
from ..workers import CancelToken

log = logging.getLogger(__name__)


def main() -> int:
    configure_logging()
    settings = load_config()
    db.init_pool(settings)

    def on_progress(payload: dict) -> None:
        if payload.get("kind") == "folder_done":
            mark = " (fallback)" if payload.get("fallback") else ""
            print(
                f"{payload.get('root')}/{payload.get('folder')}  "
                f"photos={payload.get('n_photos')} updated={payload.get('photos_updated')}{mark}",
                file=sys.stderr, flush=True,
            )

    try:
        summary = recompute_scan_order(
            settings=settings, roots=settings.master_roots,
            progress_cb=on_progress, cancel_token=CancelToken(),
        )
    finally:
        db.close_pool()

    print(json.dumps(summary.as_dict(), default=str, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
