"""Print the back-detector's per-file components for a scan-kind root.

Usage:
    python -m photoarchive.tools.backscore <root-label> [<batch>]
    python -m photoarchive.tools.backscore scans "Batch 00001"

Prints a fixed-width table so the failure mode is obvious. Currently reads
the *current* back_detect.analyse output; when the scorer is rewritten,
this tool automatically picks up the new fields.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import fields
from pathlib import Path

from ..config import load as load_config
from ..logging_setup import configure_logging
from ..modes.ingest import back_detect
from ..modes.ingest.image_io import open_image
from ..modes.ingest.scanner import walk_root


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    ap = argparse.ArgumentParser(prog="photoarchive.tools.backscore")
    ap.add_argument("root", help="master root label")
    ap.add_argument("batch", nargs="?", default=None,
                    help="restrict to this scan_batch (top-level folder)")
    ap.add_argument("--seq-range", nargs=2, type=int, metavar=("LO", "HI"),
                    help="restrict to scan_sequence in [LO, HI]")
    args = ap.parse_args(argv)

    settings = load_config()
    root = settings.master_root_by_label(args.root)
    if root is None:
        print(f"unknown root label {args.root!r}", file=sys.stderr)
        return 2

    field_names = [f.name for f in fields(back_detect.BackFeatures)]
    header = ["seq", "filename"] + field_names
    widths = {"seq": 5, "filename": 30}
    fmt_row = lambda cells: "  ".join(  # noqa: E731
        str(c).ljust(widths.get(h, 12))[:widths.get(h, 12)]
        if isinstance(c, str) else f"{c:<12.4f}"[:12]
        for h, c in zip(header, cells)
    )
    print("  ".join(h.ljust(widths.get(h, 12)) for h in header))
    print("-" * (len(fmt_row(header + [0] * (len(field_names) - len(header) + 2)))))

    for f in walk_root(root):
        if args.batch is not None and f.scan_batch != args.batch:
            continue
        if args.seq_range is not None:
            lo, hi = args.seq_range
            if f.scan_sequence is None or not (lo <= f.scan_sequence <= hi):
                continue
        if f.is_video:
            continue
        try:
            with open_image(f.master_path) as img:
                img.load()
                feats = back_detect.analyse(img)
        except Exception as e:
            print(f"{f.scan_sequence:5}  {f.source_filename:30}  ERROR: {e}")
            continue
        row = [f.scan_sequence, f.source_filename] + [
            getattr(feats, name) for name in field_names
        ]
        print(fmt_row(row))
    return 0


if __name__ == "__main__":
    sys.exit(main())
