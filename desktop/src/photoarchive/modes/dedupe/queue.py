"""Queries used by the Dedupe UI to walk the pending queue and load
per-group facts.
"""
from __future__ import annotations

from dataclasses import dataclass

from ... import db


@dataclass
class GroupMember:
    photo_id: int
    is_keeper: bool
    is_scan: bool
    triage_status: str
    is_private: bool
    mime: str
    width: int | None
    height: int | None
    file_size: int | None
    exif_taken_at: object | None
    exif_camera: str | None
    scan_batch: str | None
    scan_sequence: int | None
    source_folder: str
    source_filename: str
    working_path: str | None
    quarantine_path: str | None
    sha256: str
    phash_dist: int | None
    dhash_dist: int | None
    matched_by: str
    transform: str
    distance_to_keeper: int
    keeper_reason: str | None
    burst_hint: bool


@dataclass
class Group:
    group_id: int
    size: int
    min_distance: int
    members: list[GroupMember]


def pending_ids(order: str = "size_desc_distance_asc") -> list[int]:
    """Return the pending group ids in review order."""
    if order != "size_desc_distance_asc":
        raise ValueError(order)
    with db.connection() as conn:
        rows = conn.execute("""
            select id from dedupe_groups
            where status = 'pending'
            order by size desc, min_distance asc, id asc
        """).fetchall()
    return [r[0] for r in rows]


def load_group(group_id: int) -> Group | None:
    with db.connection() as conn:
        g = conn.execute("""
            select id, size, min_distance from dedupe_groups
            where id = %s
        """, (group_id,)).fetchone()
        if g is None:
            return None
        rows = conn.execute("""
            select m.photo_id, m.is_keeper, m.phash_dist, m.dhash_dist,
                   m.matched_by, m.transform, m.distance_to_keeper,
                   m.keeper_reason,
                   p.is_scan, p.triage_status, p.is_private, p.mime,
                   p.width, p.height, p.file_size,
                   p.exif_taken_at, p.exif_camera,
                   p.scan_batch, p.scan_sequence,
                   p.source_folder, p.source_filename,
                   p.working_path, p.quarantine_path, p.sha256,
                   coalesce(h.hint = 'burst', false) as burst_hint
            from dedupe_members m
            join photos p on p.id = m.photo_id
            left join triage_hints h on h.photo_id = m.photo_id
            where m.group_id = %s
            order by m.is_keeper desc, m.photo_id asc
        """, (group_id,)).fetchall()
    members = [
        GroupMember(
            photo_id=r[0], is_keeper=bool(r[1]),
            phash_dist=r[2], dhash_dist=r[3],
            matched_by=r[4], transform=r[5],
            distance_to_keeper=r[6], keeper_reason=r[7],
            is_scan=bool(r[8]), triage_status=r[9], is_private=bool(r[10]),
            mime=r[11], width=r[12], height=r[13], file_size=r[14],
            exif_taken_at=r[15], exif_camera=r[16],
            scan_batch=r[17], scan_sequence=r[18],
            source_folder=r[19], source_filename=r[20],
            working_path=r[21], quarantine_path=r[22], sha256=r[23],
            burst_hint=bool(r[24]),
        )
        for r in rows
    ]
    return Group(
        group_id=g[0], size=g[1], min_distance=g[2], members=members,
    )
