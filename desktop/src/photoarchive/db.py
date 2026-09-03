from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg_pool import ConnectionPool

from .config import Settings

log = logging.getLogger(__name__)

_pool: ConnectionPool | None = None


def init_pool(settings: Settings, min_size: int = 1, max_size: int = 4) -> ConnectionPool:
    global _pool
    if _pool is not None:
        return _pool
    _pool = ConnectionPool(
        conninfo=settings.DATABASE_URL,
        min_size=min_size,
        max_size=max_size,
        kwargs={"autocommit": False},
        open=True,
    )
    _pool.wait(timeout=10.0)
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def pool() -> ConnectionPool:
    if _pool is None:
        raise RuntimeError("DB pool not initialised; call init_pool(settings) first")
    return _pool


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    with pool().connection() as conn:
        yield conn


def audit(
    conn: psycopg.Connection,
    *,
    actor: str,
    action: str,
    entity_type: str,
    entity_id: int | None,
    previous_value: Any = None,
    new_value: Any = None,
    user_id: int | None = None,
) -> None:
    """Insert one audit_log row. Caller controls the transaction boundary."""
    conn.execute(
        """
        insert into audit_log
          (user_id, actor, action, entity_type, entity_id, previous_value, new_value)
        values (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            user_id,
            actor,
            action,
            entity_type,
            entity_id,
            _to_jsonb(previous_value),
            _to_jsonb(new_value),
        ),
    )


def _to_jsonb(v: Any) -> str | None:
    if v is None:
        return None
    return json.dumps(v, default=str)


def start_job_run(
    conn: psycopg.Connection,
    *,
    job_name: str,
    params: dict[str, Any],
) -> int:
    row = conn.execute(
        """
        insert into job_runs (job_name, status, params, started_at)
        values (%s, 'running', %s, now())
        returning id
        """,
        (job_name, json.dumps(params, default=str)),
    ).fetchone()
    return row[0]


def finish_job_run(
    conn: psycopg.Connection,
    *,
    job_run_id: int,
    status: str,
    stats: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        update job_runs
        set status = %s,
            finished_at = now(),
            params = params || %s::jsonb
        where id = %s
        """,
        (status, json.dumps({"stats": stats or {}}, default=str), job_run_id),
    )


def record_job_item(
    conn: psycopg.Connection,
    *,
    job_run_id: int,
    photo_id: int,
    status: str,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        insert into job_items (job_run_id, photo_id, status, error, updated_at)
        values (%s, %s, %s, %s, now())
        on conflict (job_run_id, photo_id) do update
          set status = excluded.status,
              error = excluded.error,
              updated_at = now()
        """,
        (job_run_id, photo_id, status, error),
    )


def record_ingest_failure(
    conn: psycopg.Connection,
    *,
    job_run_id: int,
    source_root: str,
    source_folder: str,
    source_filename: str,
    master_path: str,
    error: str,
) -> None:
    conn.execute(
        """
        insert into ingest_failures
          (job_run_id, source_root, source_folder, source_filename, master_path, error)
        values (%s, %s, %s, %s, %s, %s)
        """,
        (job_run_id, source_root, source_folder, source_filename, master_path, error),
    )
