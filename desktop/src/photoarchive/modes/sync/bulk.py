"""Bulk group assign / unassign from the desktop.

Applied locally on the laptop DB, then the next Sync push carries the
photo_groups rows to the web. Cheaper and safer than round-tripping
through the web API for the 12 800 back-catalogue photos.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from psycopg.rows import dict_row

from ... import db

log = logging.getLogger(__name__)


def resolve_photo_ids(
    conn,
    *,
    album_id: int | None = None,
    scan_batch: str | None = None,
    person_id: int | None = None,
    year: int | None = None,
    decade: int | None = None,
    source_folder: str | None = None,
    ids: list[int] | None = None,
) -> list[int]:
    if ids:
        return sorted(set(int(x) for x in ids))
    clauses = ["is_deleted = false", "triage_status <> 'junk'"]
    params: list[Any] = []
    if album_id is not None:
        params.append(album_id)
        clauses.append(
            f"exists (select 1 from album_photos ap "
            f"          where ap.photo_id = photos.id and ap.album_id = %s)"
        )
    if scan_batch is not None:
        params.append(scan_batch)
        clauses.append("scan_batch = %s")
    if person_id is not None:
        params.append(person_id)
        clauses.append(
            "exists (select 1 from faces f where f.photo_id = photos.id "
            "         and f.person_id = %s and f.is_deleted = false and f.is_disputed = false)"
        )
    if year is not None:
        params.append(year)
        clauses.append("extract(year from capture_date) = %s")
    if decade is not None:
        params += [decade, decade + 9]
        clauses.append("extract(year from capture_date) between %s and %s")
    if source_folder is not None:
        params.append(source_folder)
        clauses.append("source_folder = %s")
    with conn.cursor() as cur:
        cur.execute(
            f"select id from photos where {' and '.join(clauses)} order by id",
            params,
        )
        return [int(r[0]) for r in cur.fetchall()]


def unfiled_ids(conn) -> list[int]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select p.id from photos p
             where p.is_deleted = false
               and p.is_private = false
               and p.triage_status <> 'junk'
               and not exists (
                   select 1 from photo_groups pg
                    where pg.photo_id = p.id and pg.is_deleted = false
               )
             order by p.id
            """
        )
        return [int(r[0]) for r in cur.fetchall()]


def bulk_apply(conn, photo_ids: list[int], *, add: list[int], remove: list[int],
               actor: str = "desktop") -> dict:
    """Apply add/remove group ops in one transaction. Returns counts."""
    added = removed = 0
    if not photo_ids:
        return {"photos": 0, "added": 0, "removed": 0}
    with conn.cursor() as cur:
        for gid in add:
            cur.execute(
                """
                insert into photo_groups (photo_id, group_id, is_deleted)
                     select unnest(%s::bigint[]), %s, false
                on conflict (photo_id, group_id) do update
                     set is_deleted = false, deleted_at = null, deleted_by = null,
                         updated_at = now()
                """,
                (photo_ids, gid),
            )
            added += cur.rowcount
        for gid in remove:
            cur.execute(
                """
                update photo_groups
                   set is_deleted = true, deleted_at = now(), updated_at = now()
                 where group_id = %s
                   and photo_id = any(%s::bigint[])
                   and is_deleted = false
                """,
                (gid, photo_ids),
            )
            removed += cur.rowcount
    db.audit(conn, actor=actor, action="photo_group.bulk",
             entity_type="photo_groups", entity_id=None,
             new_value={"photos": len(photo_ids), "add": add, "remove": remove,
                        "added": added, "removed": removed})
    return {"photos": len(photo_ids), "added": added, "removed": removed}


def group_summary(conn) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            select g.id, g.name,
                   (select count(*)::int from photo_groups pg
                     where pg.group_id = g.id and pg.is_deleted = false) as photo_count,
                   (select count(*)::int from group_members gm
                     where gm.group_id = g.id and gm.is_deleted = false) as member_count
              from groups g
             where g.is_deleted = false
             order by g.name
            """
        )
        return cur.fetchall()
