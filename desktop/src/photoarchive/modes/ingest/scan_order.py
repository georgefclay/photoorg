"""Compute scan_sequence for scan-kind folders.

The original ingest ordered files by filename natural-sort. That's wrong
when a folder has files from several different scanning sessions (mixed
IMG_* and timestamp names) or when filenames don't reflect physical
scanning order. The scanner writes files in envelope order, so **mtime**
is the truth.

Rule (mtime primary, filename natsort as tie-break) with two fallbacks
to filename order:
  - if all mtimes are identical (span == 0) — likely a copy that flattened
    mtimes;
  - if the mtime span is > MAX_REASONABLE_SPAN (default 5 years) — likely
    corrupt/FAT32 mtimes.

`recompute_folder` returns an ordered list of (filename, sequence) plus
a `fallback_used` flag.
"""
from __future__ import annotations

import logging
from pathlib import Path

from natsort import natsort_keygen, natsorted

from .scanner import ACCEPTED_EXTS

log = logging.getLogger(__name__)

MAX_REASONABLE_SPAN_SEC = 5 * 365 * 24 * 3600  # 5 years

_natkey = natsort_keygen()


def order_files(files: list[tuple[str, float]]) -> tuple[list[str], bool]:
    """Given [(filename, mtime), ...] return (ordered_filenames, fallback_used).

    fallback_used=True when the mtime signal is unusable (all-identical
    or absurdly large span) and we fell back to filename natsort only.
    """
    if not files:
        return [], False
    mtimes = [m for _, m in files]
    span = max(mtimes) - min(mtimes)
    fallback = span == 0.0 or span > MAX_REASONABLE_SPAN_SEC
    if fallback:
        ordered = [n for n, _ in natsorted(files, key=lambda p: p[0])]
    else:
        ordered = [n for n, _ in sorted(files, key=lambda p: (p[1], _natkey(p[0])))]
    return ordered, fallback


def walk_folder_for_ordering(folder: Path) -> list[tuple[str, float]]:
    """List accepted files as (filename, mtime). Skips unsupported and probe files."""
    out: list[tuple[str, float]] = []
    if not folder.exists() or not folder.is_dir():
        return out
    for entry in folder.iterdir():
        if not entry.is_file():
            continue
        if entry.name.startswith("._photoarchive_write_probe"):
            continue
        if entry.suffix.lower() not in ACCEPTED_EXTS:
            continue
        try:
            out.append((entry.name, entry.stat().st_mtime))
        except OSError:
            continue
    return out
