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

import dataclasses
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

import psycopg
from PIL import UnidentifiedImageError

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
from ..config import load as load_settings
from ..modes.ingest.paths import resolve_working_path

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
    """Turns a SelectedItem into a RefImage.

    The default `prepare` is the only path construction any job may use
    (fix-up 11): the stored `working_path` goes through the ONE resolver
    in `modes.ingest.paths`, so an absolute row is used as-is and a bare
    row is joined onto WORKING_DIR. No job builds a path from the naming
    scheme on its own. Subclasses only override `prepare` when they need
    pre-computed bytes (none do today)."""

    _working_dir: Path | None = None

    def working_dir(self) -> Path:
        if self._working_dir is None:
            self._working_dir = Path(load_settings().WORKING_DIR)
        return self._working_dir

    def prepare(self, item: SelectedItem, max_edge: int) -> RefImage:
        path = resolve_working_path(self.working_dir(), item.working_path)
        if path is None:
            raise FileNotFoundError(f"{item.ref}: no working_path stored")
        return RefImage(ref=item.ref, path=path)


class WorkingFileUploader(Uploader):
    """Concrete uploader every photo/back job uses."""


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
    skipped: int = 0                      # items whose file was missing / undecodable
    skipped_refs: list[str] = field(default_factory=list)

    def report(self) -> str:
        return f"{self.uploaded} uploaded, {self.skipped} skipped (missing file)"


# Errors that mean "this one file is bad" rather than "the hand-over is
# broken". Anything else still aborts the hand-over.
_PER_ITEM_ERRORS = (FileNotFoundError, UnidentifiedImageError, OSError, ValueError)


class _NothingToStart(Exception):
    """Internal: every selected item was skipped, so there is no inbox to start."""


def _prepare_item(job: "Job", item: SelectedItem, max_edge: int) -> RefImage:
    """Resolve + read + downscale one item. Raises one of _PER_ITEM_ERRORS
    when the file is missing or undecodable."""
    ref = job.uploader.prepare(item, max_edge)
    if ref.prepared_bytes is not None:
        return ref
    return dataclasses.replace(ref, prepared_bytes=prepare_jpeg(ref.path, max_edge))


def _record_skip(job_run_id: int, item: SelectedItem, error: str) -> None:
    """job_items needs a photo_id; back items (photo_id None) are logged only."""
    if item.photo_id is None:
        return
    with dbmod.connection() as conn:
        conn.autocommit = True
        dbmod.record_job_item(
            conn, job_run_id=job_run_id, photo_id=item.photo_id,
            status="failed", error=error[:500],
        )


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
    skipped_refs: list[str] = []
    error: str | None = None
    started = False
    cancelled = False
    try:
        for chunk_index, chunk in enumerate(_chunks(items, ctx.chunk_size), start=1):
            if ctok.is_set():
                raise Cancelled()
            # Fix-up 11: a hand-over must never abort on one bad file. Each
            # item is resolved + read here; a missing / undecodable file
            # becomes a failed job_items row and the chunk carries on.
            refs: list[RefImage] = []
            for it in chunk:
                try:
                    refs.append(_prepare_item(job, it, max_edge))
                except _PER_ITEM_ERRORS as e:
                    msg = f"{type(e).__name__}: {e}"
                    log.warning("hand_over: %s skipping %s: %s", job.name, it.ref, msg)
                    skipped_refs.append(it.ref)
                    _record_skip(job_run_id, it, msg)
            rejected = 0
            if refs:
                result = ctx.client.upload(job.name, refs, endpoint_edge=max_edge)
                uploaded += result.accepted
                rejected = len(result.rejected)
            progress_cb({
                "kind": "uploaded_chunk",
                "job_name": job.name,
                "chunk": chunk_index,
                "uploaded": uploaded,
                "skipped": len(skipped_refs),
                "total": total,
                "rejected": rejected,
            })
        if uploaded == 0 and skipped_refs:
            # Every file was missing: nothing to start on the mini.
            raise _NothingToStart()
        # Kick off processing.
        start = ctx.client.start_from_inbox(job.name, job.endpoint)
        started = start.accepted
        progress_cb({
            "kind": "started", "job_name": job.name,
            "started": started, "already_running": start.already_running,
        })
    except Cancelled:
        cancelled = True
        error = "cancelled"
        raise
    except _NothingToStart:
        started = False
        progress_cb({"kind": "started", "job_name": job.name, "started": False,
                     "already_running": False, "reason": "all items skipped"})
    except Exception as e:
        log.exception("hand_over: %s failed", job.name)
        error = str(e)
        # Fix-up 1: `start_from_inbox` used to timeout while the mini had
        # already started the job. If the mini's /summary now says there's
        # data for this job, treat it as handed over.
        try:
            summary = ctx.client.summary(job.name)
            if (summary.total or 0) > 0 or summary.running:
                log.info(
                    "hand_over: %s start_from_inbox raised %r, but mini "
                    "/summary reports total=%s running=%s — reconciling as handed_over",
                    job.name, e, summary.total, summary.running,
                )
                error = None
                started = True
        except Exception as reconcile_err:
            log.debug("hand_over: reconcile failed for %s: %s", job.name, reconcile_err)
        if error is not None:
            raise
    finally:
        with dbmod.connection() as conn:
            conn.autocommit = True
            dbmod.finish_job_run(
                conn, job_run_id=job_run_id,
                status="cancelled" if cancelled else ("failed" if error else "handed_over"),
                stats={
                    "selected": total, "uploaded": uploaded, "started": started,
                    "error": error,
                    "skipped": len(skipped_refs), "skipped_refs": skipped_refs,
                },
            )

    progress_cb({"kind": "done", "job_name": job.name, "uploaded": uploaded,
                 "skipped": len(skipped_refs), "total": total})
    return HandoverSummary(
        job_name=job.name, selected=total, uploaded=uploaded,
        started=started, job_run_id=job_run_id, error=error,
        skipped=len(skipped_refs), skipped_refs=skipped_refs,
    )


def last_skipped(job_name: str | None = None) -> dict[str, list[str]]:
    """{job_name: skipped_refs} from the most recent hand-over row of each
    job that skipped anything. The Jobs panel's "Skipped..." button reads it."""
    with dbmod.connection() as conn:
        conn.autocommit = True
        rows = conn.execute(
            """
            select distinct on (job_name) job_name,
                   params -> 'stats' -> 'skipped_refs' as refs
            from job_runs
            where coalesce((params -> 'stats' ->> 'skipped')::int, 0) > 0
              and (%s::text is null or job_name = %s)
            order by job_name, id desc
            """,
            (job_name, job_name),
        ).fetchall()
    return {name: [str(r) for r in (refs or [])] for name, refs in rows}


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


def has_handover(job_name: str) -> bool:
    """True if this job has any hand-over row in job_runs (any status). The
    auto-collect loop uses this to skip jobs that were never handed over
    (fix-up 1)."""
    with dbmod.connection() as conn:
        conn.autocommit = True
        row = conn.execute(
            "select 1 from job_runs where job_name = %s limit 1", (job_name,)
        ).fetchone()
    return row is not None


def has_successful_handover(job_name: str) -> bool:
    with dbmod.connection() as conn:
        conn.autocommit = True
        row = conn.execute(
            """
            select 1 from job_runs
            where job_name = %s and status = 'handed_over'
            limit 1
            """,
            (job_name,),
        ).fetchone()
    return row is not None


def reconcile_handovers(
    client,
    *,
    job_names: list[str] | None = None,
) -> dict[str, str]:
    """Fix-up 1: `POST /batch/{endpoint}` used to time out reading the
    streaming body — the mini had started the job but the client wrote
    `status='failed'` to job_runs. The mini's `/summary` is the source of
    truth for "handed over": if it reports items for a job whose local
    row is failed, upgrade the row.

    Also promotes jobs the mini is running that we never wrote a row for
    at all (should not happen with the streaming fix, but is defensive).

    Returns {job_name: outcome} where outcome is one of 'promoted',
    'ok' (already handed_over locally), 'no_data' (mini has nothing),
    'unreachable' (couldn't ask the mini), or 'skipped'.
    """
    from ..inference_client import QUEUE_ORDER
    names = job_names or list(QUEUE_ORDER)
    out: dict[str, str] = {}
    for name in names:
        try:
            summary = client.summary(name)
        except Exception as e:
            log.debug("reconcile: /summary(%s) unreachable: %s", name, e)
            out[name] = "unreachable"
            continue
        mini_has_data = (summary.total or 0) > 0 or summary.running
        if not mini_has_data:
            out[name] = "no_data"
            continue
        if has_successful_handover(name):
            out[name] = "ok"
            continue
        # Mini has data but no local handed_over row — write a synthetic one.
        with dbmod.connection() as conn:
            conn.autocommit = True
            dbmod.start_job_run(
                conn, job_name=name,
                params={"phase": "reconciled_from_summary",
                        "mini_total": summary.total,
                        "mini_done": summary.done,
                        "mini_running": summary.running},
            )
            row = conn.execute(
                "select id from job_runs where job_name = %s order by id desc limit 1",
                (name,),
            ).fetchone()
            if row is not None:
                dbmod.finish_job_run(
                    conn, job_run_id=int(row[0]),
                    status="handed_over",
                    stats={"source": "reconcile_handovers"},
                )
        out[name] = "promoted"
    return out


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
