"""Recompute scan_sequence for every scan-kind folder using mtime order.

Applied to:
  - photos.scan_sequence (scan-kind roots only)
  - photo_backs.scan_sequence (matched by source_folder + source_filename;
    linked to a scan-kind photo)
  - ingest_pairings.back_scan_sequence (pending only; matched by
    back_source_folder + back_source_filename)

Never touches accepted/rejected proposals or rescans.

Returns a summary counting folders touched, folders that used the
filename-natsort fallback, and rows updated per table.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from ... import db
from ...config import MasterRoot, Settings
from ...workers import CancelToken, Cancelled
from .scan_order import order_files, walk_folder_for_ordering

log = logging.getLogger(__name__)


@dataclass
class RecomputeSummary:
    folders_processed: int = 0
    folders_fallback: int = 0
    folders_unchanged: int = 0
    photos_updated: int = 0
    photo_backs_updated: int = 0
    ingest_pairings_updated: int = 0
    fallback_folders: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def recompute_scan_order(
    *,
    settings: Settings,
    roots: Sequence[MasterRoot],
    progress_cb: Callable[[dict], None] = lambda _: None,
    cancel_token: CancelToken | None = None,
) -> RecomputeSummary:
    ctok = cancel_token or CancelToken()
    scan_roots = {r.label: r for r in roots if r.kind == "scan"}
    summary = RecomputeSummary()
    if not scan_roots:
        return summary

    # Group photos by (source_root, source_folder).
    with db.connection() as conn:
        conn.autocommit = True
        photo_rows = conn.execute(
            """
            select id, source_root, source_folder, source_filename, scan_sequence
            from photos
            where source_root = ANY(%s)
              and not is_deleted
            """,
            (list(scan_roots.keys()),),
        ).fetchall()

    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for pid, sroot, sfolder, sname, sseq in photo_rows:
        grouped[(sroot, sfolder)].append({
            "photo_id": pid, "source_filename": sname, "current_seq": sseq,
        })

    for (sroot, sfolder), files_in_db in sorted(grouped.items()):
        if ctok.is_set():
            raise Cancelled()
        root = scan_roots.get(sroot)
        if root is None:
            log.warning("recompute: unknown scan root %s", sroot)
            continue
        folder_path = root.path if sfolder == "" else root.path / sfolder
        disk_files = walk_folder_for_ordering(folder_path)
        if not disk_files:
            log.warning("recompute: folder empty on disk: %s", folder_path)
            continue
        ordered_names, fallback = order_files(disk_files)
        summary.folders_processed += 1
        if fallback:
            summary.folders_fallback += 1
            summary.fallback_folders.append(f"{sroot}/{sfolder}")
        new_seq_by_name = {name: i + 1 for i, name in enumerate(ordered_names)}

        # Build updates as batches. Postgres is happiest with a single
        # multi-row UPDATE ... FROM VALUES per table.
        photo_updates: list[tuple[int, int]] = []
        for row in files_in_db:
            new_seq = new_seq_by_name.get(row["source_filename"])
            if new_seq is None:
                # File exists in DB but not on disk. Leave alone.
                continue
            if new_seq == row["current_seq"]:
                continue
            photo_updates.append((row["photo_id"], new_seq))

        if photo_updates:
            with db.connection() as conn:
                conn.autocommit = False
                try:
                    for pid, new_seq in photo_updates:
                        conn.execute(
                            "update photos set scan_sequence = %s where id = %s",
                            (new_seq, pid),
                        )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            summary.photos_updated += len(photo_updates)
        else:
            summary.folders_unchanged += 1

        # photo_backs — match by source_folder + source_filename joined with
        # this scan root through the linked photo.
        with db.connection() as conn:
            conn.autocommit = False
            try:
                pb_rows = conn.execute(
                    """
                    select pb.id, pb.source_filename, pb.scan_sequence
                    from photo_backs pb
                    left join photos p on p.id = pb.photo_id
                    where pb.source_folder = %s
                      and (p.source_root is null or p.source_root = %s)
                    """,
                    (sfolder, sroot),
                ).fetchall()
                pb_updates: list[tuple[int, int]] = []
                for pb_id, pb_name, pb_seq in pb_rows:
                    new_seq = new_seq_by_name.get(pb_name)
                    if new_seq is None or new_seq == pb_seq:
                        continue
                    pb_updates.append((pb_id, new_seq))
                for pb_id, new_seq in pb_updates:
                    conn.execute(
                        "update photo_backs set scan_sequence = %s where id = %s",
                        (new_seq, pb_id),
                    )
                summary.photo_backs_updated += len(pb_updates)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        # ingest_pairings — pending rows in this folder.
        with db.connection() as conn:
            conn.autocommit = False
            try:
                ip_rows = conn.execute(
                    """
                    select ip.id, ip.back_source_filename, ip.back_scan_sequence
                    from ingest_pairings ip
                    join photos p on p.id = ip.front_photo_id
                    where ip.status = 'pending'
                      and p.source_root = %s
                      and ip.back_source_folder = %s
                    """,
                    (sroot, sfolder),
                ).fetchall()
                ip_updates: list[tuple[int, int]] = []
                for ip_id, ip_name, ip_seq in ip_rows:
                    new_seq = new_seq_by_name.get(ip_name)
                    if new_seq is None or new_seq == ip_seq:
                        continue
                    ip_updates.append((ip_id, new_seq))
                for ip_id, new_seq in ip_updates:
                    conn.execute(
                        "update ingest_pairings set back_scan_sequence = %s where id = %s",
                        (new_seq, ip_id),
                    )
                summary.ingest_pairings_updated += len(ip_updates)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        progress_cb({
            "kind": "folder_done",
            "root": sroot,
            "folder": sfolder,
            "n_photos": len(files_in_db),
            "photos_updated": len(photo_updates),
            "fallback": fallback,
        })

    return summary
