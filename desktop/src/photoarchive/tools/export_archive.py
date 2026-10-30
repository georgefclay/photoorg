"""Phase 12: take the entire archive out of this system, in open formats.

    python -m photoarchive.tools.export_archive --to <dir> [--masters] [--people]
                                                [--chunk N]

Writes images/ (the download tree, with images/Private/ for private photos),
backs/, db/photoorg.sql (pg_dump --format=plain), db/csv/<table>.csv,
db/web/<table>.csv (what only the website holds) and README.txt. `--masters`
adds masters/ with the original files (read-only copy, masters guard first).

Resumable: a file already at its destination with the same size is skipped,
and the copy runs as chunks of EXPORT_CHUNK files in fresh processes, so a
kill costs one chunk and running again continues. Exit 0 when everything was
placed, 1 when a chunk failed or a file could not be copied, 2 when a master
root is writable.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .. import db
from ..config import load as load_config
from ..logging_setup import configure_logging
from ..modes.export import archive


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--to", type=Path, required=True, help="export folder (created if missing)")
    parser.add_argument("--masters", action="store_true", help="also copy the original master files")
    parser.add_argument("--people", action="store_true",
                        help="also an images/People/<name>/ copy under each person tagged")
    parser.add_argument("--chunk", type=int, default=None, help="files per fresh process")
    parser.add_argument("--chunk-worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--out", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    configure_logging()
    settings = load_config()
    try:
        if args.chunk_worker:
            items = json.loads(args.chunk_worker.read_text(encoding="utf-8"))
            res = archive.run_chunk(settings, args.to, items, out=lambda m: print(m, flush=True))
            tmp = args.out.with_suffix(".partial")
            tmp.write_text(json.dumps(res), encoding="utf-8")
            tmp.replace(args.out)
            return 0
        db.init_pool(settings)
        try:
            summary = archive.run(settings, args.to, masters=args.masters, people_tree=args.people,
                                  chunk_size=args.chunk, out=lambda m: print(m, flush=True))
        finally:
            db.close_pool()
        print()
        print(summary.report())
        return 1 if summary.failed_chunks or summary.failed else 0
    except archive.MastersWritable as e:
        print(str(e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
