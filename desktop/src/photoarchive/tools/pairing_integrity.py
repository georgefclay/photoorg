"""Report pairings whose DB decision and on-disk state disagree.

Two checks:
  1. Recent pairings (decided in the last 24 h): if `status='accepted'`,
     the matching photo_backs row must exist and its `working_path` must
     point at a real file; for photo-as-back proposals the demoted
     photos row must be `is_deleted=true`. If any of that is off, the
     accept half-applied.
  2. All photo_backs: `working_path` must exist on disk. Rows whose file
     is missing usually mean an accept-time move failure that was
     never repaired.

Also repopulates decisions._NEEDS_FILE_REPAIR from the DB so the status
bar catches up after a restart. Report-only for repairs today —
manual reconciliation is safer than automatic file surgery. Print the
recommended fix per row.

Usage:
    python -m photoarchive.tools.pairing_integrity
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .. import db
from ..config import Settings, load as load_config
from ..logging_setup import configure_logging
from ..modes.ingest import decisions

log = logging.getLogger(__name__)


def check_all(settings: Settings) -> dict:
    """Run both checks, return a summary dict, and update the in-memory
    needs_file_repair set as a side effect."""
    with db.connection() as conn:
        conn.autocommit = True
        recent = conn.execute(
            """
            select ip.id, ip.status, ip.back_photo_id, ip.decided_at,
                   pb.id as photo_back_id, pb.working_path,
                   p.is_deleted, p.physical_ref_note
            from ingest_pairings ip
            left join photo_backs pb on pb.sha256 = ip.back_sha256
            left join photos p on p.id = ip.back_photo_id
            where ip.decided_at is not null
              and ip.decided_at > now() - interval '24 hours'
            order by ip.id
            """
        ).fetchall()

        backs_all = conn.execute(
            """
            select id, working_path from photo_backs
            where working_path is not null
            order by id
            """
        ).fetchall()

    recent_issues: list[dict] = []
    for r in recent:
        (pair_id, status, back_pid, decided_at,
         pb_id, pb_wpath, photo_is_del, photo_note) = r
        if status != "accepted":
            continue
        if pb_id is None:
            recent_issues.append({
                "pairing_id": pair_id, "issue": "no_photo_backs_row",
                "hint": "accept committed but photo_backs.sha256 lookup "
                        "failed; the DB is half-applied",
            })
            decisions.mark_file_repair_needed(pair_id)
            continue
        if not pb_wpath or not Path(pb_wpath).exists():
            recent_issues.append({
                "pairing_id": pair_id, "issue": "photo_backs_file_missing",
                "photo_back_id": pb_id, "recorded_path": pb_wpath,
                "hint": "file move failed after DB commit; move the file "
                        "into place or clear photo_backs.working_path",
            })
            decisions.mark_file_repair_needed(pair_id)
        if back_pid is not None and not photo_is_del:
            recent_issues.append({
                "pairing_id": pair_id, "issue": "photo_not_demoted",
                "back_photo_id": back_pid,
                "hint": "photo-as-back accept committed but the photos "
                        "row is still active — should be is_deleted=true",
            })
            decisions.mark_file_repair_needed(pair_id)

    orphaned_backs: list[dict] = []
    for pb_id, wp in backs_all:
        if not Path(wp).exists():
            orphaned_backs.append({
                "photo_back_id": pb_id, "recorded_path": wp,
            })

    return {
        "checked_recent_pairings": len(recent),
        "recent_issues": recent_issues,
        "checked_all_photo_backs": len(backs_all),
        "orphaned_backs": orphaned_backs,
        "needs_file_repair_ids": sorted(decisions.needs_file_repair_ids()),
    }


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(
        prog="photoarchive.tools.pairing_integrity",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Print the summary as JSON instead of a human-readable report",
    )
    args = parser.parse_args(argv)

    settings = load_config()
    db.init_pool(settings)
    try:
        summary = check_all(settings)
    finally:
        db.close_pool()

    if args.json:
        print(json.dumps(summary, default=str, indent=2))
        return 0 if _clean(summary) else 1

    print(f"Recent pairings checked (last 24 h): "
          f"{summary['checked_recent_pairings']}")
    if summary["recent_issues"]:
        print(f"Half-applied accepts: {len(summary['recent_issues'])}")
        for r in summary["recent_issues"]:
            print(f"  pairing {r['pairing_id']}: {r['issue']}")
            for k, v in r.items():
                if k in ("pairing_id", "issue"):
                    continue
                print(f"    {k}: {v}")
    else:
        print("  (none)")

    print()
    print(f"photo_backs rows checked: {summary['checked_all_photo_backs']}")
    if summary["orphaned_backs"]:
        print(f"Rows whose working file is missing: "
              f"{len(summary['orphaned_backs'])}")
        for b in summary["orphaned_backs"][:20]:
            print(f"  photo_back {b['photo_back_id']}: "
                  f"missing {b['recorded_path']}")
        if len(summary["orphaned_backs"]) > 20:
            print(f"  … and {len(summary['orphaned_backs']) - 20} more")
    else:
        print("  (all present)")

    print()
    ids = summary["needs_file_repair_ids"]
    print(f"needs_file_repair set: {len(ids)} pairings")
    return 0 if _clean(summary) else 1


def _clean(summary: dict) -> bool:
    return (not summary["recent_issues"]
            and not summary["orphaned_backs"])


if __name__ == "__main__":
    sys.exit(main())
