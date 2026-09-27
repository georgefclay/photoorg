"""Write a Cleanup run report from the command line.

    python -m photoarchive.tools.cleanup_report [--batch "Batch 00001" ...]
                                               [--samples 10]

Same output as the Cleanup panel's "Write report" button: `report.md`,
`summary.json` and `contact-sheet.html` plus the before/after pair JPEGs,
under `CLEANUP_DIR/_report/<timestamp>/`.
"""
from __future__ import annotations

import argparse
import logging
import sys

from .. import db
from ..config import load as load_config
from ..logging_setup import configure_logging
from ..modes.cleanup import report as report_mod

log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", action="append", dest="batches",
                        help="restrict to this scan_batch (repeatable)")
    parser.add_argument("--samples", type=int, default=report_mod.DEFAULT_SAMPLES,
                        help="how many before/after pairs to render")
    parser.add_argument("--title", default="Cleanup analysis")
    args = parser.parse_args(argv)

    configure_logging()
    settings = load_config()
    db.init_pool(settings)
    try:
        paths = report_mod.write_report(
            settings, samples=args.samples, batches=args.batches,
            title=args.title,
        )
    finally:
        db.close_pool()

    print(f"Report:        {paths.markdown}")
    print(f"Summary JSON:  {paths.summary}")
    print(f"Contact sheet: {paths.contact_sheet}")
    print(paths.markdown.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
