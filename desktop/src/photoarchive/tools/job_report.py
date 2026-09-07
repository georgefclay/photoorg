"""Per-job status report — handed-over / processed on mini / collected /
ETA. Prints a compact table. No side effects.

Usage:
    python -m photoarchive.tools.job_report
    python -m photoarchive.tools.job_report --json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from .. import db as dbmod
from ..config import load as load_config
from ..inference_client import JOB_TO_ENDPOINT, QUEUE_ORDER, shared
from ..logging_setup import configure_logging
from ..modes.jobs.stats import load_local_stats

log = logging.getLogger(__name__)


def gather() -> list[dict]:
    stats = load_local_stats()
    client = shared.client()
    out: list[dict] = []
    try:
        hs = client.health()
    except Exception as e:
        log.warning("service /health unreachable: %s", e)
        hs = None

    for name in QUEUE_ORDER:
        s = stats[name]
        summary = None
        try:
            summary = client.summary(name)
        except Exception as e:
            log.debug("summary(%s) unavailable: %s", name, e)
        inbox_from_health = None
        if hs is not None:
            inbox_from_health = (hs.inbox or {}).get(name)
        out.append({
            "job": name,
            "endpoint": JOB_TO_ENDPOINT[name],
            "eligible": s.eligible,
            "handed_over": inbox_from_health,
            "processed_on_mini": summary.done if summary else None,
            "collected_in_db": s.done_in_db,
            "cursor": s.cursor,
            "eta_seconds": summary.eta_seconds if summary else None,
            "running": summary.running if summary else None,
            "blackout_active": (hs.blackout_active if hs else None),
        })
    return out


def print_table(rows: list[dict]) -> None:
    hdr = ("job", "eligible", "handed", "processed", "collected", "cursor", "eta", "running")
    widths = [max(len(h), 6) for h in hdr]
    fmt = "  ".join(f"{{:{w}}}" for w in widths)
    print(fmt.format(*hdr))
    print(fmt.format(*("-" * w for w in widths)))
    for r in rows:
        eta = _fmt_eta(r["eta_seconds"])
        vals = (
            r["job"],
            str(r["eligible"]),
            "-" if r["handed_over"] is None else str(r["handed_over"]),
            "-" if r["processed_on_mini"] is None else str(r["processed_on_mini"]),
            str(r["collected_in_db"]),
            str(r["cursor"]),
            eta,
            "-" if r["running"] is None else ("yes" if r["running"] else "no"),
        )
        print(fmt.format(*vals))


def _fmt_eta(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds/60:.0f}m"
    if seconds < 86400 * 2:
        return f"{seconds/3600:.1f}h"
    return f"{seconds/86400:.1f}d"


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(prog="photoarchive.tools.job_report")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a table")
    args = parser.parse_args(argv)

    settings = load_config()
    dbmod.init_pool(settings)
    try:
        rows = gather()
    finally:
        dbmod.close_pool()

    if args.json:
        print(json.dumps(rows, default=str, indent=2))
    else:
        print_table(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
