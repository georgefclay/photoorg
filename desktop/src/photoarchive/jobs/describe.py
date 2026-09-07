"""describe — one factual sentence + tags per photo.

Selector: keep/private, not deleted, photo_job_status for describe not done
at (model, prompt_version).

Writer: `suggestions` kind='description' payload {text, tags, prompt_version}.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import psycopg

from ..inference_client import ENDPOINT_DESCRIBE, RefImage, ResultLine
from .base import Job, JobContext, SelectedItem, Selector, Uploader, Writer

log = logging.getLogger(__name__)


class DescribeSelector(Selector):
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
              on pjs.photo_id = p.id and pjs.job_name = 'describe'
            where p.triage_status in ('keep','private')
              and not p.is_deleted
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


class DescribeUploader(Uploader):
    def prepare(self, item: SelectedItem, max_edge: int) -> RefImage:
        return RefImage(ref=item.ref, path=Path(item.working_path))


class DescribeWriter(Writer):
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
        text = str(result.get("text") or "").strip()
        tags = list(result.get("tags") or [])
        model = line.model or ctx.model or ""
        prompt_version = line.prompt_version or ctx.prompt_version or ""

        exists = conn.execute(
            """
            select 1 from suggestions
            where kind = 'description'
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

        payload = {"text": text, "tags": tags, "prompt_version": prompt_version}
        conn.execute(
            """
            insert into suggestions (photo_id, kind, payload, source, model)
            values (%s, 'description', %s, 'ai', %s)
            """,
            (photo_id, json.dumps(payload), model),
        )
        return "ok"


def _photo_id(ref: str) -> int | None:
    try:
        return int(ref)
    except (TypeError, ValueError):
        return None


def make_job() -> Job:
    return Job(
        name="describe",
        endpoint=ENDPOINT_DESCRIBE,
        selector=DescribeSelector(),
        uploader=DescribeUploader(),
        writer=DescribeWriter(),
    )
