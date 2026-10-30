"""The one audited way a confirmed date stops being a fact.

`unconfirm` clears the value, not just the flag (answer F1): most readers —
Browse's oldest/newest sort, the year and decade filters, a person's year
span, the search index's date range — look at `capture_date` and never at
`capture_date_confirmed`, so a flag-only clear would leave a 1960s print
sorting as 2025. The old value survives twice: as a pending `date`
suggestion, re-acceptable in one click, and in the audit row's `previous`.

The suggestions table has no `details` column, so the reason travels in the
payload beside `evidence` (`payload.reason`, plus `payload.rule` from the
hygiene tool). The web's accept reads only `date` and `precision`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import psycopg

from ... import db as dbmod
from ..ingest.scanner import parse_date_folder
from .rules import DateRow

ACTION = "photo.capture_date.unconfirm"
SET_ACTION = "photo.capture_date.set"
# Below the folder hint's 0.3: the value was wrong enough to be cleared.
CLEARED_CONFIDENCE = 0.2


def human_set(conn: psycopg.Connection, photo_id: int) -> bool:
    """Did a person ever set this photo's date (either accept path)?"""
    return conn.execute(
        """
        select exists (select 1 from audit_log
                        where entity_type = 'photo' and entity_id = %s
                          and action = %s)
        """, (photo_id, SET_ACTION),
    ).fetchone()[0]


def load_ingest_confirmed(conn: psycopg.Connection) -> list[DateRow]:
    """Every confirmed date no person set — i.e. ingest-sourced.

    All photos, junk and soft-deleted included: a restored photo must not
    come back carrying the scanner's clock.
    """
    rows = conn.execute(
        """
        select p.id, p.capture_date, p.capture_date_precision::text,
               p.exif_taken_at, p.is_scan, p.source_root, p.source_folder,
               p.scan_batch,
               coalesce((select min(m.ingested_at) from photo_masters m
                          where m.photo_id = p.id), p.created_at),
               p.triage_status::text, p.is_deleted
          from photos p
         where p.capture_date_confirmed and p.capture_date is not null
           and not exists (select 1 from audit_log al
                            where al.entity_type = 'photo' and al.entity_id = p.id
                              and al.action = %s)
         order by p.id
        """, (SET_ACTION,),
    ).fetchall()
    out: list[DateRow] = []
    for (pid, cd, prec, taken, is_scan, root, folder, batch, ingested,
         triage, deleted) in rows:
        if isinstance(taken, datetime) and taken.tzinfo is not None:
            taken = taken.replace(tzinfo=None)
        out.append(DateRow(
            photo_id=int(pid), capture_date=cd, precision=prec or "unknown",
            exif_taken_at=taken, is_scan=bool(is_scan), source_root=root,
            source_folder=folder, scan_batch=batch, ingested_at=ingested,
            triage_status=triage or "untriaged", is_deleted=bool(deleted),
        ))
    return out


@dataclass(frozen=True)
class Unconfirmed:
    photo_id: int
    previous_date: str
    previous_precision: str
    suggestion_id: int | None
    folder_suggestion_id: int | None
    source: str


def _iso(d: Any) -> str | None:
    if d is None:
        return None
    return d.isoformat() if isinstance(d, (date, datetime)) else str(d)


def _pending_date_suggestion(conn, photo_id: int, d: str, precision: str) -> int | None:
    row = conn.execute(
        """
        select id from suggestions
         where photo_id = %s and kind = 'date' and status = 'pending'
           and payload->>'date' = %s
           and coalesce(payload->>'precision', 'exact') = %s
         order by id limit 1
        """, (photo_id, d, precision),
    ).fetchone()
    return int(row[0]) if row else None


def _insert_suggestion(conn, *, photo_id: int, source: str, confidence: float,
                       payload: dict[str, Any]) -> int:
    return int(conn.execute(
        """
        insert into suggestions (photo_id, kind, source, confidence, payload, status)
        values (%s, 'date', %s, %s, %s::jsonb, 'pending')
        returning id
        """, (photo_id, source, confidence, json.dumps(payload, default=str)),
    ).fetchone()[0])


def folder_month_suggestion(conn, photo_id: int, *, is_scan: bool,
                            source_folder: str | None) -> int | None:
    """The `_YYYY-MM` suggestion ingest would have recorded had it not
    trusted the camera (answer F1) — digital photos only, and only when the
    photo has never had one."""
    if is_scan:
        return None
    hint = parse_date_folder(source_folder or "")
    if hint is None:
        return None
    exists = conn.execute(
        """
        select 1 from suggestions
         where photo_id = %s and kind = 'date'
           and payload->>'evidence' like 'export folder %%'
         limit 1
        """, (photo_id,),
    ).fetchone()
    if exists:
        return None
    year, month = hint
    # Same shape and confidence as ingest's `_record_folder_hints`.
    return _insert_suggestion(conn, photo_id=photo_id, source="import", confidence=0.3, payload={
        "date": f"{year:04d}-{month:02d}-01",
        "precision": "month",
        "evidence": f"export folder _{year:04d}-{month:02d}",
    })


def unconfirm(
    conn: psycopg.Connection,
    photo_id: int,
    *,
    reason: str,
    actor: str = "desktop",
    via: str,
    rule: str | None = None,
    evidence: str | None = None,
    keep_as_suggestion: bool = True,
) -> Unconfirmed | None:
    """Clear one photo's confirmed date, audited. None if it had none.

    The caller owns the transaction. `keep_as_suggestion=False` is the pull
    of a web clear: the web already holds the suggestion, and a web-born
    suggestion stays on the web like every other one.
    """
    row = conn.execute(
        """
        select capture_date, capture_date_precision::text, capture_date_confirmed,
               is_scan, source_folder
          from photos where id = %s for update
        """, (photo_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"photo {photo_id} not found")
    cd, prec, confirmed, is_scan, folder = row
    if not confirmed:
        return None
    prev_date, prev_prec = _iso(cd), (prec or "unknown")
    source = "human" if human_set(conn, photo_id) else "import"

    sid: int | None = None
    if keep_as_suggestion and prev_date is not None:
        sid = _pending_date_suggestion(conn, photo_id, prev_date, prev_prec)
        if sid is None:
            payload: dict[str, Any] = {
                "date": prev_date, "precision": prev_prec,
                "evidence": evidence or ("confirmed date, cleared" if source == "human"
                                         else "EXIF date, cleared"),
                "reason": reason,
            }
            if rule:
                payload["rule"] = rule
            sid = _insert_suggestion(conn, photo_id=photo_id, source=source,
                                     confidence=CLEARED_CONFIDENCE, payload=payload)

    conn.execute(
        """
        update photos
           set capture_date = null,
               capture_date_precision = 'unknown',
               capture_date_confirmed = false
         where id = %s
        """, (photo_id,),
    )
    fsid = folder_month_suggestion(conn, photo_id, is_scan=bool(is_scan), source_folder=folder)
    new_value: dict[str, Any] = {
        "capture_date": None, "precision": "unknown", "confirmed": False,
        "reason": reason, "via": via, "suggestion_id": sid,
    }
    if rule:
        new_value["rule"] = rule
    if fsid is not None:
        new_value["folder_suggestion_id"] = fsid
    dbmod.audit(
        conn, actor=actor, action=ACTION, entity_type="photo", entity_id=photo_id,
        previous_value={"capture_date": prev_date, "precision": prev_prec,
                        "confirmed": True, "source": source},
        new_value=new_value,
    )
    conn.execute("select refresh_completeness(%s)", (photo_id,))
    return Unconfirmed(photo_id=photo_id, previous_date=prev_date or "",
                       previous_precision=prev_prec, suggestion_id=sid,
                       folder_suggestion_id=fsid, source=source)
