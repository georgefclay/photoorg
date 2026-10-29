"""Phase 13: write the settled truth into the working copies.

    python -m photoarchive.tools.write_metadata [--dry-run] [--photo ID ...]
                                               [--batch "Batch 00012" ...]
                                               [--limit N] [--chunk N]

Incremental: only photos whose payload hash changed, or whose working file
was replaced since the last write, are touched; a second run after no fact
changes writes nothing. Each chunk of ~250 runs in its own fresh process, so
a kill costs one chunk and re-running continues.

`--dry-run` prints, per file, every tag that would change, and writes the
report (counts by tag, sample payloads, pre-existing values, date
mismatches) without writing a byte to any image.

The masters guard runs first and again in every chunk; a writable master root
refuses the run (exit 2). exiftool must be installed (EXIFTOOL_PATH).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .. import db
from ..config import load as load_config
from ..logging_setup import configure_logging
from ..modes.metadata import exiftool as et_mod
from ..modes.metadata import run as run_mod
from ..modes.metadata import writer

log = logging.getLogger(__name__)


def _ids(values: list[str]) -> list[int]:
    return [int(v) for chunk in values for v in str(chunk).replace(" ", "").split(",") if v]


def _chunk_worker(settings, ids_file: Path, out_file: Path, *, dry_run: bool) -> int:
    ids = json.loads(ids_file.read_text(encoding="utf-8"))

    def progress(r: writer.PhotoResult, i: int, n: int) -> None:
        d = r.to_dict()
        if r.status in ("written", "would_write"):
            if dry_run:
                print("\n".join(run_mod.format_changes(d)), flush=True)
            else:
                print(f"  [{i}/{n}] photo {r.photo_id}: written v{r.new_version} "
                      f"({len(r.changes)} tags)", flush=True)
        else:
            print(f"  [{i}/{n}] photo {r.photo_id}: {r.status} — "
                  f"{r.error or r.reason}", flush=True)

    results = writer.run_chunk(settings, ids, dry_run=dry_run, progress=progress)
    tmp = out_file.with_suffix(".partial")
    tmp.write_text(json.dumps([r.to_dict() for r in results], default=str,
                              ensure_ascii=False), encoding="utf-8")
    tmp.replace(out_file)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change; write nothing")
    parser.add_argument("--photo", dest="photos", action="append", default=[],
                        help="restrict to these photo ids (repeatable or comma-separated)")
    parser.add_argument("--batch", dest="batches", action="append",
                        help="restrict to this scan_batch (repeatable)")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--chunk", type=int, default=None,
                        help="photos per fresh process (default METADATA_CHUNK)")
    parser.add_argument("--chunk-worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--out", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    configure_logging()
    settings = load_config()
    db.init_pool(settings)
    try:
        if args.chunk_worker:
            return _chunk_worker(settings, args.chunk_worker, args.out, dry_run=args.dry_run)
        summary = run_mod.run(
            settings, dry_run=args.dry_run, photo_ids=_ids(args.photos) or None,
            batches=args.batches, limit=args.limit, chunk_size=args.chunk,
        )
        print()
        print(summary.report())
        return 1 if summary.failed_chunks or summary.failed else 0
    except writer.MastersWritable as e:
        print(str(e), file=sys.stderr)
        return 2
    except et_mod.ExifToolMissing as e:
        print(str(e), file=sys.stderr)
        return 2
    finally:
        db.close_pool()


if __name__ == "__main__":
    sys.exit(main())
