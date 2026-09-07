"""'Not duplicates' persistence. Every pair George rejects becomes a
row in `dedupe_exclusions` (ordered least-first, unique) so it never
returns from `dedupe_scan`, even after re-runs.

`mark_group_not_duplicates` sets the group's status and inserts every
pairwise exclusion in the group.
"""
from __future__ import annotations

import json
from itertools import combinations

import psycopg

from ... import db
from ...config import Settings


def _ordered(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def add_exclusion(
    conn: psycopg.Connection, photo_a: int, photo_b: int,
    *, reason: str | None = None,
) -> None:
    """Insert a single exclusion pair, ordered (a<b). Idempotent."""
    if photo_a == photo_b:
        raise ValueError("cannot exclude a photo from itself")
    a, b = _ordered(photo_a, photo_b)
    conn.execute("""
        insert into dedupe_exclusions (photo_a, photo_b, reason)
        values (%s, %s, %s)
        on conflict (photo_a, photo_b) do nothing
    """, (a, b, reason))


def mark_group_not_duplicates(
    settings: Settings, group_id: int, *,
    actor: str = "desktop",
    reason: str | None = None,
) -> int:
    """Set the group's status to not_duplicates and insert every pair
    from the group into dedupe_exclusions. Returns the number of
    exclusions inserted (existing ones are skipped, not counted).
    """
    inserted = 0
    with db.connection() as conn:
        conn.autocommit = False
        try:
            rows = conn.execute("""
                select status, (select array_agg(photo_id order by photo_id)
                                from dedupe_members where group_id = g.id)
                from dedupe_groups g where g.id = %s
            """, (group_id,)).fetchone()
            if rows is None:
                raise ValueError(f"dedupe group {group_id} not found")
            status, members = rows
            if status != "pending":
                raise ValueError(
                    f"dedupe group {group_id} is {status!r}, not pending"
                )
            if not members or len(members) < 2:
                raise ValueError(f"dedupe group {group_id} has fewer than 2 members")

            reason_text = reason or "reviewer said not duplicates"
            for a, b in combinations(members, 2):
                a, b = _ordered(a, b)
                r = conn.execute("""
                    insert into dedupe_exclusions (photo_a, photo_b, reason)
                    values (%s, %s, %s)
                    on conflict (photo_a, photo_b) do nothing
                    returning id
                """, (a, b, reason_text)).fetchone()
                if r is not None:
                    inserted += 1

            conn.execute("""
                update dedupe_groups
                set status = 'not_duplicates',
                    resolved_at = now(),
                    resolved_by = %s
                where id = %s
            """, (actor, group_id))

            conn.execute("""
                insert into audit_log
                  (actor, action, entity_type, entity_id, new_value)
                values (%s, 'dedupe.not_duplicates', 'dedupe_group', %s, %s::jsonb)
            """, (actor, group_id,
                  json.dumps({"members": list(members), "inserted": inserted})))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return inserted
