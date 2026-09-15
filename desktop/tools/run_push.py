"""Headless push driver for Phase 9 verification.

Runs `push()` against the configured WEB_API_URL, prints per-stage
progress, and reports totals. Reads settings via desktop `.env`.

Usage:
  python -m photoarchive.tools.run_push          # push everything
  python -m photoarchive.tools.run_push --dry    # count target rows only
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from photoarchive import db
from photoarchive.config import load as load_settings
from photoarchive.modes.sync.client import WebSyncClient
from photoarchive.modes.sync.push import push, PushProgress


def _humanb(n: float) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.2f} {u}"
        n /= 1024
    return f"{n:.2f} PB"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="count targets, don't push")
    args = ap.parse_args()

    settings = load_settings()
    print(f"WEB_API_URL: {settings.WEB_API_URL}")
    print(f"WORKING_DIR: {settings.WORKING_DIR}")

    db.init_pool(settings, min_size=1, max_size=2)
    try:
        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("select count(*) from photos where is_private = false and triage_status <> 'junk'")
                pushable = cur.fetchone()[0]
                cur.execute("select count(*) from photos where is_private")
                priv = cur.fetchone()[0]
                cur.execute("select count(*) from photos where triage_status = 'junk'")
                junk = cur.fetchone()[0]
        print(f"pushable={pushable}  skipped: private={priv} junk={junk}")
        if args.dry:
            return 0

        client = WebSyncClient(settings.WEB_API_URL, settings.WEB_API_TOKEN)
        started = time.time()
        last_print = 0.0
        seen_stages: set[str] = set()

        def prog(p: PushProgress) -> None:
            nonlocal last_print
            now = time.time()
            key = p.stage
            first_of_stage = key not in seen_stages
            if first_of_stage:
                seen_stages.add(key)
            # Rate-limit line prints to keep the terminal readable but
            # always show the first & last update of each stage.
            if first_of_stage or (now - last_print) > 2 or p.done == p.total:
                sys.stdout.write(f"  {p.stage}: {p.done}/{p.total} {p.detail}\n")
                sys.stdout.flush()
                last_print = now

        env_face = os.environ.get("SYNC_FACE_EMBEDDINGS", "false").strip().lower() in ("1", "true", "yes")
        print(f"SYNC_FACE_EMBEDDINGS={env_face}")
        print("--- push begin ---")
        stats = push(
            client,
            working_dir=Path(settings.WORKING_DIR),
            thumbs_dir=Path(settings.THUMBS_DIR),
            send_face_embeddings=env_face,
            progress=prog,
        )
        elapsed = time.time() - started
        print("--- push done ---")
        print(f"photos upserted: {stats.photos_upserted}")
        print(f"files uploaded:  {stats.files_uploaded}")
        print(f"bytes uploaded:  {_humanb(stats.bytes_uploaded)} ({stats.bytes_uploaded} raw)")
        print(f"elapsed:         {elapsed:.1f} s")
        print("per-table:")
        for k, v in sorted(stats.tables.items()):
            print(f"  {k}: {v}")
        return 0
    finally:
        db.close_pool()


if __name__ == "__main__":
    sys.exit(main())
