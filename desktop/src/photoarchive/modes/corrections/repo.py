"""Find & replace across the desktop's human-visible text.

The shape of a correction:

1. **Search** — one `ilike`/`like` per target in `targets.TARGETS`, giving
   a count and the matching rows per group. Nothing is written.
2. **Preview** — the replacement is computed in Python, so what the
   dialog shows is literally the string that will be stored. No
   SQL-side `replace()` whose semantics could differ from the preview's.
3. **Apply** — one transaction, one `batch_id`, one `correction.replace`
   audit row per changed row carrying the previous and new value, plus a
   `correction.batch` summary row.
4. **Undo** — reads those audit rows back, so it survives a restart (the
   prompt asks for session-independent undo). A row whose current value
   no longer equals what the correction wrote has been changed since;
   undo **skips it and lists it** rather than clobbering newer work
   (answer 4).

Two things this module deliberately does not do. It never touches the
master-derived columns (`targets` marks them `editable=False` and
`apply_correction` refuses them even if a caller asks), and it never
touches a resolved suggestion — rewriting the text a decision was made
on would falsify the decision.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable

import psycopg
from psycopg.rows import dict_row

from ... import db as dbmod
from .targets import BY_KEY, Shape, Target, TARGETS

log = logging.getLogger(__name__)

#: Guard rail on the preview, not on the apply: a search matching more
#: than this per group is still counted in full, but only this many rows
#: are listed. Apply works from the counts, not from the listed sample.
SAMPLE_LIMIT = 200


@dataclass
class MatchRow:
    target_key: str
    key: dict[str, Any]
    context: str
    value: str
    new_value: str

    @property
    def changed(self) -> bool:
        return self.new_value != self.value


@dataclass
class GroupResult:
    target: Target
    count: int
    rows: list[MatchRow] = field(default_factory=list)
    truncated: bool = False


@dataclass
class ApplyResult:
    batch_id: str
    changed: int
    by_target: dict[str, int] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)


@dataclass
class UndoResult:
    batch_id: str
    restored: int
    skipped_ids: list[str] = field(default_factory=list)


def replace_text(value: str, needle: str, replacement: str, match_case: bool) -> str:
    """The one place a replacement is computed. Preview and apply both
    call it, which is what makes the preview honest."""
    if not needle:
        return value
    if match_case:
        return value.replace(needle, replacement)
    # A lambda for the replacement so backslashes in the user's text are
    # literal and not re group references.
    return re.sub(re.escape(needle), lambda _m: replacement, value, flags=re.IGNORECASE)


def _like_pattern(needle: str) -> str:
    escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def search(
    conn: psycopg.Connection,
    needle: str,
    replacement: str = "",
    *,
    match_case: bool = True,
    sample_limit: int = SAMPLE_LIMIT,
    targets: Iterable[Target] | None = None,
) -> list[GroupResult]:
    """Every group with at least one match, in `TARGETS` order.

    Groups with no match are left out entirely — a results pane listing
    twenty empty headings hides the three that matter.
    """
    if not needle:
        return []
    op = "like" if match_case else "ilike"
    pattern = _like_pattern(needle)
    out: list[GroupResult] = []
    for target in (targets if targets is not None else TARGETS):
        sql = target.find_sql.format(op=op)
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, {"pattern": pattern})
            found = cur.fetchall()
        if not found:
            continue
        rows = [
            MatchRow(
                target_key=target.key,
                key=r["key"],
                context=r["context"] or "",
                value=r["value"],
                new_value=replace_text(r["value"], needle, replacement, match_case),
            )
            for r in found[:sample_limit]
        ]
        out.append(GroupResult(
            target=target, count=len(found), rows=rows,
            truncated=len(found) > sample_limit,
        ))
    return out


# ----------------------------------------------------------------------
# Readers and writers, one pair per row shape.
# ----------------------------------------------------------------------

def key_after_write(
    target: Target, key: dict[str, Any], written: str | None,
) -> dict[str, Any]:
    """The key that identifies the row *after* `written` was stored.

    For almost every target the key is an id and does not move. For
    `place_aliases` the text **is** part of the primary key, so writing a
    corrected alias renames the row: the audit entry records the key as it
    was before the write, and undo looking the row up under that stale key
    finds nothing and skips a row it should have restored. (It did exactly
    that until `test_undo_restores_the_batch_from_the_audit_log` caught
    it.)
    """
    if target.shape is Shape.ALIAS and written is not None:
        return {**key, "alias": written}
    return key


def read_value(conn: psycopg.Connection, target: Target, key: dict[str, Any]) -> str | None:
    if target.shape is Shape.COLUMN:
        row = conn.execute(
            f"select {target.field} from {target.table} where id = %s", (key["id"],)
        ).fetchone()
    elif target.shape is Shape.JSON:
        row = conn.execute(
            "select payload->>%s from suggestions where id = %s",
            (key["path"], key["id"]),
        ).fetchone()
    elif target.shape is Shape.ALIAS:
        row = conn.execute(
            """
            select alias from place_aliases
             where place_id = %s and alias = %s and is_deleted = false
            """,
            (key["place_id"], key["alias"]),
        ).fetchone()
    else:  # pragma: no cover - Shape is closed
        raise ValueError(f"unknown shape {target.shape}")
    return row[0] if row else None


def write_value(
    conn: psycopg.Connection, target: Target, key: dict[str, Any], value: str,
) -> bool:
    """Store `value`. Returns False when the write could not be made —
    today only the alias collision below. Stamps `edited_on_desktop_at`
    on the five tables that carry it (Phase 15 answer 2), which is what
    stops the next push overwriting a web edit of the same row and, in
    the other direction, is the proof a human touched it here.
    """
    if target.shape is Shape.COLUMN:
        stamp = ", edited_on_desktop_at = now()" if target.stamps_edit else ""
        conn.execute(
            f"update {target.table} set {target.field} = %s{stamp} where id = %s",
            (value, key["id"]),
        )
        return True

    if target.shape is Shape.JSON:
        # jsonb_set so the rest of the payload — tags, confidence,
        # prompt_version, parsed_dates — survives the correction.
        conn.execute(
            """
            update suggestions
               set payload = jsonb_set(payload, array[%s], to_jsonb(%s::text)),
                   edited_on_desktop_at = now()
             where id = %s
            """,
            (key["path"], value, key["id"]),
        )
        return True

    if target.shape is Shape.ALIAS:
        # The text is part of the primary key, so the update rewrites the
        # key itself, and it can land on an alias the place already holds
        # (the unique is case-insensitive, so "Canada" and "canada"
        # collide too). Check first and refuse: letting the constraint
        # raise would abort the whole correction over one duplicate, and
        # an upsert that dropped the row on conflict would lose the alias
        # altogether — a delete by accident.
        # A soft-deleted row still holds the key, so it still collides:
        # the check deliberately ignores `is_deleted`.
        clash = conn.execute(
            """
            select 1 from place_aliases
             where place_id = %s and lower(alias) = lower(%s) and alias <> %s
            """,
            (key["place_id"], value, key["alias"]),
        ).fetchone()
        if clash:
            return False
        conn.execute(
            """
            update place_aliases set alias = %s, updated_at = now()
             where place_id = %s and alias = %s
            """,
            (value, key["place_id"], key["alias"]),
        )
        return True

    raise ValueError(f"unknown shape {target.shape}")  # pragma: no cover


# ----------------------------------------------------------------------
# Apply
# ----------------------------------------------------------------------

def apply_correction(
    conn: psycopg.Connection,
    *,
    needle: str,
    replacement: str,
    match_case: bool,
    rows: Iterable[MatchRow],
    actor: str = "desktop",
    batch_id: str | None = None,
) -> ApplyResult:
    """Write the selected rows and audit every one of them.

    The caller passes the exact `MatchRow`s the reviewer ticked, so what
    is written is what was previewed. Each row is re-read first: if it has
    changed since the search it is skipped and listed, the same rule undo
    uses.
    """
    batch = batch_id or uuid.uuid4().hex
    result = ApplyResult(batch_id=batch, changed=0)
    meta = {
        "batch_id": batch, "search": needle, "replace": replacement,
        "match_case": match_case,
    }

    for row in rows:
        target = BY_KEY.get(row.target_key)
        if target is None:
            result.skipped.append(f"{row.target_key}: unknown target")
            continue
        if not target.editable:
            # Belt and braces: the UI gives these no checkbox, and the
            # reason is on screen. A caller that ticks one anyway is a bug,
            # and silently writing the master mirror would be the worst
            # possible outcome of it.
            result.skipped.append(f"{target.label}: mirrors the master disk, not editable")
            continue
        if not row.changed:
            continue

        current = read_value(conn, target, row.key)
        if current != row.value:
            result.skipped.append(
                f"{target.label} {_key_str(row.key)}: changed since the search"
            )
            continue

        if not write_value(conn, target, row.key, row.new_value):
            result.skipped.append(
                f"{target.label} {_key_str(row.key)}: "
                f"'{row.new_value}' already exists on that place"
            )
            continue

        dbmod.audit(
            conn, actor=actor, action="correction.replace",
            entity_type=target.entity_type,
            entity_id=_entity_id(row.key),
            previous_value={"target": target.key, "key": row.key, "value": row.value},
            new_value={**meta, "target": target.key, "key": row.key, "value": row.new_value},
        )
        result.changed += 1
        result.by_target[target.key] = result.by_target.get(target.key, 0) + 1

    dbmod.audit(
        conn, actor=actor, action="correction.batch",
        entity_type="correction", entity_id=None,
        new_value={
            **meta, "changed": result.changed, "by_target": result.by_target,
            "skipped": result.skipped,
        },
    )
    return result


def _entity_id(key: dict[str, Any]) -> int | None:
    for name in ("id", "place_id"):
        if name in key:
            try:
                return int(key[name])
            except (TypeError, ValueError):  # pragma: no cover
                return None
    return None


def _key_str(key: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v}" for k, v in key.items())


# ----------------------------------------------------------------------
# Batches and undo
# ----------------------------------------------------------------------

@dataclass
class BatchSummary:
    batch_id: str
    search: str
    replace: str
    changed: int
    created_at: Any
    undone: bool


def list_batches(conn: psycopg.Connection, limit: int = 50) -> list[BatchSummary]:
    """Recent corrections, newest first, flagged if already undone.

    Read from the audit log, not from session state: the prompt asks for
    an undo that works after a restart.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            select b.new_value->>'batch_id' as batch_id,
                   b.new_value->>'search'   as search,
                   b.new_value->>'replace'  as replace_with,
                   (b.new_value->>'changed')::int as changed,
                   b.created_at,
                   exists (
                     select 1 from audit_log u
                      where u.action = 'correction.undo.batch'
                        and u.new_value->>'batch_id' = b.new_value->>'batch_id'
                   ) as undone
              from audit_log b
             where b.action = 'correction.batch'
               and (b.new_value->>'changed')::int > 0
             order by b.id desc
             limit %s
            """,
            (limit,),
        )
        return [
            BatchSummary(
                batch_id=r["batch_id"], search=r["search"] or "",
                replace=r["replace_with"] or "", changed=r["changed"] or 0,
                created_at=r["created_at"], undone=r["undone"],
            )
            for r in cur.fetchall()
        ]


def undo_batch(
    conn: psycopg.Connection, batch_id: str, *, actor: str = "desktop",
) -> UndoResult:
    """Put back every row of `batch_id` that still holds what the
    correction wrote.

    Answer 4: a row edited since the correction is **skipped and listed**.
    Undo that overwrote newer work would be a second mistake with no
    third chance, and the whole point of the phase is that mistakes stay
    fixable.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            select id, previous_value, new_value
              from audit_log
             where action = 'correction.replace'
               and new_value->>'batch_id' = %s
             order by id desc
            """,
            (batch_id,),
        )
        entries = cur.fetchall()

    result = UndoResult(batch_id=batch_id, restored=0)
    if not entries:
        return result

    for entry in entries:
        prev = entry["previous_value"] or {}
        new = entry["new_value"] or {}
        target = BY_KEY.get(new.get("target") or prev.get("target") or "")
        if target is None or not target.editable:
            result.skipped_ids.append(f"audit #{entry['id']}: unknown target")
            continue
        key = new.get("key") or prev.get("key")
        if not key:  # pragma: no cover - written by apply, always present
            result.skipped_ids.append(f"audit #{entry['id']}: no key")
            continue
        # The audit row holds the key as it was *before* the correction.
        # Where the text is part of the key, the row has been renamed since.
        live_key = key_after_write(target, key, new.get("value"))

        current = read_value(conn, target, live_key)
        if current != new.get("value"):
            result.skipped_ids.append(f"{target.label} {_key_str(key)}")
            continue

        if not write_value(conn, target, live_key, prev.get("value")):
            result.skipped_ids.append(
                f"{target.label} {_key_str(key)}: name now taken"
            )
            continue

        dbmod.audit(
            conn, actor=actor, action="correction.undo",
            entity_type=target.entity_type, entity_id=_entity_id(key),
            previous_value={"target": target.key, "key": key, "value": new.get("value")},
            new_value={
                "target": target.key, "key": key, "value": prev.get("value"),
                "batch_id": batch_id, "undo_of_audit_id": entry["id"],
            },
        )
        result.restored += 1

    dbmod.audit(
        conn, actor=actor, action="correction.undo.batch",
        entity_type="correction", entity_id=None,
        new_value={
            "batch_id": batch_id, "restored": result.restored,
            "skipped": result.skipped_ids,
        },
    )
    return result
