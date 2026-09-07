"""estimate_date — visual date range estimate.

Selector: keep/private, not deleted, no confirmed date, photo_job_status for
estimate_date not done at (model, prompt_version).

Writer: `suggestions` kind='date' payload
  {"range":{"year_min":Y1,"year_max":Y2},
   "date": "<Y1>-01-01",
   "precision": "decade" if (Y2-Y1)>=10 else "year",
   "evidence": <reasoning>,
   "prompt_version": ...}
confidence = service confidence × 0.5 (visual dating is coarse — don't
crowd out handwritten-back dates in the admin queue).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import psycopg

from ..inference_client import ENDPOINT_ESTIMATE_DATE, RefImage, ResultLine
from .base import Job, JobContext, SelectedItem, Selector, Uploader, Writer

log = logging.getLogger(__name__)


class EstimateDateSelector(Selector):
    def select(
        self,
        conn: psycopg.Connection,
        *,
        model: str,
        prompt_version: str | None,
        limit: int | None = None,
    ) -> list[SelectedItem]:
        rows = conn.execute(
            """
            select p.id, p.working_path
            from photos p
            left join photo_job_status pjs
              on pjs.photo_id = p.id and pjs.job_name = 'estimate_date'
            where p.triage_status in ('keep','private')
              and not p.is_deleted
              and not p.capture_date_confirmed
              and p.working_path is not null
              and (
                pjs.photo_id is null
                or pjs.status <> 'done'
                or coalesce(pjs.model, '') <> %s
                or coalesce(pjs.prompt_version, '') <> %s
              )
            order by p.id
            """
            + ("" if limit is None else " limit %s"),
            (model, prompt_version or "") if limit is None else (model, prompt_version or "", limit),
        ).fetchall()
        return [
            SelectedItem(ref=str(pid), photo_id=pid, working_path=wp)
            for pid, wp in rows
        ]


class EstimateDateUploader(Uploader):
    def prepare(self, item: SelectedItem, max_edge: int) -> RefImage:
        return RefImage(ref=item.ref, path=Path(item.working_path))


class EstimateDateWriter(Writer):
    def apply(
        self,
        conn: psycopg.Connection,
        line: ResultLine,
        ctx: JobContext,
    ) -> str:
        photo_id = _photo_id(line.ref)
        if photo_id is None:
            return "skipped_bad_ref"
        if not line.ok:
            return "error"

        result = line.result or {}
        y_min = _as_int(result.get("year_min"))
        y_max = _as_int(result.get("year_max"))
        conf_in = _as_float(result.get("confidence")) or 0.0
        reasoning = str(result.get("reasoning") or "")
        model = line.model or ctx.model or ""
        prompt_version = line.prompt_version or ctx.prompt_version or ""

        if y_min is None or y_max is None:
            return "error"
        if y_min > y_max:
            y_min, y_max = y_max, y_min

        precision = "decade" if (y_max - y_min) >= 10 else "year"
        # Pin the ISO date to the first day of the range so the admin's
        # promote-to-fact path (which copies payload.date into
        # photos.capture_date) has something to write.
        iso_date = f"{y_min:04d}-01-01"
        confidence = conf_in * 0.5

        exists = conn.execute(
            """
            select 1 from suggestions
            where kind = 'date'
              and source = 'ai'
              and photo_id = %s
              and coalesce(model, '') = %s
              and coalesce(payload ->> 'prompt_version', '') = %s
            limit 1
            """,
            (photo_id, model, prompt_version),
        ).fetchone()
        if exists:
            return "skipped_duplicate"

        payload = {
            "date": iso_date,
            "precision": precision,
            "range": {"year_min": y_min, "year_max": y_max},
            "evidence": reasoning,
            "prompt_version": prompt_version,
        }
        conn.execute(
            """
            insert into suggestions (photo_id, kind, payload, confidence, source, model)
            values (%s, 'date', %s, %s, 'ai', %s)
            """,
            (photo_id, json.dumps(payload), confidence, model),
        )
        return "ok"


def _photo_id(ref: str) -> int | None:
    try:
        return int(ref)
    except (TypeError, ValueError):
        return None


def _as_int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _as_float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def make_job() -> Job:
    return Job(
        name="estimate_date",
        endpoint=ENDPOINT_ESTIMATE_DATE,
        selector=EstimateDateSelector(),
        uploader=EstimateDateUploader(),
        writer=EstimateDateWriter(),
    )
