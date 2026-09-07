"""Undo a resolved dedupe group. Reads the `dedupe.resolve` audit row,
reverses every carry-over move, restores each loser via
`triage.apply_decision(prior_status)`, and reopens the group as pending.

Order (inverse of resolve):
  1. In one transaction:
     * strip appended physical_ref notes from the keeper,
     * re-point photo_backs, photo_masters, suggestions back to each loser,
     * reinstate album memberships (undo skipped-because-duplicate: put the
       loser row back if we deleted it; reverse a moved row if we
       renamed it),
     * clear the keeper's `is_private` if resolve set it,
     * reopen the group (`status='pending'`, `resolved_at=null`,
       `resolved_by=null`),
     * write an `dedupe.undo` audit row referencing the resolve audit row.
  2. For each loser, call `triage.apply_decision(loser_id, prior_status)`.
     This restores the file from quarantine to working/.
"""
from __future__ import annotations

import json
import logging

import psycopg

from ... import db
from ...config import Settings
from ..triage import decisions as triage

log = logging.getLogger(__name__)


def _load_resolve_payload(
    conn: psycopg.Connection, audit_id: int,
) -> dict:
    row = conn.execute("""
        select action, entity_id, new_value
        from audit_log where id = %s
    """, (audit_id,)).fetchone()
    if row is None:
        raise ValueError(f"audit row {audit_id} not found")
    if row[0] != "dedupe.resolve":
        raise ValueError(
            f"audit row {audit_id} action is {row[0]!r}, not dedupe.resolve"
        )
    payload = row[2]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return payload


def _strip_physical_ref(
    conn: psycopg.Connection, keeper_id: int, appended: str | None,
) -> None:
    if not appended:
        return
    row = conn.execute(
        "select physical_ref_note from photos where id = %s", (keeper_id,),
    ).fetchone()
    current = row[0] if row and row[0] else ""
    # Remove the appended chunk. It was appended with " | " unless it was
    # the sole entry.
    sep = " | "
    if current == appended:
        new_val: str | None = None
    elif current.endswith(sep + appended):
        new_val = current[: -(len(sep) + len(appended))] or None
    elif appended in current:
        # Fallback: replace once. Preserve leading text.
        stripped = current.replace(sep + appended, "", 1)
        if stripped == current:
            stripped = current.replace(appended + sep, "", 1)
        if stripped == current:
            stripped = current.replace(appended, "", 1)
        new_val = stripped or None
    else:
        # Nothing to remove — keeper's note was edited elsewhere. Leave.
        return
    conn.execute(
        "update photos set physical_ref_note = %s where id = %s",
        (new_val, keeper_id),
    )


def _restore_backs(
    conn: psycopg.Connection, loser_id: int, back_ids: list[int],
) -> None:
    if not back_ids:
        return
    conn.execute(
        "update photo_backs set photo_id = %s where id = any(%s)",
        (loser_id, back_ids),
    )


def _restore_masters(
    conn: psycopg.Connection, loser_id: int, master_ids: list[int],
) -> None:
    if not master_ids:
        return
    # Set is_preferred = (this is the photo's own master, matching sha256).
    conn.execute("""
        update photo_masters
        set photo_id = %s,
            is_preferred = case
              when sha256 = (select sha256 from photos where id = %s) then true
              else false
            end
        where id = any(%s)
    """, (loser_id, loser_id, master_ids))


def _restore_albums(
    conn: psycopg.Connection, loser_id: int, keeper_id: int,
    moves: list[dict],
) -> None:
    for move in moves:
        album_id = move["album_id"]
        position = move.get("position")
        if move["moved"]:
            # Move keeper's row back to loser. Skip if the keeper is no
            # longer in the album (edited elsewhere).
            conn.execute("""
                update album_photos
                set photo_id = %s
                where album_id = %s and photo_id = %s
            """, (loser_id, album_id, keeper_id))
        else:
            # Reinsert the loser row we deleted (keeper's own row stays).
            conn.execute("""
                insert into album_photos (album_id, photo_id, position)
                values (%s, %s, %s)
                on conflict do nothing
            """, (album_id, loser_id, position))


def _restore_suggestions(
    conn: psycopg.Connection, loser_id: int, suggestion_ids: list[int],
) -> None:
    if not suggestion_ids:
        return
    conn.execute(
        "update suggestions set photo_id = %s where id = any(%s)",
        (loser_id, suggestion_ids),
    )


def undo_resolve(
    settings: Settings, group_id: int, *, actor: str = "desktop",
) -> None:
    """Reverse a resolved group. Looks up the most recent
    dedupe.resolve audit row for this group, reverses all carry-over,
    reopens the group, and restores losers via triage.apply_decision.
    """
    with db.connection() as conn:
        conn.autocommit = False
        try:
            row = conn.execute("""
                select id from audit_log
                where action = 'dedupe.resolve'
                  and entity_type = 'dedupe_group'
                  and entity_id = %s
                order by id desc
                limit 1
            """, (group_id,)).fetchone()
            if row is None:
                raise ValueError(
                    f"no dedupe.resolve audit row found for group {group_id}"
                )
            audit_id = row[0]
            payload = _load_resolve_payload(conn, audit_id)
            keeper_id = payload["keeper_id"]
            losers = payload["losers"]

            for loser in losers:
                _strip_physical_ref(
                    conn, keeper_id, loser.get("physical_ref_appended"),
                )
                _restore_backs(conn, loser["loser_id"], loser["back_ids_moved"])
                _restore_masters(
                    conn, loser["loser_id"], loser["master_ids_moved"],
                )
                _restore_albums(
                    conn, loser["loser_id"], keeper_id,
                    loser["album_photo_moves"],
                )
                _restore_suggestions(
                    conn, loser["loser_id"], loser["suggestion_ids_moved"],
                )

            if payload.get("keeper_made_private"):
                conn.execute(
                    "update photos set is_private = false where id = %s",
                    (keeper_id,),
                )

            conn.execute("""
                update dedupe_groups
                set status = 'pending', resolved_at = null, resolved_by = null
                where id = %s
            """, (group_id,))

            conn.execute("""
                insert into audit_log
                  (actor, action, entity_type, entity_id, previous_value, new_value)
                values (%s, 'dedupe.undo', 'dedupe_group', %s,
                        %s::jsonb, %s::jsonb)
            """, (actor, group_id,
                  json.dumps({"resolve_audit_id": audit_id}),
                  json.dumps({"group_id": group_id})))

            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # Restore each loser to its prior state via triage. Its own audit row
    # records the transition. The file moves out of quarantine.
    for loser in losers:
        try:
            triage.apply_decision(
                settings, loser["loser_id"], loser["prior_triage_status"],
                hint=f"dedupe_undo_of {payload['group_id']}",
                actor=actor,
            )
        except Exception as e:
            log.error(
                "dedupe.undo: triage.apply_decision(%d, %s) failed: %s",
                loser["loser_id"], loser["prior_triage_status"], e,
            )
