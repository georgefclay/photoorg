"""Working-file integrity scan (Phase 6 fix-up 7, extended in fix-up 11).

Every non-deleted `photos` row should have an **absolute** `working_path`
that names an existing file; likewise every `photo_backs` row. This got out
of sync twice:

  * fix-up 7 — scans that went through `_staging/` in Phase 2 and were
    later released via rebuilds / rejections, leaving `working_path`
    pointing at a moved file;
  * fix-up 11 — the Phase 9 local verification push ran the laptop web
    server against the desktop's own database, and the web's sync routes
    rewrote every `photos.working_path` and `photo_backs.working_path` to
    a bare basename (`00000001_13b533cd.jpg`). The file was never touched.

A bare (non-absolute) `working_path` is always treated as stale, even if a
file of that name happens to sit in the current directory — `.exists()` on
a relative path is CWD-dependent and must not decide anything.

For every affected photo we look, in order, at:
  1. The standard working name: `WORKING_DIR/{id:08d}_{sha[:8]}.{ext}`
     (ext from the stored name when there is one, else the master's).
     If the file is there, update the pointer only (no file move).
  2. The staging alias: `WORKING_DIR/_staging/{sha}.{ext}`. If found,
     move it to the standard name and point the DB at it. `file_version`
     bumps because the file location genuinely changed.
  3. The preferred master. Copy master → standard name (masters are
     read-only; never move). `file_version` bumps.

For every affected back:
  1. `WORKING_DIR/back_{id:08d}_{sha[:8]}.{ext}` exists → pointer only.
  2. Else copy from `photo_backs.master_path` (read-only; copy never move).

Deleted photos that still carry a bare `working_path` get the pointer
repair only when the standard file exists; nothing else is touched.

Every repair writes an audit row (`photo.working_path_repaired.*` /
`photo_back.working_path_repaired.*`) with the before/after paths. Photos
whose file is nowhere to be found are listed under `truly_missing`.

The summary ends with the post-condition the PM asked for in fix-up 11:
the number of live rows (photos and backs) whose `working_path` is still
not absolute. Both must be 0 after a real run.

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
    bare_pointers_seen: int = 0
    pointer_repaired_standard: int = 0
    pointer_repaired_staging: int = 0
    recopied_from_master: int = 0
    truly_missing: int = 0
    truly_missing_ids: list[int] = field(default_factory=list)
    # photo_backs (fix-up 11)
    backs_scanned: int = 0
    backs_already_ok: int = 0
    backs_pointer_repaired: int = 0
    backs_recopied_from_master: int = 0
    backs_missing: int = 0
    backs_missing_ids: list[int] = field(default_factory=list)
    # deleted photos with a bare pointer (fix-up 11): pointer-only repair
    deleted_pointer_repaired: int = 0
    deleted_unresolved: int = 0
    # post-condition: live rows still non-absolute after the run
    photos_still_bare: int = 0
    backs_still_bare: int = 0

    def as_dict(self) -> dict:
        return {
            "photos_scanned": self.photos_scanned,
            "already_ok": self.already_ok,
            "bare_pointers_seen": self.bare_pointers_seen,
            "pointer_repaired_standard": self.pointer_repaired_standard,
            "pointer_repaired_staging": self.pointer_repaired_staging,
            "recopied_from_master": self.recopied_from_master,
            "truly_missing": self.truly_missing,
            "truly_missing_ids": self.truly_missing_ids,
            "backs_scanned": self.backs_scanned,
            "backs_already_ok": self.backs_already_ok,
            "backs_pointer_repaired": self.backs_pointer_repaired,
            "backs_recopied_from_master": self.backs_recopied_from_master,
            "backs_missing": self.backs_missing,
            "backs_missing_ids": self.backs_missing_ids,
            "deleted_pointer_repaired": self.deleted_pointer_repaired,
            "deleted_unresolved": self.deleted_unresolved,
            "photos_still_bare": self.photos_still_bare,
            "backs_still_bare": self.backs_still_bare,
        }


def _ext_from(stored_working_path: str | None, master_path: str | None) -> str:
    """Extension for the standard name: the stored working name wins (it is
    what the file on disk is actually called), then the master's."""
    for candidate in (stored_working_path, master_path):
        if candidate:
            ext = Path(candidate).suffix.lstrip(".").lower()
            if ext:
                return ext
    return "jpg"


def _is_absolute(stored: str | None) -> bool:
    return ingest_paths.is_absolute_working_path(stored)


def _write_audit(conn, *, entity_type: str, entity_id: int, action: str,
                 prev_path: str | None, new_path: str) -> None:
    conn.execute(
        """
        insert into audit_log
          (user_id, actor, action, entity_type, entity_id,
           previous_value, new_value)
        values (null, 'desktop', %s, %s, %s, %s::jsonb, %s::jsonb)
        """,
        (
            action, entity_type, entity_id,
            json.dumps({"working_path": prev_path}),
            json.dumps({"working_path": new_path}),
        ),
    )


def _set_photo_pointer(conn, photo_id: int, new_path: Path, *, bump_version: bool,
                       action: str, prev_path: str | None) -> None:
    if bump_version:
        conn.execute(
            "update photos set working_path = %s, file_version = file_version + 1 where id = %s",
            (str(new_path), photo_id),
        )
    else:
        conn.execute(
            "update photos set working_path = %s where id = %s",
            (str(new_path), photo_id),
        )
    _write_audit(conn, entity_type="photo", entity_id=photo_id, action=action,
                 prev_path=prev_path, new_path=str(new_path))


def _load_photo_rows() -> list[tuple]:
    with dbmod.connection() as conn:
        conn.autocommit = True
        return conn.execute(
            """
            select p.id, p.working_path, p.sha256, p.mime,
                   pm.master_path, p.is_deleted
            from photos p
            left join photo_masters pm
              on pm.photo_id = p.id and pm.is_preferred
            where not p.is_deleted
               or (p.working_path is not null)
            order by p.id
            """
        ).fetchall()


def _load_back_rows() -> list[tuple]:
    with dbmod.connection() as conn:
        conn.autocommit = True
        return conn.execute(
            """
            select id, working_path, sha256, master_path
            from photo_backs
            order by id
            """
        ).fetchall()


def _post_condition_counts() -> tuple[int, int]:
    """Live photos / all backs whose working_path is still not absolute.
    Postgres regex: a Windows drive letter + ':\\' or a POSIX leading '/'."""
    with dbmod.connection() as conn:
        conn.autocommit = True
        photos = conn.execute(
            r"""
            select count(*) from photos
            where not is_deleted
              and working_path is not null
              and working_path !~ '^([A-Za-z]:\\|/)'
            """
        ).fetchone()[0]
        backs = conn.execute(
            r"""
            select count(*) from photo_backs
            where working_path is not null
              and working_path !~ '^([A-Za-z]:\\|/)'
            """
        ).fetchone()[0]
    return int(photos), int(backs)


# --- photos ---------------------------------------------------------------


def _check_photo(counts: CheckCounts, settings, row: tuple, *, dry_run: bool) -> None:
    photo_id, working_path, sha256, _mime, master_path, is_deleted = row
    bare = bool(working_path) and not _is_absolute(working_path)
    if bare:
        counts.bare_pointers_seen += 1

    if is_deleted:
        # Deleted rows: only ever fix a bare pointer, and only when the
        # standard file is there. Never move or copy for a deleted photo.
        if not bare:
            return
        ext = _ext_from(working_path, master_path)
        standard = ingest_paths.working_path(settings, photo_id, sha256, ext)
        if standard.exists():
            log.info("deleted photo %d: bare pointer (%s) → %s", photo_id, working_path, standard)
            if not dry_run:
                with dbmod.connection() as conn:
                    conn.autocommit = True
                    _set_photo_pointer(
                        conn, photo_id, standard, bump_version=False,
                        action="photo.working_path_repaired.pointer",
                        prev_path=working_path,
                    )
            counts.deleted_pointer_repaired += 1
        else:
            counts.deleted_unresolved += 1
        return

    counts.photos_scanned += 1

    if working_path and not bare and Path(working_path).exists():
        counts.already_ok += 1
        return

    ext = _ext_from(working_path, master_path)
    standard = ingest_paths.working_path(settings, photo_id, sha256, ext)
    staging = ingest_paths.staging_working_path(settings, sha256, ext)

    # 1. Standard-name file already on disk → pointer repair only.
    if standard.exists():
        log.info("photo %d: pointer stale (%s) → %s", photo_id, working_path, standard)
        if not dry_run:
            with dbmod.connection() as conn:
                conn.autocommit = True
                _set_photo_pointer(
                    conn, photo_id, standard, bump_version=False,
                    action="photo.working_path_repaired.pointer",
                    prev_path=working_path,
                )
        counts.pointer_repaired_standard += 1
        return

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
                    _set_photo_pointer(
                        conn, photo_id, staging, bump_version=True,
                        action="photo.working_path_repaired.staging_pointer",
                        prev_path=working_path,
                    )
                counts.pointer_repaired_staging += 1
                return
            with dbmod.connection() as conn:
                conn.autocommit = True
                _set_photo_pointer(
                    conn, photo_id, standard, bump_version=True,
                    action="photo.working_path_repaired.staging_moved",
                    prev_path=working_path,
                )
        counts.pointer_repaired_staging += 1
        return

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
                return
            with dbmod.connection() as conn:
                conn.autocommit = True
                _set_photo_pointer(
                    conn, photo_id, standard, bump_version=True,
                    action="photo.working_path_repaired.recopied_from_master",
                    prev_path=working_path,
                )
        counts.recopied_from_master += 1
        return

    # 4. Nothing found anywhere.
    counts.truly_missing += 1
    counts.truly_missing_ids.append(photo_id)
    log.warning(
        "photo %d: truly missing — working_path=%s standard=%s staging=%s master=%s",
        photo_id, working_path, standard, staging, master_path,
    )


# --- photo_backs ----------------------------------------------------------


def _check_back(counts: CheckCounts, settings, row: tuple, *, dry_run: bool) -> None:
    back_id, working_path, sha256, master_path = row
    counts.backs_scanned += 1
    bare = bool(working_path) and not _is_absolute(working_path)
    if bare:
        counts.bare_pointers_seen += 1

    if working_path and not bare and Path(working_path).exists():
        counts.backs_already_ok += 1
        return

    ext = _ext_from(working_path, master_path)
    standard = ingest_paths.back_working_path(settings, back_id, sha256, ext)

    if standard.exists():
        log.info("back %d: pointer stale (%s) → %s", back_id, working_path, standard)
        if not dry_run:
            with dbmod.connection() as conn:
                conn.autocommit = True
                conn.execute(
                    "update photo_backs set working_path = %s where id = %s",
                    (str(standard), back_id),
                )
                _write_audit(conn, entity_type="photo_back", entity_id=back_id,
                             action="photo_back.working_path_repaired.pointer",
                             prev_path=working_path, new_path=str(standard))
        counts.backs_pointer_repaired += 1
        return

    if master_path and Path(master_path).exists():
        log.info("back %d: nothing on disk; copying from master %s → %s",
                 back_id, master_path, standard)
        if not dry_run:
            standard.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(master_path, str(standard))
            except Exception as e:
                log.error("back %d: copy from master failed: %s", back_id, e)
                counts.backs_missing += 1
                counts.backs_missing_ids.append(back_id)
                return
            with dbmod.connection() as conn:
                conn.autocommit = True
                conn.execute(
                    "update photo_backs set working_path = %s where id = %s",
                    (str(standard), back_id),
                )
                _write_audit(conn, entity_type="photo_back", entity_id=back_id,
                             action="photo_back.working_path_repaired.recopied_from_master",
                             prev_path=working_path, new_path=str(standard))
        counts.backs_recopied_from_master += 1
        return

    counts.backs_missing += 1
    counts.backs_missing_ids.append(back_id)
    log.warning("back %d: truly missing — working_path=%s standard=%s master=%s",
                back_id, working_path, standard, master_path)


# --- driver ---------------------------------------------------------------


def check(*, dry_run: bool, limit: int | None) -> CheckCounts:
    settings = load_config()
    counts = CheckCounts()

    rows = _load_photo_rows()
    if limit:
        rows = rows[:limit]
    for row in rows:
        _check_photo(counts, settings, row, dry_run=dry_run)

    backs = _load_back_rows()
    if limit:
        backs = backs[:limit]
    for row in backs:
        _check_back(counts, settings, row, dry_run=dry_run)

    counts.photos_still_bare, counts.backs_still_bare = _post_condition_counts()
    return counts


def format_summary(counts: CheckCounts, *, dry_run: bool) -> str:
    d = counts.as_dict()
    lines = [
        f"mode:                        {'DRY RUN' if dry_run else 'repair'}",
        f"photos scanned (live):       {d['photos_scanned']}",
        f"  already_ok:                {d['already_ok']}",
        f"  pointer_repaired_standard: {d['pointer_repaired_standard']}",
        f"  pointer_repaired_staging:  {d['pointer_repaired_staging']}",
        f"  recopied_from_master:      {d['recopied_from_master']}",
        f"  truly_missing:             {d['truly_missing']}",
        f"backs scanned:               {d['backs_scanned']}",
        f"  already_ok:                {d['backs_already_ok']}",
        f"  pointer_repaired:          {d['backs_pointer_repaired']}",
        f"  recopied_from_master:      {d['backs_recopied_from_master']}",
        f"  missing:                   {d['backs_missing']}",
        f"deleted photos, bare ptr:    repaired {d['deleted_pointer_repaired']}, "
        f"unresolved {d['deleted_unresolved']}",
        f"bare pointers seen (all):    {d['bare_pointers_seen']}",
        "post-condition (must be 0 after a real run):",
        f"  live photos still bare:    {d['photos_still_bare']}",
        f"  backs still bare:          {d['backs_still_bare']}",
    ]
    for label, ids in (("truly missing photo ids", d["truly_missing_ids"]),
                       ("missing back ids", d["backs_missing_ids"])):
        if ids:
            lines.append(f"{label}:")
            lines.extend(f"  {i}" for i in ids[:100])
            if len(ids) > 100:
                lines.append(f"  ... and {len(ids) - 100} more")
    return "\n".join(lines)


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
        print(format_summary(counts, dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
