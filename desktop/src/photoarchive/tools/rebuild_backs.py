"""Headless CLI for rebuild_back_proposals — same code path as the Ingest
panel's Rebuild button.

Usage: python -m photoarchive.tools.rebuild_backs
"""
from __future__ import annotations

import json
import logging
import sys

from .. import db
from ..config import load as load_config
from ..logging_setup import configure_logging
from ..modes.ingest.rebuild import rebuild_back_proposals
from ..workers import CancelToken

log = logging.getLogger(__name__)


def main() -> int:
    configure_logging()
    settings = load_config()
    db.init_pool(settings)

    def on_progress(payload: dict) -> None:
        if payload.get("kind") == "scored":
            print(f"scored {payload.get('n')}", file=sys.stderr, flush=True)

    try:
        summary = rebuild_back_proposals(
            settings=settings, roots=settings.master_roots,
            progress_cb=on_progress, cancel_token=CancelToken(),
        )
    finally:
        db.close_pool()

    print(json.dumps(summary.as_dict(), default=str, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
