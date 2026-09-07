"""Resolve a pending dedupe group: quarantine losers, carry metadata,
persist enough audit data for undo.

Order (matches CLAUDE.md's "DB is the source of truth, then file moves"):
  1. Verify the group is pending and pick the keeper (either the one the
     scan chose or an override from the reviewer).
  2. For each loser, call `triage.apply_decision(loser_id, 'junk',
     hint=f'dedupe_loser_of {keeper_id}')`. This performs the loser's
     triage transition, writes a `triage.decision` audit row with the
     dedupe hint, and moves the file to quarantine.
  3. In one carry-over transaction on the (now-junked) losers:
       * append the loser's scan-locator to the keeper's `physical_ref_note`
         when the loser is a scan and the keeper is not,
       * re-point `photo_backs.photo_id`,
       * re-parent `photo_masters` (non-preferred; keeper's preferred and
         sha256 are untouched),
       * move album memberships (skip if the keeper is already a member),
       * move suggestions (skip identical (kind, source, payload)),
       * set the keeper's `is_private` if any member was private,
       * mark the group `resolved`, `resolved_at=now()`, `resolved_by`,
       * write one `dedupe.resolve` audit row whose `new_value` carries
         the per-loser payload needed by `undo_resolve`.

Undo consults the `dedupe.resolve` audit row and reverses the
metadata moves, then restores the losers via `triage.apply_decision`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import psycopg

from ... import db
from ...config import Settings
from ..triage import decisions as triage
from .keeper import MemberFacts, pick_keeper

log = logging.getLogger(__name__)


@dataclass
class LoserCarryOver:
    loser_id: int
    prior_triage_status: str
    prior_is_private: bool
    prior_working_path: str | None
    physical_ref_appended: str | None = None
    back_ids_moved: list[int] = field(default_factory=list)
    master_ids_moved: list[int] = field(default_factory=list)
    album_photo_moves: list[dict] = field(default_factory=list)
    suggestion_ids_moved: list[int] = field(default_factory=list)


@dataclass
class ResolveResult:
    group_id: int
    keeper_id: int
    keeper_reason: str
    loser_carry_overs: list[LoserCarryOver]
    audit_id: int
    keeper_made_private: bool


def _load_group(
    conn: psycopg.Connection, group_id: int,
) -> tuple[list[int], list[int]]:
    rows = conn.execute("""
        select photo_id, is_keeper
        from dedupe_members where group_id = %s
        order by photo_id
    """, (group_id,)).fetchall()
    if not rows:
        raise ValueError(f"dedupe group {group_id} has no members")
    members = [r[0] for r in rows]
    keeper_candidates = [r[0] for r in rows if r[1]]
    return members, keeper_candidates


def _load_facts(
    conn: psycopg.Connection, photo_ids: list[int],
) -> dict[int, MemberFacts]:
    rows = conn.execute("""
        select id, is_scan, mime, width, height, file_size,
               exif_taken_at, exif_camera
        from photos where id = any(%s)
    """, (photo_ids,)).fetchall()
    return {
        r[0]: MemberFacts(
            photo_id=r[0], is_scan=bool(r[1]), mime=r[2],
            width=r[3], height=r[4], file_size=r[5],
            exif_taken_at=r[6], exif_camera=r[7],
        )
        for r in rows
    }


def _snapshot_loser(conn: psycopg.Connection, photo_id: int) -> LoserCarryOver:
    row = conn.execute("""
        select triage_status, is_private, working_path
        from photos where id = %s
    """, (photo_id,)).fetchone()
    if row is None:
        raise ValueError(f"photo {photo_id} not found")
    return LoserCarryOver(
        loser_id=photo_id,
        prior_triage_status=row[0],
        prior_is_private=bool(row[1]),
        prior_working_path=row[2],
    )


def _append_physical_ref(
    conn: psycopg.Connection, keeper_id: int, keeper_row: dict, loser_id: int,
) -> str | None:
    """When a loser is a scan and the keeper is not, append a scan-locator
    note to the keeper's `physical_ref_note`. Returns the exact text
    that was appended (for undo), or None if nothing changed.
    """
    loser = conn.execute("""
        select is_scan, scan_batch, scan_sequence, source_filename
        from photos where id = %s
    """, (loser_id,)).fetchone()
    if loser is None:
        return None
    l_is_scan, batch, seq, filename = loser
    if not l_is_scan or keeper_row["is_scan"]:
        return None
    if batch and seq is not None:
        note = f"also scanned: {batch} #{int(seq):03d}"
    elif batch:
        note = f"also scanned: {batch} ({filename})"
    else:
        note = f"also scanned: {filename}"
    existing = keeper_row["physical_ref_note"] or ""
    new_val = f"{existing} | {note}" if existing else note
    conn.execute(
        "update photos set physical_ref_note = %s where id = %s",
        (new_val, keeper_id),
    )
    keeper_row["physical_ref_note"] = new_val
    return note


def _move_backs(
    conn: psycopg.Connection, keeper_id: int, loser_id: int,
) -> list[int]:
    rows = conn.execute(
        "select id from photo_backs where photo_id = %s", (loser_id,),
    ).fetchall()
    if not rows:
        return []
    ids = [r[0] for r in rows]
    conn.execute(
        "update photo_backs set photo_id = %s where id = any(%s)",
        (keeper_id, ids),
    )
    return ids


def _move_masters(
    conn: psycopg.Connection, keeper_id: int, loser_id: int,
) -> list[int]:
    """Re-parent the loser's photo_masters rows to the keeper, marking
    them all non-preferred. The keeper's existing preferred master and
    `photos.sha256` are untouched.
    """
    rows = conn.execute(
        "select id from photo_masters where photo_id = %s", (loser_id,),
    ).fetchall()
    if not rows:
        return []
    ids = [r[0] for r in rows]
    conn.execute("""
        update photo_masters
        set photo_id = %s, is_preferred = false
        where id = any(%s)
    """, (keeper_id, ids))
    return ids


def _move_album_memberships(
    conn: psycopg.Connection, keeper_id: int, loser_id: int,
) -> list[dict]:
    """Move album memberships. If keeper already in the album, delete
    the loser row (skip the move) but record it for undo. Each entry is
    {album_id, position, moved: bool}.
    """
    rows = conn.execute("""
        select album_id, position from album_photos where photo_id = %s
    """, (loser_id,)).fetchall()
    if not rows:
        return []
    moves: list[dict] = []
    for album_id, position in rows:
        exists = conn.execute("""
            select 1 from album_photos
            where album_id = %s and photo_id = %s
        """, (album_id, keeper_id)).fetchone()
        if exists:
            conn.execute("""
                delete from album_photos
                where album_id = %s and photo_id = %s
            """, (album_id, loser_id))
            moves.append({"album_id": album_id, "position": position, "moved": False})
        else:
            conn.execute("""
                update album_photos
                set photo_id = %s
                where album_id = %s and photo_id = %s
            """, (keeper_id, album_id, loser_id))
            moves.append({"album_id": album_id, "position": position, "moved": True})
    return moves


def _move_suggestions(
    conn: psycopg.Connection, keeper_id: int, loser_id: int,
) -> list[int]:
    """Re-parent the loser's suggestions to the keeper unless an
    identical (kind, source, payload) suggestion already exists on the
    keeper. Comparison is over the JSONB payload as text (stable enough
    for exact duplicate detection — the goal is not to reformat).
    """
    rows = conn.execute("""
        select id, kind, source, payload::text
        from suggestions where photo_id = %s
    """, (loser_id,)).fetchall()
    if not rows:
        return []
    moved: list[int] = []
    for sug_id, kind, source, payload_text in rows:
        exists = conn.execute("""
            select 1 from suggestions
            where photo_id = %s and kind = %s and source = %s
              and payload::text = %s
        """, (keeper_id, kind, source, payload_text)).fetchone()
        if exists:
            continue
        conn.execute(
            "update suggestions set photo_id = %s where id = %s",
            (keeper_id, sug_id),
        )
        moved.append(sug_id)
    return moved


def _promote_keeper_private(
    conn: psycopg.Connection, keeper_id: int, member_ids: list[int],
    loser_priors: dict[int, bool],
) -> bool:
    """If any loser WAS private (before junk) or the keeper is private,
    set the keeper's `is_private=true`. Returns True if the flag was
    changed by this call (so undo knows to reset it).
    """
    any_private_prior = any(loser_priors.values())
    keeper_prior_row = conn.execute("""
        select is_private, triage_status from photos where id = %s
    """, (keeper_id,)).fetchone()
    keeper_currently_private = bool(keeper_prior_row[0])
    if not any_private_prior and not keeper_currently_private:
        return False
    if keeper_currently_private:
        return False
    conn.execute(
        "update photos set is_private = true where id = %s", (keeper_id,),
    )
    return True


def resolve_group(
    settings: Settings, group_id: int, *,
    chosen_keeper_id: int | None = None,
    actor: str = "desktop",
) -> ResolveResult:
    """Resolve a pending dedupe group. If `chosen_keeper_id` is None,
    use the scan's pre-selected keeper.
    """
    with db.connection() as conn:
        conn.autocommit = False
        try:
            status_row = conn.execute(
                "select status from dedupe_groups where id = %s", (group_id,),
            ).fetchone()
            if status_row is None:
                raise ValueError(f"dedupe group {group_id} not found")
            if status_row[0] != "pending":
                raise ValueError(
                    f"dedupe group {group_id} is {status_row[0]!r}, not pending"
                )

            member_ids, stored_keeper = _load_group(conn, group_id)
            facts = _load_facts(conn, member_ids)

            if chosen_keeper_id is not None:
                if chosen_keeper_id not in member_ids:
                    raise ValueError(
                        f"chosen keeper {chosen_keeper_id} is not a member of group {group_id}"
                    )
                keeper_id = chosen_keeper_id
                keeper_reason = "chosen by reviewer"
            elif stored_keeper:
                keeper_id = stored_keeper[0]
                r = conn.execute("""
                    select keeper_reason from dedupe_members
                    where group_id = %s and photo_id = %s
                """, (group_id, keeper_id)).fetchone()
                keeper_reason = (r[0] if r and r[0] else "scan default")
            else:
                keeper = pick_keeper(list(facts.values()))
                keeper_id = keeper.keeper_id
                keeper_reason = keeper.reason

            loser_ids = [pid for pid in member_ids if pid != keeper_id]
            loser_snapshots = [_snapshot_loser(conn, lid) for lid in loser_ids]
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    loser_prior_private = {
        co.loser_id: co.prior_is_private or co.prior_triage_status == "private"
        for co in loser_snapshots
    }

    # Junk each loser via triage. Its own audit row records the hint.
    for co in loser_snapshots:
        triage.apply_decision(
            settings, co.loser_id, "junk",
            hint=f"dedupe_loser_of {keeper_id}",
            actor=actor,
        )

    # One transaction for the metadata carry-over + group state + audit.
    with db.connection() as conn:
        conn.autocommit = False
        try:
            k_row = conn.execute("""
                select is_scan, physical_ref_note from photos where id = %s
            """, (keeper_id,)).fetchone()
            keeper_row = {
                "is_scan": bool(k_row[0]),
                "physical_ref_note": k_row[1],
            }

            for co in loser_snapshots:
                co.physical_ref_appended = _append_physical_ref(
                    conn, keeper_id, keeper_row, co.loser_id,
                )
                co.back_ids_moved = _move_backs(conn, keeper_id, co.loser_id)
                co.master_ids_moved = _move_masters(conn, keeper_id, co.loser_id)
                co.album_photo_moves = _move_album_memberships(
                    conn, keeper_id, co.loser_id,
                )
                co.suggestion_ids_moved = _move_suggestions(
                    conn, keeper_id, co.loser_id,
                )

            keeper_made_private = _promote_keeper_private(
                conn, keeper_id, member_ids, loser_prior_private,
            )

            conn.execute("""
                update dedupe_members set is_keeper = (photo_id = %s),
                       keeper_reason = case when photo_id = %s then %s else keeper_reason end
                where group_id = %s
            """, (keeper_id, keeper_id, keeper_reason, group_id))
            conn.execute("""
                update dedupe_groups
                set status = 'resolved', resolved_at = now(), resolved_by = %s
                where id = %s
            """, (actor, group_id))

            payload = {
                "group_id": group_id,
                "keeper_id": keeper_id,
                "keeper_reason": keeper_reason,
                "keeper_made_private": keeper_made_private,
                "losers": [
                    {
                        "loser_id": co.loser_id,
                        "prior_triage_status": co.prior_triage_status,
                        "prior_is_private": co.prior_is_private,
                        "prior_working_path": co.prior_working_path,
                        "physical_ref_appended": co.physical_ref_appended,
                        "back_ids_moved": co.back_ids_moved,
                        "master_ids_moved": co.master_ids_moved,
                        "album_photo_moves": co.album_photo_moves,
                        "suggestion_ids_moved": co.suggestion_ids_moved,
                    }
                    for co in loser_snapshots
                ],
            }
            audit_id = conn.execute("""
                insert into audit_log
                  (actor, action, entity_type, entity_id, new_value)
                values (%s, 'dedupe.resolve', 'dedupe_group', %s, %s::jsonb)
                returning id
            """, (actor, group_id, _jsonb(payload))).fetchone()[0]

            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return ResolveResult(
        group_id=group_id, keeper_id=keeper_id, keeper_reason=keeper_reason,
        loser_carry_overs=loser_snapshots, audit_id=audit_id,
        keeper_made_private=keeper_made_private,
    )


def _jsonb(v: Any) -> str:
    import json
    return json.dumps(v, default=str)
