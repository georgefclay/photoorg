"""Places: name, notes, coordinates, aliases.

Desktop-authoritative. `/sync/places` already assigns every editable
column in its `do update set`, and Phase 15 adds `/sync/place_aliases`
so an alias correction reaches the VM too — before this, alias edits
stopped at the laptop (PROJECT-PLAN §5 item 11).

Alias **removal is a soft-delete** since fix-up 1. The row keeps its
`(place_id, alias)` key and gains `is_deleted`, so a removal is
restorable and the sync route flags the web's copy rather than deleting
it. Re-adding a removed alias flips the flag back — a plain insert would
hit the case-insensitive unique and the name would be un-re-addable.

`places.name` is unique on `lower(name)`, so a rename can collide. The
rename refuses and says which place holds the name rather than letting
the constraint abort a larger correction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import psycopg
from psycopg.rows import dict_row

from ... import db as dbmod

log = logging.getLogger(__name__)


@dataclass
class PlaceRow:
    id: int
    name: str
    latitude: float | None
    longitude: float | None
    notes: str | None
    is_deleted: bool
    photo_count: int


def list_places(
    conn: psycopg.Connection, search: str = "", *, include_deleted: bool = False,
) -> list[PlaceRow]:
    where = ["true"]
    params: list[object] = []
    if not include_deleted:
        where.append("pl.is_deleted = false")
    if search.strip():
        where.append("pl.name ilike %s")
        params.append(f"%{search.strip()}%")
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            select pl.id, pl.name, pl.latitude, pl.longitude, pl.notes, pl.is_deleted,
                   (select count(*) from photo_places pp
                     where pp.place_id = pl.id and pp.is_deleted = false)::int as photo_count
              from places pl
             where {' and '.join(where)}
             order by pl.is_deleted, lower(pl.name)
            """,
            params,
        )
        return [PlaceRow(**r) for r in cur.fetchall()]


def list_aliases(conn: psycopg.Connection, place_id: int) -> list[tuple[str, str]]:
    """Live aliases only. Removal is a soft-delete since fix-up 1 — "no
    real deletes, ever" has no exception for text that is its own key."""
    rows = conn.execute(
        """
        select alias, kind from place_aliases
         where place_id = %s and is_deleted = false
         order by lower(alias)
        """,
        (place_id,),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def name_holder(conn: psycopg.Connection, name: str, *, excluding: int) -> int | None:
    row = conn.execute(
        "select id from places where lower(name) = lower(%s) and id <> %s",
        (name.strip(), excluding),
    ).fetchone()
    return int(row[0]) if row else None


def update_place(
    conn: psycopg.Connection, place_id: int, *,
    name: str | None = None, notes: str | None = None,
    latitude: float | None = None, longitude: float | None = None,
    set_coords: bool = False, actor: str = "desktop",
) -> bool:
    """Save the editable fields. `set_coords` distinguishes "clear the
    coordinates" from "leave them alone" — both arrive as None."""
    prev = conn.execute(
        "select name, notes, latitude, longitude from places where id = %s", (place_id,)
    ).fetchone()
    if prev is None:
        return False

    fields: dict[str, object] = {}
    if name is not None and name.strip() and name.strip() != prev[0]:
        fields["name"] = name.strip()
    if notes is not None and (notes or None) != prev[1]:
        fields["notes"] = notes or None
    if set_coords:
        if latitude != prev[2]:
            fields["latitude"] = latitude
        if longitude != prev[3]:
            fields["longitude"] = longitude
    if not fields:
        return False

    sets = ", ".join(f"{k} = %s" for k in fields)
    conn.execute(
        f"update places set {sets}, edited_on_desktop_at = now() where id = %s",
        [*fields.values(), place_id],
    )
    dbmod.audit(
        conn, actor=actor, action="place.update", entity_type="place",
        entity_id=place_id,
        previous_value={
            "name": prev[0], "notes": prev[1],
            "latitude": prev[2], "longitude": prev[3],
        },
        new_value=fields,
    )
    return True


def add_alias(
    conn: psycopg.Connection, place_id: int, alias: str, *,
    kind: str = "alias", actor: str = "desktop",
) -> bool:
    """Add, or bring back a removed alias.

    The row survives a removal, so a plain insert would hit
    `place_aliases_ci_uq` and the alias would be un-re-addable. Match
    case-insensitively (that is what the unique is on), and take the
    caller's spelling.
    """
    alias = alias.strip()
    if not alias:
        return False
    existing = conn.execute(
        """
        select alias, is_deleted from place_aliases
         where place_id = %s and lower(alias) = lower(%s)
        """,
        (place_id, alias),
    ).fetchone()
    if existing is not None:
        if not existing[1]:
            return False  # already live
        conn.execute(
            """
            update place_aliases
               set alias = %s, kind = %s, is_deleted = false, deleted_at = null,
                   deleted_by = null, updated_at = now()
             where place_id = %s and alias = %s
            """,
            (alias, kind, place_id, existing[0]),
        )
        dbmod.audit(
            conn, actor=actor, action="place.alias.add", entity_type="place",
            entity_id=place_id,
            previous_value={"alias": existing[0], "is_deleted": True},
            new_value={"alias": alias, "kind": kind, "restored": True},
        )
        return True

    row = conn.execute(
        """
        insert into place_aliases (place_id, alias, kind) values (%s, %s, %s)
        on conflict do nothing
        returning alias
        """,
        (place_id, alias, kind),
    ).fetchone()
    if row is None:
        return False
    dbmod.audit(
        conn, actor=actor, action="place.alias.add", entity_type="place",
        entity_id=place_id, new_value={"alias": alias, "kind": kind},
    )
    return True


def remove_alias(
    conn: psycopg.Connection, place_id: int, alias: str, *, actor: str = "desktop",
) -> bool:
    """Soft-delete (fix-up 1).

    The first draft issued a real `delete` on the grounds that the text is
    its own key and there was nothing to flag. There is now: the row keeps
    its key and carries `is_deleted`, so the removal is restorable and
    `/sync/place_aliases` can flag the web's copy instead of deleting it.
    """
    row = conn.execute(
        """
        select kind, is_deleted from place_aliases
         where place_id = %s and alias = %s
        """,
        (place_id, alias),
    ).fetchone()
    if row is None or row[1]:
        return False
    conn.execute(
        """
        update place_aliases
           set is_deleted = true, deleted_at = now(), updated_at = now()
         where place_id = %s and alias = %s
        """,
        (place_id, alias),
    )
    dbmod.audit(
        conn, actor=actor, action="place.alias_remove", entity_type="place",
        entity_id=place_id,
        previous_value={"alias": alias, "kind": row[0], "is_deleted": False},
        new_value={"alias": alias, "is_deleted": True},
    )
    return True
