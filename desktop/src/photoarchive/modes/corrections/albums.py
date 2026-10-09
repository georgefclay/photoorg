"""Albums: rename, soft-delete, reorder, remove a photo.

Albums are desktop-authoritative — the web stays read-only for them
until an album pull-back exists (PROJECT-PLAN §5 item 11), which is why
the editor carries that sentence on screen. Everything here is an
ordinary `update` that `/sync/albums` and `/sync/album_photos` assign in
their `do update set`, so an edit reaches the VM on the next push with
nothing else to remember (Phase 9 fix-up 2).

The one thing that needed a migration: **a removal has to be a
soft-delete.** `album_photos` was insert-or-update-only and the push only
upserts, so "take this photo out of the album" never reached the web and
the site kept showing it there. Phase 15's migration gives the table the
same `is_deleted / deleted_at / deleted_by / updated_at` shape as
`photo_groups`, and the sync route does LWW by `updated_at` exactly as
that one does. Nothing here hard-deletes a row — "no real deletes, ever"
covers an album membership as much as a photograph.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import psycopg
from psycopg.rows import dict_row

from ... import db as dbmod

log = logging.getLogger(__name__)


@dataclass
class AlbumRow:
    id: int
    name: str
    description: str | None
    source: str
    is_deleted: bool
    photo_count: int


@dataclass
class AlbumPhotoRow:
    photo_id: int
    position: int | None
    source_filename: str | None
    capture_date: object | None
    is_private: bool
    triage_status: str


def list_albums(
    conn: psycopg.Connection, search: str = "", *, include_deleted: bool = False,
) -> list[AlbumRow]:
    where = ["true"]
    params: list[object] = []
    if not include_deleted:
        where.append("a.is_deleted = false")
    if search.strip():
        where.append("a.name ilike %s")
        params.append(f"%{search.strip()}%")
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            select a.id, a.name, a.description, a.source, a.is_deleted,
                   (select count(*) from album_photos ap
                     where ap.album_id = a.id and ap.is_deleted = false)::int as photo_count
              from albums a
             where {' and '.join(where)}
             order by a.is_deleted, lower(a.name)
            """,
            params,
        )
        return [AlbumRow(**r) for r in cur.fetchall()]


def album_photos(conn: psycopg.Connection, album_id: int) -> list[AlbumPhotoRow]:
    """Live memberships, in album order. Soft-deleted rows are left out —
    the restore path is by id, from the audit row."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            select ap.photo_id, ap.position, p.source_filename, p.capture_date,
                   p.is_private, p.triage_status
              from album_photos ap
              join photos p on p.id = ap.photo_id
             where ap.album_id = %s and ap.is_deleted = false
             order by ap.position nulls last, ap.photo_id
            """,
            (album_id,),
        )
        return [AlbumPhotoRow(**r) for r in cur.fetchall()]


def update_album(
    conn: psycopg.Connection, album_id: int, *,
    name: str | None = None, description: str | None = None,
    actor: str = "desktop",
) -> bool:
    """Rename and/or re-describe. One audit row with previous and new."""
    prev = conn.execute(
        "select name, description from albums where id = %s", (album_id,)
    ).fetchone()
    if prev is None:
        return False
    fields: dict[str, object] = {}
    if name is not None and name.strip() and name.strip() != prev[0]:
        fields["name"] = name.strip()
    if description is not None and (description or None) != prev[1]:
        fields["description"] = description or None
    if not fields:
        return False

    sets = ", ".join(f"{k} = %s" for k in fields)
    conn.execute(
        f"update albums set {sets}, edited_on_desktop_at = now() where id = %s",
        [*fields.values(), album_id],
    )
    dbmod.audit(
        conn, actor=actor, action="album.update", entity_type="album",
        entity_id=album_id,
        previous_value={"name": prev[0], "description": prev[1]},
        new_value=fields,
    )
    return True


def set_album_deleted(
    conn: psycopg.Connection, album_id: int, deleted: bool, *, actor: str = "desktop",
) -> bool:
    prev = conn.execute(
        "select name, is_deleted from albums where id = %s", (album_id,)
    ).fetchone()
    if prev is None or bool(prev[1]) == deleted:
        return False
    conn.execute(
        "update albums set is_deleted = %s, edited_on_desktop_at = now() where id = %s",
        (deleted, album_id),
    )
    dbmod.audit(
        conn, actor=actor,
        action="album.delete" if deleted else "album.restore",
        entity_type="album", entity_id=album_id,
        previous_value={"name": prev[0], "is_deleted": bool(prev[1])},
        new_value={"name": prev[0], "is_deleted": deleted},
    )
    return True


def set_photo_in_album(
    conn: psycopg.Connection, album_id: int, photo_id: int, present: bool,
    *, actor: str = "desktop",
) -> bool:
    """Soft-remove (or restore) one photo's membership.

    Never a `delete`: the row carries the flags so the push can tell the
    web the photo left the album. `updated_at` moves with it, which is
    what the LWW on `/sync/album_photos` compares.
    """
    prev = conn.execute(
        "select is_deleted from album_photos where album_id = %s and photo_id = %s",
        (album_id, photo_id),
    ).fetchone()
    if prev is None:
        if not present:
            return False
        conn.execute(
            "insert into album_photos (album_id, photo_id) values (%s, %s)",
            (album_id, photo_id),
        )
    else:
        if bool(prev[0]) == (not present):
            return False
        conn.execute(
            """
            update album_photos
               set is_deleted = %s,
                   deleted_at = case when %s then now() else null end,
                   updated_at = now()
             where album_id = %s and photo_id = %s
            """,
            (not present, not present, album_id, photo_id),
        )
    dbmod.audit(
        conn, actor=actor,
        action="album.photo.add" if present else "album.photo.remove",
        entity_type="album", entity_id=album_id,
        previous_value={"photo_id": photo_id, "is_deleted": (bool(prev[0]) if prev else None)},
        new_value={"photo_id": photo_id, "is_deleted": not present},
    )
    return True


def reorder_album(
    conn: psycopg.Connection, album_id: int, ordered_photo_ids: list[int],
    *, actor: str = "desktop",
) -> int:
    """Write `position` 1..n in the given order.

    Only rows that actually move are written, so the search triggers and
    the sync `updated_at` stay quiet on a no-op reorder.
    """
    current = {r.photo_id: r.position for r in album_photos(conn, album_id)}
    moved = 0
    for index, photo_id in enumerate(ordered_photo_ids, start=1):
        if photo_id not in current or current[photo_id] == index:
            continue
        conn.execute(
            "update album_photos set position = %s, updated_at = now() "
            "where album_id = %s and photo_id = %s",
            (index, album_id, photo_id),
        )
        moved += 1
    if moved:
        dbmod.audit(
            conn, actor=actor, action="album.reorder", entity_type="album",
            entity_id=album_id,
            previous_value={"positions": {str(k): v for k, v in current.items()}},
            new_value={"order": ordered_photo_ids, "moved": moved},
        )
    return moved
