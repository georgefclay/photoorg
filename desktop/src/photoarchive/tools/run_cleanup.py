"""Run `cleanup_analyse` from the command line.

    python -m photoarchive.tools.run_cleanup [--batch "Batch 00001" ...]
                                            [--reanalyse] [--limit N]
                                            [--no-previews] [--report]

The masters guard runs first, exactly as it does in the panel: any writable
master root refuses the run (invariant 4). Resumable — without `--reanalyse`
photos that already have a proposal are skipped, so killing it at N and
starting again continues at N.
"""
from __future__ import annotations

import argparse
import logging
import sys

from .. import db
from ..config import load as load_config
from ..logging_setup import configure_logging
from ..modes.cleanup import job as job_mod
from ..modes.cleanup import report as report_mod
from ..workers import CancelToken, Cancelled

log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", action="append", dest="batches",
                        help="restrict to this scan_batch (repeatable)")
    parser.add_argument("--reanalyse", action="store_true",
                        help="re-measure photos that already have a proposal")
    parser.add_argument(
        "--photo", dest="photos", action="append", default=[],
        help="restrict to these photo ids (repeatable, or comma-separated). "
             "Implies --reanalyse: a named photo is one you mean to re-measure.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--no-previews", action="store_true",
                        help="skip the ~2000 px preview render (faster; the "
                             "review pane renders on demand anyway)")
    parser.add_argument("--report", action="store_true",
                        help="write the run report when the pass finishes")
    parser.add_argument("--samples", type=int, default=report_mod.DEFAULT_SAMPLES)
    args = parser.parse_args(argv)
    # --photo 1,2 and --photo 1 --photo 2 mean the same thing.
    photo_ids = [int(v) for chunk in args.photos
                 for v in str(chunk).replace(" ", "").split(",") if v]

    configure_logging()
    settings = load_config()
    db.init_pool(settings)

    last = {"done": 0}

    def progress(payload: dict) -> None:
        kind = payload.get("kind")
        if kind == "start":
            print(f"Analysing {payload.get('total', 0)} scans…", flush=True)
        elif kind == "item":
            done = int(payload.get("done") or 0)
            if done - last["done"] >= 25 or payload.get("status") == "failed":
                last["done"] = done
                print(f"  [{done}/{payload.get('total', '?')}] "
                      f"photo {payload.get('photo_id')}: "
                      f"{payload.get('status')}"
                      + (f" ({payload['ms']} ms)" if payload.get("ms") else ""),
                      flush=True)

    try:
        stats = job_mod.run_cleanup_analyse(
            settings, reanalyse=args.reanalyse or bool(photo_ids),
            batches=args.batches, photo_ids=photo_ids,
            limit=args.limit, progress_cb=progress,
            cancel_token=CancelToken(),
            write_previews=not args.no_previews,
        )
    except job_mod.MastersWritable as e:
        print(str(e), file=sys.stderr)
        db.close_pool()
        return 2
    except Cancelled:
        print("Cancelled; re-run to continue where it stopped.")
        db.close_pool()
        return 1

    print()
    print(stats.report())

    if args.report:
        paths = report_mod.write_report(
            settings, stats=stats.to_dict(), samples=args.samples,
            batches=args.batches,
            title=("Cleanup analysis — " + ", ".join(args.batches)
                   if args.batches else "Cleanup analysis — full scope"),
        )
        print()
        print(f"Report:        {paths.markdown}")
        print(f"Contact sheet: {paths.contact_sheet}")

    db.close_pool()
    return 0


if __name__ == "__main__":
    sys.exit(main())
