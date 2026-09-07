"""Cheap SQL rollups the Jobs panel refreshes on a timer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ... import db as dbmod
from ...inference_client import QUEUE_ORDER


@dataclass
class JobStats:
    job_name: str
    eligible: int = 0
    done_in_db: int = 0
    cursor: int = 0
    # Filled by the service (may be None if the mini is unreachable).
    inbox_total: int | None = None
    inbox_pending: int | None = None
    processed_on_mini: int | None = None
    eta_seconds: float | None = None
    blackout_active: bool | None = None
    running: bool | None = None
    error: str | None = None


def load_local_stats() -> dict[str, JobStats]:
    """SQL-only rollups. No network I/O. Safe to call every second if we want."""
    out: dict[str, JobStats] = {name: JobStats(job_name=name) for name in QUEUE_ORDER}
    with dbmod.connection() as conn:
        conn.autocommit = True
        # Cursors
        for name, line_no in conn.execute(
            "select job_name, line_no from job_cursors"
        ).fetchall():
            if name in out:
                out[name].cursor = int(line_no)

        # done_in_db per job — count of photo_job_status.status='done' rows
        # for jobs that use it. transcribe_backs doesn't; count differently.
        for name in QUEUE_ORDER:
            if name == "transcribe_backs":
                row = conn.execute(
                    "select count(*) from photo_backs where transcribed_text is not null"
                ).fetchone()
                out[name].done_in_db = int(row[0]) if row else 0
            else:
                row = conn.execute(
                    """
                    select count(*) from photo_job_status
                    where job_name = %s and status = 'done'
                    """,
                    (name,),
                ).fetchone()
                out[name].done_in_db = int(row[0]) if row else 0

        # eligible per job — call the selector's SQL directly here so we
        # don't have to fetch every row.
        out["transcribe_backs"].eligible = int(conn.execute(
            """
            select count(*)
            from photo_backs b
            left join photos p on p.id = b.photo_id
            where b.transcribed_text is null
              and (p.id is null
                   or (p.triage_status in ('keep','private') and not p.is_deleted))
            """
        ).fetchone()[0])

        out["detect_faces"].eligible = int(conn.execute(
            """
            select count(*)
            from photos p
            left join photo_job_status pjs
              on pjs.photo_id = p.id and pjs.job_name = 'detect_faces'
            where p.triage_status in ('keep','private')
              and not p.is_deleted
              and p.working_path is not null
              and (pjs.photo_id is null or pjs.status <> 'done')
            """
        ).fetchone()[0])

        for name in ("classify", "describe", "estimate_date"):
            date_gate = ""
            if name == "estimate_date":
                date_gate = " and not p.capture_date_confirmed"
            out[name].eligible = int(conn.execute(
                f"""
                select count(*)
                from photos p
                left join photo_job_status pjs
                  on pjs.photo_id = p.id and pjs.job_name = %s
                where p.triage_status in ('keep','private')
                  and not p.is_deleted
                  and p.working_path is not null{date_gate}
                  and (pjs.photo_id is null or pjs.status <> 'done')
                """,
                (name,),
            ).fetchone()[0])

    return out


def merge_service_summary(
    stats: dict[str, JobStats],
    job_name: str,
    *,
    inbox_total: int | None = None,
    inbox_pending: int | None = None,
    processed_on_mini: int | None = None,
    eta_seconds: float | None = None,
    blackout_active: bool | None = None,
    running: bool | None = None,
    error: str | None = None,
) -> None:
    s = stats.get(job_name)
    if s is None:
        return
    if inbox_total is not None:
        s.inbox_total = inbox_total
    if inbox_pending is not None:
        s.inbox_pending = inbox_pending
    if processed_on_mini is not None:
        s.processed_on_mini = processed_on_mini
    if eta_seconds is not None:
        s.eta_seconds = eta_seconds
    if blackout_active is not None:
        s.blackout_active = blackout_active
    if running is not None:
        s.running = running
    if error is not None:
        s.error = error
