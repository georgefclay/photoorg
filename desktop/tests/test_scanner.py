from __future__ import annotations

import os
from pathlib import Path

from photoarchive.config import MasterRoot
from photoarchive.modes.ingest.scanner import (
    is_batch_folder,
    parse_date_folder,
    walk_root,
)


def _touch(p: Path, name: str, mtime: float | None = None) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    f = p / name
    f.write_bytes(b"x")
    if mtime is not None:
        os.utime(f, (mtime, mtime))
    return f


def test_scan_root_orders_by_mtime(tmp_path):
    """Fix-up 3: scan_sequence follows mtime (the scanner writes envelope
    order). Filenames alone would sort 0001 < 0004 < 0010 < 0026, but we
    stamp explicit mtimes to force scanner-order 0026, 0004, 0010, 0001."""
    folder = tmp_path / "Batch 00012"
    for i, name in enumerate(["IMG_0026.JPG", "IMG_0004.JPG",
                              "IMG_0010.JPG", "IMG_0001.JPG"]):
        _touch(folder, name, mtime=1_700_000_000 + i * 60)
    root = MasterRoot(label="scans", path=tmp_path, kind="scan")
    seen = list(walk_root(root))
    assert [f.source_filename for f in seen] == [
        "IMG_0026.JPG", "IMG_0004.JPG", "IMG_0010.JPG", "IMG_0001.JPG",
    ]
    assert [f.scan_sequence for f in seen] == [1, 2, 3, 4]


def test_scan_root_falls_back_to_natsort_when_mtimes_identical(tmp_path):
    """If every file has the same mtime (a copy that flattened mtimes),
    fall back to natsort by filename."""
    folder = tmp_path / "Batch 00099"
    same = 1_700_000_000
    for name in ["IMG_0026.JPG", "IMG_0004.JPG", "IMG_0010.JPG", "IMG_0001.JPG"]:
        _touch(folder, name, mtime=same)
    root = MasterRoot(label="scans", path=tmp_path, kind="scan")
    seen = list(walk_root(root))
    assert [f.source_filename for f in seen] == [
        "IMG_0001.JPG", "IMG_0004.JPG", "IMG_0010.JPG", "IMG_0026.JPG",
    ]


def test_scan_batch_top_level_only(tmp_path):
    # scan_batch is the TOP-LEVEL folder, subfolders stay in source_folder.
    _touch(tmp_path / "Chuck and Lola Wedding" / "High Quality", "IMG_0001.JPG")
    _touch(tmp_path / "Batch 00012", "IMG004.JPG")
    _touch(tmp_path / "Batch 00012", "IMG005.JPG")
    root = MasterRoot(label="scans", path=tmp_path, kind="scan")
    by_name = {f.source_filename: f for f in walk_root(root)}
    hq = by_name["IMG_0001.JPG"]
    assert hq.scan_batch == "Chuck and Lola Wedding"
    assert hq.source_folder == "Chuck and Lola Wedding/High Quality"
    assert hq.scan_sequence == 1
    b = by_name["IMG004.JPG"]
    assert b.scan_batch == "Batch 00012"
    assert b.source_folder == "Batch 00012"


def test_digital_root_has_no_batch_or_sequence(tmp_path):
    _touch(tmp_path / "_2005-04", "f123.jpg")
    root = MasterRoot(label="photos", path=tmp_path, kind="digital")
    files = list(walk_root(root))
    assert len(files) == 1
    assert files[0].scan_batch is None
    assert files[0].scan_sequence is None
    assert files[0].source_folder == "_2005-04"


def test_unsupported_extensions_are_skipped(tmp_path):
    _touch(tmp_path, "a.jpg")
    _touch(tmp_path, "b.txt")   # unsupported
    _touch(tmp_path, "c.CR2")   # unsupported RAW
    _touch(tmp_path, "d.MP4")   # video (accepted)
    root = MasterRoot(label="photos", path=tmp_path, kind="digital")
    names = {f.source_filename for f in walk_root(root)}
    assert names == {"a.jpg", "d.MP4"}


def test_folder_hint_parsing():
    assert parse_date_folder("_2005-04") == (2005, 4)
    assert parse_date_folder("Family/_2005-04") == (2005, 4)
    assert parse_date_folder("Batch 00012") is None
    assert parse_date_folder("2005-04") is None  # missing underscore
    assert parse_date_folder("") is None


def test_batch_folder_regex():
    assert is_batch_folder("Batch 00012")
    assert is_batch_folder("batch 00001")  # case-insensitive
    assert not is_batch_folder("Batch 12")  # not 5 digits
    assert not is_batch_folder("Chuck and Lola Wedding")
    assert not is_batch_folder("Batch 00012b")
