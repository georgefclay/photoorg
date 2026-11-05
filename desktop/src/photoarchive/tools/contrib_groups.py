"""File already-ingested contributions into the groups they were approved for.

Ingest only started reading `manifest.json`'s approved groups on
2026-11-05; the first four contributions (#14468–14471) came in before
that and sat unfiled. This finds every live photo from a `contrib` root
that has **no `photo_groups` row at all** — a removed row counts as a
decision and is left alone — and applies its manifest's groups through
the same `contrib_groups.apply_groups` ingest now uses.

    python -m photoarchive.tools.contrib_groups            # report
    python -m photoarchive.tools.contrib_groups --apply    # write

Push afterwards: the photos' rows and bytes go up with their groups.
"""

from __future__ import annotations

import argparse
import sys

from .. import db as dbmod
from ..config import load as load_config
from ..logging_setup import configure_logging
from ..modes.ingest import contrib_groups


def candidates(conn, labels: list[str]) -> list[tuple[int, str, str, str]]:
    return conn.execute(
        """
        select p.id, p.source_root, p.source_folder, p.source_filename
          from photos p
         where p.source_root = any(%s)
           and not p.is_deleted
           and not exists (select 1 from photo_groups pg where pg.photo_id = p.id)
         order by p.id
        """,
        (labels,),
    ).fetchall()


def run(settings, *, apply: bool) -> list[dict]:
    roots = {r.label: r for r in settings.master_roots if r.kind == "contrib"}
    out: list[dict] = []
    if not roots:
        return out
    with dbmod.connection() as conn:
        conn.autocommit = False
        for pid, label, folder, filename in candidates(conn, list(roots)):
            cid, group_ids = contrib_groups.approved_groups(roots[label].path, folder, filename)
            added = (contrib_groups.apply_groups(conn, pid, group_ids, contribution_id=cid,
                                                 actor="desktop:contrib_groups")
                     if apply else [])
            out.append({"photo_id": pid, "contribution_id": cid,
                        "approved": group_ids, "added": added})
        if apply:
            conn.commit()
        else:
            conn.rollback()
    return out


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(prog="photoarchive.tools.contrib_groups")
    parser.add_argument("--apply", action="store_true", help="write the groups (default: report only)")
    args = parser.parse_args(argv)

    settings = load_config()
    dbmod.init_pool(settings)
    try:
        rows = run(settings, apply=args.apply)
    finally:
        dbmod.close_pool()

    if not rows:
        print("No unfiled contributed photos.")
        return 0
    for r in rows:
        verb = "added to" if args.apply else "would add to"
        groups = r["added"] if args.apply else r["approved"]
        print(f"#{r['photo_id']} (contribution {r['contribution_id']}): {verb} groups {groups or 'none'}")
    if not args.apply:
        print("Report only. Re-run with --apply to write, then push.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
