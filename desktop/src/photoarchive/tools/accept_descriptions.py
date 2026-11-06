"""Bulk-accept the AI descriptions (Phase 13 fix-up 2).

Usage:
    python -m photoarchive.tools.accept_descriptions --report
    python -m photoarchive.tools.accept_descriptions --apply [--chunk N]

`--report` counts what is due and writes nothing. `--apply` promotes every
pending `source='ai'` description on a kept, live photo into
`photos.description_ai`, rejects the folder-name `import` descriptions with a
note, and audits each row under one batch id (see
`modes.metadata.descriptions`). Commits per chunk, so a killed run keeps what
it finished; a second run finds 0 due. Afterwards run the metadata writer:
every touched photo is due by its payload hash.
"""
from __future__ import annotations

import argparse
import sys
import time

from .. import db as dbmod
from ..config import load as load_config
from ..logging_setup import configure_logging
from ..modes.metadata import descriptions, repo as meta_repo


def format_plan(p: descriptions.Plan) -> str:
    return "\n".join([
        f"AI descriptions to accept:        {len(p.accept)}",
        f"Older AI duplicates to reject:    {len(p.superseded)}",
        f"Import descriptions to reject:    {len(p.imports)}",
        f"Left pending (photo already has other text): {len(p.conflict)}",
        f"Left pending (no text):           {len(p.empty)}",
        f"Left pending (photo junk/deleted): {p.out_of_scope}",
    ])


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    ap = argparse.ArgumentParser(prog="photoarchive.tools.accept_descriptions")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--report", action="store_true", help="count only; writes nothing")
    mode.add_argument("--apply", action="store_true", help="accept / reject and audit")
    ap.add_argument("--chunk", type=int, default=descriptions.DEFAULT_CHUNK)
    args = ap.parse_args(argv)

    dbmod.init_pool(load_config())
    try:
        with dbmod.connection() as conn:
            conn.autocommit = False
            p = descriptions.plan(conn)
            conn.rollback()
            print(format_plan(p))
            if args.report:
                return 0
            t0 = time.monotonic()
            res = descriptions.apply(conn, p, chunk=args.chunk, progress=print)
            elapsed = time.monotonic() - t0
            after = descriptions.plan(conn)
            conn.rollback()
            conn.autocommit = True
            due, grouped = meta_repo.count_due(conn)
    finally:
        dbmod.close_pool()
    print(f"\nBatch {res.batch_id}: "
          + ", ".join(f"{k}={v}" for k, v in sorted(res.counts.items()))
          + f" in {elapsed:.1f} s")
    print(f"Still due after the run: {after.due}")
    print(f"Metadata writer: {due} photos now due ({grouped} in a live group re-push on the next push)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
