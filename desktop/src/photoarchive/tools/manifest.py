"""Walk a master root and write path,size,mtime,sha256 CSV.

Usage:
    python -m photoarchive.tools.manifest <label>            # write manifest
    python -m photoarchive.tools.manifest --diff <a> <b>     # diff two manifests
    python -m photoarchive.tools.manifest --list             # list roots

sha256 is cached by (path, size, mtime) in a small sqlite next to the manifests,
so the second run over 17k files takes seconds.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from ..config import load as load_config
from ..logging_setup import configure_logging

log = logging.getLogger(__name__)


def manifest_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    d = Path(base) / "PhotoArchive" / "manifests"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(manifest_dir() / "sha256_cache.sqlite")
    conn.execute(
        """
        create table if not exists sha256_cache (
          path text primary key,
          size integer not null,
          mtime real not null,
          sha256 text not null
        )
        """
    )
    return conn


def sha256_of(path: Path, cache: sqlite3.Connection) -> str:
    st = path.stat()
    row = cache.execute(
        "select sha256 from sha256_cache where path = ? and size = ? and mtime = ?",
        (str(path), st.st_size, st.st_mtime),
    ).fetchone()
    if row:
        return row[0]
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    digest = h.hexdigest()
    cache.execute(
        "insert or replace into sha256_cache (path, size, mtime, sha256) values (?, ?, ?, ?)",
        (str(path), st.st_size, st.st_mtime, digest),
    )
    cache.commit()
    return digest


def write_manifest(root_label: str, root_path: Path) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = manifest_dir() / f"{root_label}_{ts}.csv"
    cache = _cache_conn()
    count = 0
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["path", "size", "mtime", "sha256"])
        for dirpath, _, filenames in os.walk(root_path):
            for name in sorted(filenames):
                if name.startswith("._photoarchive_write_probe"):
                    continue
                p = Path(dirpath) / name
                try:
                    st = p.stat()
                    digest = sha256_of(p, cache)
                except OSError as e:
                    log.warning("skip %s: %s", p, e)
                    continue
                rel = str(p.relative_to(root_path)).replace("\\", "/")
                w.writerow([rel, st.st_size, f"{st.st_mtime:.6f}", digest])
                count += 1
                if count % 500 == 0:
                    log.info("manifest %s: %d files", root_label, count)
    log.info("manifest %s written to %s (%d files)", root_label, out, count)
    return out


def diff_manifests(a: Path, b: Path) -> int:
    def load(p: Path) -> dict[str, str]:
        m: dict[str, str] = {}
        with open(p, encoding="utf-8") as fh:
            r = csv.DictReader(fh)
            for row in r:
                m[row["path"]] = row["sha256"]
        return m

    ma = load(a)
    mb = load(b)
    added = sorted(set(mb) - set(ma))
    removed = sorted(set(ma) - set(mb))
    changed = sorted(p for p in set(ma) & set(mb) if ma[p] != mb[p])
    if not added and not removed and not changed:
        print("diff empty")
        return 0
    print(f"added={len(added)} removed={len(removed)} changed={len(changed)}")
    for p in added[:20]:
        print(f"  + {p}")
    for p in removed[:20]:
        print(f"  - {p}")
    for p in changed[:20]:
        print(f"  ~ {p}")
    return 1


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(prog="photoarchive.tools.manifest")
    parser.add_argument("label", nargs="?", help="root label to manifest")
    parser.add_argument("--list", action="store_true", help="list configured roots and exit")
    parser.add_argument("--diff", nargs=2, metavar=("A", "B"),
                        help="diff two manifest CSVs")
    args = parser.parse_args(argv)

    if args.diff:
        return diff_manifests(Path(args.diff[0]), Path(args.diff[1]))

    settings = load_config()
    if args.list:
        for r in settings.master_roots:
            print(f"{r.label:16s} {r.kind:8s} {r.path}")
        return 0

    if not args.label:
        parser.error("label required (or use --list / --diff)")
    root = settings.master_root_by_label(args.label)
    if root is None:
        print(f"unknown root label {args.label!r}", file=sys.stderr)
        return 2
    write_manifest(root.label, root.path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
