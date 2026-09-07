"""Batch runner framework — one shape, five jobs.

Each job has three collaborators:
  Selector — SQL query picking the photos/backs that still need this job at
             the current (model, prompt_version).
  Uploader — turns a selected item into a RefImage for /batch/upload/{job}.
  Writer   — applies one ResultLine to the DB, idempotently.

The framework owns the hand-over loop (chunks of 50 with progress, job_runs
row) and the collect loop (cursor advance in job_cursors, sweep with
?done=true, photo_job_status update). All five jobs share it.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator

import psycopg

from ..inference_client import (
    ENDPOINT_MAX_EDGE,
    InferenceClient,
    JOB_TO_ENDPOINT,
    RefImage,
    ResultLine,
)
from ..inference_client.image_prep import prepare_jpeg
from ..workers import CancelToken, Cancelled
from .. import db as dbmod

log = logging.getLogger(__name__)


ProgressCb = Callable[[dict[str, Any]], None]

DEFAULT_UPLOAD_CHUNK = 50


# --- collaborators ---------------------------------------------------------


@dataclass
class SelectedItem:
    """One item to hand over. `ref` is opaque to the framework; jobs use it
    to key writes (usually `str(photo_id)`, or `b<id>[_f|_r]` for backs)."""

    ref: str
    photo_id: int | None  # None for orphan backs and mid-job retry variants
    working_path: str


class Selector(ABC):
    @abstractmethod
    def select(
        self,
        conn: psycopg.Connection,
        *,
        model: str,
        prompt_version: str | None,
        limit: int | None = None,
    ) -> list[SelectedItem]: ...


class Uploader(ABC):
    @abstractmethod
    def prepare(self, item: SelectedItem, max_edge: int) -> RefImage: ...


class Writer(ABC):
    @abstractmethod
    def apply(
        self,
        conn: psycopg.Connection,
        line: ResultLine,
        ctx: "JobContext",
    ) -> str:
        """Apply one result. Return a short status code the framework can
        record ('ok'|'error'|'skipped_duplicate'|'skipped_variant'|...).
        Must be idempotent — re-applying the same line must not insert.
        `ctx.client` is available for the rare inline HTTP call
        (transcribe_backs' low-confidence retry)."""


# --- job registration ------------------------------------------------------


@dataclass
class Job:
    name: str                 # canonical job name (matches photo_job_status.job_name)
    endpoint: str             # service slug, e.g. "transcribe-back"
    selector: Selector
    uploader: Uploader
    writer: Writer
    updates_photo_job_status: bool = True
    # Some jobs (transcribe_backs) address photo_backs rows rather than
    # photos rows and can't fill photo_job_status without a photo_id. If a
    # ref refers to a variant retry (b<id>_f), the framework skips
    # photo_job_status updates for it and lets the writer choose.


# --- context passed into workers ------------------------------------------


@dataclass
class JobContext:
    client: InferenceClient
    model: str
    prompt_version: str | None
    chunk_size: int = DEFAULT_UPLOAD_CHUNK
    cancel: CancelToken | None = None


# --- hand-over ------------------------------------------------------------


@dataclass
class HandoverProgress:
    kind: str  # 'selected' | 'uploaded_chunk' | 'started' | 'done'
    job_name: str
    total: int = 0
    uploaded: int = 0
    chunk: int = 0
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class HandoverSummary:
    job_name: str
    selected: int
    uploaded: int
    started: bool
    job_run_id: int | None
    error: str | None = None


def hand_over(
    job: Job,
    ctx: JobContext,
    *,
    progress_cb: ProgressCb = lambda _: None,
) -> HandoverSummary:
    """Select eligible items, upload in chunks, then POST /batch/{endpoint}
    with from_inbox. Records the hand-over in job_runs. Idempotent w.r.t.
    re-uploading — the service overwrites refs (see inference README)."""
    ctok = ctx.cancel or CancelToken()

    with dbmod.connection() as conn:
        conn.autocommit = True
        items = job.selector.select(
            conn,
            model=ctx.model,
            prompt_version=ctx.prompt_version,
        )
    total = len(items)
    progress_cb({"kind": "selected", "job_name": job.name, "total": total})

    if not items:
        return HandoverSummary(
            job_name=job.name, selected=0, uploaded=0, started=False, job_run_id=None
        )

    with dbmod.connection() as conn:
        conn.autocommit = True
        job_run_id = dbmod.start_job_run(
            conn,
            job_name=job.name,
            params={
                "phase": "hand_over",
                "model": ctx.model,
                "prompt_version": ctx.prompt_version,
                "total": total,
            },
        )

    max_edge = ENDPOINT_MAX_EDGE[job.endpoint]
    uploaded = 0
    error: str | None = None
    started = False
    try:
        for chunk_index, chunk in enumerate(_chunks(items, ctx.chunk_size), start=1):
            if ctok.is_set():
                raise Cancelled()
            refs = [job.uploader.prepare(it, max_edge) for it in chunk]
            result = ctx.client.upload(job.name, refs, endpoint_edge=max_edge)
            uploaded += result.accepted
            progress_cb({
                "kind": "uploaded_chunk",
                "job_name": job.name,
                "chunk": chunk_index,
                "uploaded": uploaded,
                "total": total,
                "rejected": len(result.rejected),
            })
        # Kick off processing.
        start = ctx.client.start_from_inbox(job.name, job.endpoint)
        started = start.accepted
        progress_cb({
            "kind": "started", "job_name": job.name,
            "started": started, "already_running": start.already_running,
        })
    except Cancelled:
        error = "cancelled"
        raise
    except Exception as e:
        log.exception("hand_over: %s failed", job.name)
        error = str(e)
        raise
    finally:
        with dbmod.connection() as conn:
            conn.autocommit = True
            dbmod.finish_job_run(
                conn, job_run_id=job_run_id,
                status="failed" if error else "handed_over",
                stats={"selected": total, "uploaded": uploaded, "started": started, "error": error},
            )

    progress_cb({"kind": "done", "job_name": job.name, "uploaded": uploaded, "total": total})
    return HandoverSummary(
        job_name=job.name, selected=total, uploaded=uploaded,
        started=started, job_run_id=job_run_id, error=error,
    )


# --- collect --------------------------------------------------------------


@dataclass
class CollectProgress:
    kind: str  # 'started' | 'wrote_line' | 'swept' | 'done'
    job_name: str
    written: int = 0
    line_no: int = 0
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class CollectSummary:
    job_name: str
    written: int
    skipped: int
    failed: int
    cursor_before: int
    cursor_after: int
    swept: int


def collect(
    job: Job,
    ctx: JobContext,
    *,
    progress_cb: ProgressCb = lambda _: None,
    sweep_when_done: bool = True,
) -> CollectSummary:
    """Read new NDJSON lines from `after=<cursor>`, apply the writer per
    line inside its own transaction (idempotent, so a re-read is safe),
    advance the cursor, update photo_job_status, and finally sweep the
    inbox with ?done=true."""
    ctok = ctx.cancel or CancelToken()
    cursor_before = _load_cursor(job.name)
    progress_cb({"kind": "started", "job_name": job.name, "cursor": cursor_before})

    written = 0
    skipped = 0
    failed = 0
    cursor_after = cursor_before

    for line in ctx.client.results_after(job.name, cursor_before):
        if ctok.is_set():
            raise Cancelled()
        try:
            with dbmod.connection() as conn:
                conn.autocommit = False
                try:
                    status = job.writer.apply(conn, line, ctx)
                    _advance_cursor(conn, job.name, line.line_no)
                    if job.updates_photo_job_status:
                        _update_photo_job_status(
                            conn,
                            job_name=job.name,
                            ref=line.ref,
                            model=line.model or ctx.model,
                            prompt_version=line.prompt_version or ctx.prompt_version,
                            ok=line.ok and status.startswith("ok"),
                            error=line.error if not line.ok else None,
                        )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            if status.startswith("ok"):
                written += 1
            elif status.startswith("skipped"):
                skipped += 1
            else:
                failed += 1
            cursor_after = line.line_no
            progress_cb({
                "kind": "wrote_line", "job_name": job.name,
                "written": written, "line_no": line.line_no, "status": status,
            })
        except Exception as e:
            log.exception("collect: writer failed for %s line %d ref=%s: %s",
                          job.name, line.line_no, line.ref, e)
            failed += 1
            # Do NOT advance the cursor. Next collect retries this line.
            break

    swept = 0
    if sweep_when_done and cursor_after > cursor_before:
        try:
            swept = ctx.client.sweep(job.name, done_only=True)
        except Exception as e:
            log.warning("collect: sweep failed for %s: %s", job.name, e)
        progress_cb({"kind": "swept", "job_name": job.name, "swept": swept})

    progress_cb({"kind": "done", "job_name": job.name,
                 "written": written, "skipped": skipped, "failed": failed})
    return CollectSummary(
        job_name=job.name, written=written, skipped=skipped, failed=failed,
        cursor_before=cursor_before, cursor_after=cursor_after, swept=swept,
    )


# --- cursor / status helpers ----------------------------------------------


def _load_cursor(job_name: str) -> int:
    with dbmod.connection() as conn:
        conn.autocommit = True
        row = conn.execute(
            "select line_no from job_cursors where job_name = %s", (job_name,)
        ).fetchone()
    return int(row[0]) if row else 0


def _advance_cursor(conn: psycopg.Connection, job_name: str, line_no: int) -> None:
    conn.execute(
        """
        insert into job_cursors (job_name, line_no)
        values (%s, %s)
        on conflict (job_name) do update
          set line_no = greatest(job_cursors.line_no, excluded.line_no)
        """,
        (job_name, line_no),
    )


def _update_photo_job_status(
    conn: psycopg.Connection,
    *,
    job_name: str,
    ref: str,
    model: str,
    prompt_version: str | None,
    ok: bool,
    error: str | None,
) -> None:
    """Upsert (photo_id, job_name). `ref` is expected to be a numeric photo_id
    or `b<id>[_f|_r]` for backs — back refs are ignored here (transcribe_backs
    updates its own state on photo_backs)."""
    photo_id = _photo_id_from_ref(ref)
    if photo_id is None:
        return
    status = "done" if ok else ("error" if error else "failed")
    conn.execute(
        """
        insert into photo_job_status
          (photo_id, job_name, model, prompt_version, status, completed_at)
        values (%s, %s, %s, %s, %s, case when %s then now() else null end)
        on conflict (photo_id, job_name) do update
          set model = excluded.model,
              prompt_version = excluded.prompt_version,
              status = excluded.status,
              completed_at = excluded.completed_at
        """,
        (photo_id, job_name, model, prompt_version, status, ok),
    )


def _photo_id_from_ref(ref: str) -> int | None:
    if not ref:
        return None
    if ref.startswith("b"):
        # back refs are keyed on photo_back id, not photo id
        return None
    try:
        return int(ref)
    except ValueError:
        return None


def _chunks(items: list[SelectedItem], size: int) -> Iterator[list[SelectedItem]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]
