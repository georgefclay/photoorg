"""Bulk-reject pending ingest_pairings using a JSON file the contact sheet
produced. Each id is put through the normal reject path (held staged file
inserted as a normal photo; photo-as-back left alone).

Usage: python -m photoarchive.tools.reject_from_contact <path/to/marked.json>
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .. import db
from ..config import load as load_config
from ..logging_setup import configure_logging
from ..modes.ingest import decisions

log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    ap = argparse.ArgumentParser(prog="photoarchive.tools.reject_from_contact")
    ap.add_argument("json_path", help="marked-ids JSON file from the contact sheet")
    args = ap.parse_args(argv)

    data = json.loads(Path(args.json_path).read_text(encoding="utf-8"))
    ids: list[int] = list(data.get("rejected_pairing_ids") or [])
    if not ids:
        print("no ids to reject", file=sys.stderr)
        return 1

    settings = load_config()
    db.init_pool(settings)
    ok = 0
    skipped = 0
    failed = 0
    try:
        for pid in ids:
            try:
                decisions.reject_pairing(settings, int(pid))
                ok += 1
            except ValueError as e:
                log.warning("skip pairing %s: %s", pid, e)
                skipped += 1
            except Exception as e:
                log.exception("reject pairing %s failed", pid)
                failed += 1
    finally:
        db.close_pool()

    print(json.dumps({"rejected": ok, "skipped": skipped, "failed": failed}, indent=2))
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
