"""Merge two people into one. Loser is soft-deleted; every face and every
name variant is moved onto the winner. Audit rows record both directions
of the merge."""

from __future__ import annotations

import logging

import psycopg

from ... import db as dbmod

log = logging.getLogger(__name__)


def merge_people(
    conn: psycopg.Connection,
    *,
    winner_id: int,
    loser_id: int,
    reason: str = "duplicate_person",
) -> dict[str, int]:
    if winner_id == loser_id:
        raise ValueError("winner and loser cannot be the same person")

    # Move faces
    moved_faces = conn.execute(
        """
        update faces set person_id = %s
        where person_id = %s
        returning id
        """,
        (winner_id, loser_id),
    ).fetchall()

    # Move the loser's **live** name variants (fix-up 1: removal is a
    # soft-delete, and a name somebody removed must not come back through
    # a merge). Deduped by the (person_id, lower(variant)) unique.
    moved_variants = 0
    try:
        moved_variants = int(conn.execute(
            """
            update person_name_variants set person_id = %s
            where person_id = %s
              and is_deleted = false
              and not exists (
                select 1 from person_name_variants v2
                where v2.person_id = %s
                  and lower(v2.variant) = lower(person_name_variants.variant)
              )
            returning id
            """,
            (winner_id, loser_id, winner_id),
        ).rowcount or 0)
    except psycopg.Error as e:
        # If our anti-dup filter still leaves a race, fall back to nothing;
        # variants can be re-added manually.
        log.warning("merge_people: variant move partial: %s", e)

    # Leftovers are the ones that would have collided with a name the
    # winner already has. Soft-delete them rather than deleting: the rows
    # are pushed, so a real delete would leave the web showing a nickname
    # attached to a person who no longer exists.
    conn.execute(
        """
        update person_name_variants
           set is_deleted = true, deleted_at = now(), edited_on_desktop_at = now()
         where person_id = %s and is_deleted = false
        """,
        (loser_id,),
    )

    # Soft-delete the loser
    conn.execute(
        "update people set is_deleted = true where id = %s",
        (loser_id,),
    )

    dbmod.audit(
        conn, actor="desktop", action="person.merge",
        entity_type="person", entity_id=winner_id,
        previous_value={"loser_id": loser_id},
        new_value={"reason": reason, "faces_moved": len(moved_faces),
                   "variants_moved": moved_variants},
    )
    dbmod.audit(
        conn, actor="desktop", action="person.merged_into",
        entity_type="person", entity_id=loser_id,
        new_value={"winner_id": winner_id, "reason": reason},
    )
    return {"faces_moved": len(moved_faces), "variants_moved": moved_variants}
