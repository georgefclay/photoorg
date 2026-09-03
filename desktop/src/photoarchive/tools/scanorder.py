"""Diagnose per-scan-folder ordering trouble.

For every folder under a scan-kind root, prints:
  files                    total accepted files (not videos, not unsupported)
  styles                   distinct filename styles seen (timestamp / IMG /
                           IMG_YYYYMMDD / other)
  mtime_span               time between earliest and latest file mtime
  natsort==mtime           yes if natural-sort filename order equals mtime
                           order, no if they differ
  disagree                 number of positions where natsort and mtime rank
                           disagree
  back_runs (>=2)          count of runs of two-or-more consecutive proposed
                           backs when the folder is walked in the current
                           persisted `photos.scan_sequence`

Also identifies the folder that contains the ~100th proposal in the
current review order (ingest_pairings ORDER BY (scan_batch, back_scan_seq)),
i.e. "the batch after which the review went bad" per fix-up 3.

Usage:  python -m photoarchive.tools.scanorder [--root <label>] [--only <folder>]
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

from natsort import natsorted

from ..config import load as load_config
from ..logging_setup import configure_logging

TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{4}\.(jpe?g|tiff?|png)$", re.IGNORECASE)
IMG_RE = re.compile(r"^IMG_?\d+\.(jpe?g|tiff?|png)$", re.IGNORECASE)
IMG_DATED_RE = re.compile(r"^IMG_\d{8}_\d+\.(jpe?g|tiff?|png)$", re.IGNORECASE)


def _style(name: str) -> str:
    if IMG_DATED_RE.match(name):
        return "IMG_YYYYMMDD"
    if TIMESTAMP_RE.match(name):
        return "timestamp"
    if IMG_RE.match(name):
        return "IMG"
    return "other"


def _walk_folder(folder: Path) -> list[tuple[str, float]]:
    """Return [(filename, mtime), ...] for accepted files in `folder`."""
    from ..modes.ingest.scanner import ACCEPTED_EXTS
    out: list[tuple[str, float]] = []
    for entry in folder.iterdir():
        if entry.is_file() and entry.suffix.lower() in ACCEPTED_EXTS:
            try:
                out.append((entry.name, entry.stat().st_mtime))
            except OSError:
                continue
    return out


def _disagreements(files: list[tuple[str, float]]) -> tuple[int, int]:
    """Return (num_disagreements, mtime_span_seconds)."""
    if not files:
        return 0, 0
    by_name = natsorted(files, key=lambda x: x[0])
    by_mtime = sorted(files, key=lambda x: (x[1], x[0]))
    disagree = sum(1 for a, b in zip(by_name, by_mtime) if a[0] != b[0])
    span = int(max(f[1] for f in files) - min(f[1] for f in files))
    return disagree, span


def _run_counts(seq: list[bool]) -> int:
    """seq[i] = True if position i is a proposed back. Count runs of len >= 2."""
    runs = 0
    i = 0
    while i < len(seq):
        if seq[i]:
            j = i
            while j < len(seq) and seq[j]:
                j += 1
            if j - i >= 2:
                runs += 1
            i = j
        else:
            i += 1
    return runs


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    ap = argparse.ArgumentParser(prog="photoarchive.tools.scanorder")
    ap.add_argument("--root", help="restrict to this master root label")
    ap.add_argument("--only", help="restrict to a single top-level folder name")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap number of folders printed (still analyses all)")
    args = ap.parse_args(argv)

    settings = load_config()
    scan_roots = [r for r in settings.master_roots if r.kind == "scan"]
    if args.root:
        scan_roots = [r for r in scan_roots if r.label == args.root]
    if not scan_roots:
        print("no scan-kind master roots configured / matched", file=sys.stderr)
        return 2

    # --- Load per-photo state from DB -----------------------------------
    from .. import db
    db.init_pool(settings)
    back_seq_by_folder: dict[tuple[str, str], set[int]] = defaultdict(set)
    proposal_order: list[tuple[str, str, int]] = []  # (root, folder, seq)
    # (root, folder) -> [(scan_sequence, source_filename), ...] from photos.
    db_order_by_folder: dict[tuple[str, str], list[tuple[int, str]]] = defaultdict(list)
    with db.connection() as conn:
        conn.autocommit = True
        for row in conn.execute(
            """
            select p.source_root, ip.back_source_folder, ip.back_scan_sequence
            from ingest_pairings ip
            join photos p on p.id = ip.front_photo_id
            where ip.status = 'pending'
              and ip.back_scan_sequence is not null
            """
        ).fetchall():
            root_label, folder, seq = row
            back_seq_by_folder[(root_label, folder)].add(int(seq))
        for row in conn.execute(
            """
            select p.source_root, ip.back_source_folder, ip.back_scan_sequence
            from ingest_pairings ip
            join photos p on p.id = ip.front_photo_id
            where ip.status = 'pending'
              and ip.back_scan_sequence is not null
            order by p.scan_batch nulls last, ip.back_scan_sequence asc
            """
        ).fetchall():
            proposal_order.append((row[0], row[1], int(row[2])))
        for row in conn.execute(
            """
            select source_root, source_folder, scan_sequence, source_filename
            from photos
            where source_root = ANY(%s) and not is_deleted
              and scan_sequence is not null
            """,
            ([r.label for r in scan_roots],),
        ).fetchall():
            db_order_by_folder[(row[0], row[1])].append((int(row[2]), row[3]))
    db.close_pool()

    # --- Walk each scan root's folders ----------------------------------
    folders: list[dict] = []
    for root in scan_roots:
        if not root.path.exists():
            print(f"root {root.label}: {root.path} not present", file=sys.stderr)
            continue
        for entry in sorted(root.path.iterdir()):
            if not entry.is_dir():
                continue
            if args.only and entry.name != args.only:
                continue
            files = _walk_folder(entry)
            if not files:
                continue
            disagree, span = _disagreements(files)
            styles = sorted({_style(n) for n, _ in files})
            back_seqs = back_seq_by_folder.get((root.label, entry.name), set())
            # DB-order pass: what's stored in photos.scan_sequence?
            db_pairs = sorted(db_order_by_folder.get((root.label, entry.name), []))
            db_ordered_names = [name for _, name in db_pairs]
            # Disk mtime order:
            mtime_ordered = [
                n for n, _ in sorted(files, key=lambda p: (p[1], p[0]))
            ]
            # DB matches mtime order? Only meaningful if the DB knows about
            # every disk file (some may be held as proposed backs and not
            # yet in `photos`); compare the intersection preserving order.
            db_set = set(db_ordered_names)
            mtime_shared = [n for n in mtime_ordered if n in db_set]
            db_matches_mtime = db_ordered_names == mtime_shared
            # Back-run detection uses the current DB scan_sequence, not disk
            # order — this is the "review-order sanity" check that actually
            # improves after Recompute Scan Order.
            back_flags = [(seq in back_seqs) for seq, _ in db_pairs]
            back_runs = _run_counts(back_flags)
            folders.append({
                "root": root.label,
                "folder": entry.name,
                "files": len(files),
                "styles": ",".join(styles),
                "span_s": span,
                "natsort_eq_mtime": disagree == 0,
                "disagree": disagree,
                "db_matches_mtime": db_matches_mtime,
                "back_runs": back_runs,
            })

    # --- Print ---------------------------------------------------------
    header = (
        f"{'root':8}  {'folder':32}  {'files':>5}  {'styles':22}  "
        f"{'span':>10}  {'nat=mt':6}  {'db=mt':6}  {'disagree':>8}  {'back_runs':>9}"
    )
    print(header)
    print("-" * len(header))
    problematic = 0
    for f in folders:
        span_h = f"{f['span_s']//3600}h{(f['span_s']%3600)//60:02d}m"
        # "review-order sane" = db_matches_mtime AND no back_runs. natsort
        # disagreements are a property of disk names, informational only.
        flag = "OK" if f["db_matches_mtime"] and f["back_runs"] == 0 else "!!"
        print(
            f"{f['root']:8}  {f['folder']:32}  {f['files']:>5}  "
            f"{f['styles']:22}  {span_h:>10}  "
            f"{'yes' if f['natsort_eq_mtime'] else 'no':>6}  "
            f"{'yes' if f['db_matches_mtime'] else 'no':>6}  "
            f"{f['disagree']:>8}  {f['back_runs']:>9}  {flag}"
        )
        if not f["db_matches_mtime"] or f["back_runs"] > 0:
            problematic += 1
    print()
    print(f"folders total {len(folders)}, problematic {problematic}")
    if proposal_order:
        # Fix-up 3 says George reviewed ~100 in order before it went bad.
        idx = min(99, len(proposal_order) - 1)
        r, folder, seq = proposal_order[idx]
        print(f"proposal #{idx + 1} (0-indexed {idx}): root={r} folder={folder} seq={seq}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
