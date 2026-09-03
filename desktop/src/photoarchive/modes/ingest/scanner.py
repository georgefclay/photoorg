from __future__ import annotations

import logging
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from natsort import natsorted

from ...config import MasterRoot

log = logging.getLogger(__name__)

IMAGE_EXTS = {".jpg", ".jpeg", ".tif", ".tiff", ".png", ".heic"}
VIDEO_EXTS = {".mp4", ".mov"}
ACCEPTED_EXTS = IMAGE_EXTS | VIDEO_EXTS

_BATCH_RE = re.compile(r"^Batch \d{5}$", re.IGNORECASE)
_DATE_FOLDER_RE = re.compile(r"^_(\d{4})-(\d{2})$")


@dataclass(frozen=True)
class ScannedFile:
    root_label: str
    root_kind: str  # "digital" | "scan"
    master_path: Path
    source_folder: str  # forward-slash, relative to root, "" for root itself
    source_filename: str
    scan_batch: str | None    # top-level folder, scans only
    scan_sequence: int | None # 1-based within source_folder, scans only
    is_video: bool
    ext: str  # lowercased, no dot


@dataclass(frozen=True)
class ScanCounts:
    total_files_seen: int
    accepted: int
    skipped_other_ext: int


def walk_root(root: MasterRoot) -> Iterator[ScannedFile]:
    """Yield accepted files in each folder, natsort ordered for stable
    scan_sequence. Unsupported extensions are logged and skipped (caller
    tallies with scan_counts if it wants totals).
    """
    if not root.path.exists():
        log.warning("Root %s does not exist: %s", root.label, root.path)
        return
    # Group by directory so scan_sequence is assigned per-folder.
    by_folder: dict[Path, list[str]] = defaultdict(list)
    for dirpath, _, filenames in os.walk(root.path):
        d = Path(dirpath)
        for name in filenames:
            if name.startswith("._photoarchive_write_probe"):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in ACCEPTED_EXTS:
                log.info("skip (unsupported ext): %s", d / name)
                continue
            by_folder[d].append(name)

    from .scan_order import order_files
    for folder in sorted(by_folder.keys()):
        try:
            rel = folder.relative_to(root.path)
        except ValueError:
            continue
        source_folder = "" if str(rel) == "." else str(rel).replace("\\", "/")
        top = source_folder.split("/", 1)[0] if source_folder else ""
        is_scan_root = root.kind == "scan"
        scan_batch = top if (is_scan_root and top) else None
        # For scan-kind roots, order by mtime (envelope order) with natsort
        # filename as tie-break; for digital roots, natsort is fine.
        if is_scan_root:
            with_mtimes: list[tuple[str, float]] = []
            for name in by_folder[folder]:
                try:
                    with_mtimes.append((name, (folder / name).stat().st_mtime))
                except OSError:
                    with_mtimes.append((name, 0.0))
            ordered_names, _fallback = order_files(with_mtimes)
        else:
            ordered_names = natsorted(by_folder[folder])
        for i, name in enumerate(ordered_names, start=1):
            ext = os.path.splitext(name)[1].lower()
            yield ScannedFile(
                root_label=root.label,
                root_kind=root.kind,
                master_path=folder / name,
                source_folder=source_folder,
                source_filename=name,
                scan_batch=scan_batch,
                scan_sequence=(i if is_scan_root else None),
                is_video=(ext in VIDEO_EXTS),
                ext=ext.lstrip("."),
            )


def is_batch_folder(name: str) -> bool:
    return bool(_BATCH_RE.match(name))


def parse_date_folder(folder: str) -> tuple[int, int] | None:
    """Return (year, month) if any component matches `_YYYY-MM`, deepest wins."""
    if not folder:
        return None
    hit: tuple[int, int] | None = None
    for part in folder.split("/"):
        m = _DATE_FOLDER_RE.match(part)
        if m:
            hit = (int(m.group(1)), int(m.group(2)))
    return hit
