"""Working-file integrity scan (Phase 6 fix-up 7).

Every non-deleted `photos` row should have a `working_path` that names an
existing file. This got out of sync historically for some scans that went
through `_staging/` in Phase 2 (as proposed backs) and were later released
via rebuilds / rejections / fix-up folds — leaving `working_path` pointing
at a moved file, or the standard-name file existing without the DB
knowing.

For every affected photo we look, in order, at:
  1. The standard working name: `WORKING_DIR/{id:08d}_{sha[:8]}.{ext}`.
     If the file is there, update the pointer only (no file move).
  2. The staging alias: `WORKING_DIR/_staging/{sha}.{ext}`. If found,
     move it to the standard name and point the DB at it. `file_version`
     bumps because the file location genuinely changed.
  3. The preferred master. Copy master → standard name (masters are
     read-only; never move). `file_version` bumps.

Every repair writes an audit row (`photo.working_path_repaired`) with the
before/after paths. Photos whose file is nowhere to be found are listed
under `truly_missing` — they need manual attention.

Usage:
    python -m photoarchive.tools.check_working_files            # scan + repair
    python -m photoarchive.tools.check_working_files --dry-run
    python -m photoarchive.tools.check_working_files --limit 500
    python -m photoarchive.tools.check_working_files --json
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .. import db as dbmod
from ..config import load as load_config
from ..logging_setup import configure_logging
from ..modes.ingest import paths as ingest_paths

log = logging.getLogger(__name__)


@dataclass
class CheckCounts:
    photos_scanned: int = 0
    already_ok: int = 0
    pointer_repaired_standard: int = 0
    pointer_repaired_staging: int = 0
    recopied_from_master: int = 0
    truly_missing: int = 0
    truly_missing_ids: list[int] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "photos_scanned": self.photos_scanned,
            "already_ok": self.already_ok,
            "pointer_repaired_standard": self.pointer_repaired_standard,
            "pointer_repaired_staging": self.pointer_repaired_staging,
            "recopied_from_master": self.recopied_from_master,
            "truly_missing": self.truly_missing,
            "truly_missing_ids": self.truly_missing_ids,
        }


def _ext_from_master(master_path: str | None) -> str:
    if not master_path:
        return "jpg"
    ext = Path(master_path).suffix.lstrip(".").lower()
    return ext or "jpg"


def _write_audit(conn, *, photo_id: int, action: str, prev_path: str | None, new_path: str) -> None:
    conn.execute(
        """
        insert into audit_log
          (user_id, actor, action, entity_type, entity_id,
           previous_value, new_value)
        values (null, 'desktop', %s, 'photo', %s, %s::jsonb, %s::jsonb)
        """,
        (
            action, photo_id,
            json.dumps({"working_path": prev_path}),
            json.dumps({"working_path": new_path}),
        ),
    )


def _load_rows() -> list[tuple]:
    with dbmod.connection() as conn:
        conn.autocommit = True
        return conn.execute(
            """
            select p.id, p.working_path, p.sha256, p.mime,
                   pm.master_path
            from photos p
            left join photo_masters pm
              on pm.photo_id = p.id and pm.is_preferred
            where not p.is_deleted
            order by p.id
            """
        ).fetchall()


def check(*, dry_run: bool, limit: int | None) -> CheckCounts:
    settings = load_config()
    working_dir: Path = settings.WORKING_DIR
    counts = CheckCounts()

    rows = _load_rows()
    if limit:
        rows = rows[:limit]

    for row in rows:
        counts.photos_scanned += 1
        photo_id, working_path, sha256, mime, master_path = row

        if working_path and Path(working_path).exists():
            counts.already_ok += 1
            continue

        ext = _ext_from_master(master_path)
        standard = ingest_paths.working_path(settings, photo_id, sha256, ext)
        staging = ingest_paths.staging_working_path(settings, sha256, ext)

        # 1. Standard-name file already on disk → pointer repair only.
        if standard.exists():
            log.info("photo %d: pointer stale (%s) → %s", photo_id, working_path, standard)
            if not dry_run:
                with dbmod.connection() as conn:
                    conn.autocommit = True
                    conn.execute(
                        "update photos set working_path = %s where id = %s",
                        (str(standard), photo_id),
                    )
                    _write_audit(
                        conn,
                        photo_id=photo_id,
                        action="photo.working_path_repaired.pointer",
                        prev_path=working_path,
                        new_path=str(standard),
                    )
            counts.pointer_repaired_standard += 1
            continue

        # 2. Staging alias → move file into place, then point at it.
        if staging.exists():
            log.info("photo %d: staging alias at %s → move to %s", photo_id, staging, standard)
            if not dry_run:
                standard.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.move(str(staging), str(standard))
                except Exception as e:
                    log.warning("photo %d: staging move failed (%s); leaving pointer at staging", photo_id, e)
                    with dbmod.connection() as conn:
                        conn.autocommit = True
                        conn.execute(
                            """
                            update photos
                            set working_path = %s, file_version = file_version + 1
                            where id = %s
                            """,
                            (str(staging), photo_id),
                        )
                        _write_audit(
                            conn,
                            photo_id=photo_id,
                            action="photo.working_path_repaired.staging_pointer",
                            prev_path=working_path,
                            new_path=str(staging),
                        )
                    counts.pointer_repaired_staging += 1
                    continue
                with dbmod.connection() as conn:
                    conn.autocommit = True
                    conn.execute(
                        """
                        update photos
                        set working_path = %s, file_version = file_version + 1
                        where id = %s
                        """,
                        (str(standard), photo_id),
                    )
                    _write_audit(
                        conn,
                        photo_id=photo_id,
                        action="photo.working_path_repaired.staging_moved",
                        prev_path=working_path,
                        new_path=str(standard),
                    )
            counts.pointer_repaired_staging += 1
            continue

        # 3. Master → copy (masters are read-only; never move).
        if master_path and Path(master_path).exists():
            log.info("photo %d: nothing on disk; copying from master %s → %s",
                     photo_id, master_path, standard)
            if not dry_run:
                standard.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(master_path, str(standard))
                except Exception as e:
                    log.error("photo %d: copy from master failed: %s", photo_id, e)
                    counts.truly_missing += 1
                    counts.truly_missing_ids.append(photo_id)
                    continue
                with dbmod.connection() as conn:
                    conn.autocommit = True
                    conn.execute(
                        """
                        update photos
                        set working_path = %s, file_version = file_version + 1
                        where id = %s
                        """,
                        (str(standard), photo_id),
                    )
                    _write_audit(
                        conn,
                        photo_id=photo_id,
                        action="photo.working_path_repaired.recopied_from_master",
                        prev_path=working_path,
                        new_path=str(standard),
                    )
            counts.recopied_from_master += 1
            continue

        # 4. Nothing found anywhere.
        counts.truly_missing += 1
        counts.truly_missing_ids.append(photo_id)
        log.warning(
            "photo %d: truly missing — working_path=%s standard=%s staging=%s master=%s",
            photo_id, working_path, standard, staging, master_path,
        )

    return counts


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(prog="photoarchive.tools.check_working_files")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    settings = load_config()
    dbmod.init_pool(settings)
    try:
        counts = check(dry_run=args.dry_run, limit=args.limit)
    finally:
        dbmod.close_pool()

    if args.json:
        print(json.dumps(counts.as_dict(), indent=2))
    else:
        d = counts.as_dict()
        print(f"scanned:                    {d['photos_scanned']}")
        print(f"already_ok:                 {d['already_ok']}")
        print(f"pointer_repaired_standard:  {d['pointer_repaired_standard']}")
        print(f"pointer_repaired_staging:   {d['pointer_repaired_staging']}")
        print(f"recopied_from_master:       {d['recopied_from_master']}")
        print(f"truly_missing:              {d['truly_missing']}")
        if d["truly_missing_ids"]:
            print("truly missing photo ids:")
            for pid in d["truly_missing_ids"][:100]:
                print(f"  {pid}")
            if len(d["truly_missing_ids"]) > 100:
                print(f"  ... and {len(d['truly_missing_ids']) - 100} more")
    return 0


if __name__ == "__main__":
    sys.exit(main())
