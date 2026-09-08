"""Framework tests: selector honours model+prompt_version; cursor advances;
sweep is called; collect is idempotent on re-read."""

from __future__ import annotations

from typing import Any, Iterator

import pytest

from photoarchive import db as dbmod
from photoarchive.inference_client import BatchSummary, ResultLine
from photoarchive.jobs.base import (
    JobContext,
    collect,
    has_handover,
    has_successful_handover,
    reconcile_handovers,
)
from photoarchive.jobs.describe import make_job as make_describe

from .phase6_fixtures import insert_photo, phase6  # noqa: F401


class FakeClient:
    """Just enough of the InferenceClient surface for the collect loop."""

    def __init__(self, lines: list[ResultLine]) -> None:
        self._lines = lines
        self.sweep_calls: list[tuple[str, bool]] = []

    def results_after(self, job_name: str, after: int) -> Iterator[ResultLine]:
        for line in self._lines:
            if line.line_no > after:
                yield line

    def sweep(self, job_name: str, *, done_only: bool = True) -> int:
        self.sweep_calls.append((job_name, done_only))
        return 0

    # Unused surface — the collect path doesn't touch these.
    def upload(self, *a, **kw): raise NotImplementedError
    def health(self): raise NotImplementedError
    def call_endpoint(self, *a, **kw): raise NotImplementedError
    def inbox(self, *a, **kw): raise NotImplementedError
    def start_from_inbox(self, *a, **kw): raise NotImplementedError
    def summary(self, *a, **kw): raise NotImplementedError
    def cancel(self, *a, **kw): raise NotImplementedError


def _line(ref: str, result: dict[str, Any], *, ok: bool = True, model: str = "m",
          prompt_version: str = "v1", line_no: int = 1) -> ResultLine:
    return ResultLine(
        line_no=line_no, ref=ref, ok=ok, model=model,
        prompt_version=prompt_version, elapsed_ms=100,
        result=result, error=None, raw={"ref": ref, "result": result},
    )


def test_collect_advances_cursor_and_calls_sweep(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        p1 = insert_photo(conn)
        p2 = insert_photo(conn)
        conn.commit()

    lines = [
        _line(str(p1), {"text": "a", "tags": []}, line_no=1),
        _line(str(p2), {"text": "b", "tags": []}, line_no=2),
    ]
    client = FakeClient(lines)
    ctx = JobContext(client=client, model="m", prompt_version="v1")
    job = make_describe()

    summary = collect(job, ctx)
    assert summary.written == 2
    assert summary.cursor_before == 0
    assert summary.cursor_after == 2
    assert client.sweep_calls == [("describe", True)]

    with dbmod.connection() as conn:
        conn.autocommit = True
        cursor = conn.execute(
            "select line_no from job_cursors where job_name = 'describe'"
        ).fetchone()[0]
        assert cursor == 2
        # photo_job_status marked done for both.
        done = conn.execute(
            "select photo_id from photo_job_status where job_name='describe' and status='done'"
        ).fetchall()
        assert {r[0] for r in done} == {p1, p2}


def test_collect_replay_is_idempotent(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = False
        p1 = insert_photo(conn)
        conn.commit()

    line = _line(str(p1), {"text": "one", "tags": []}, line_no=1)
    client = FakeClient([line])
    ctx = JobContext(client=client, model="m", prompt_version="v1")
    job = make_describe()

    # First pass: writes one suggestion.
    collect(job, ctx)

    # Manually rewind the cursor and collect again — same line, must not
    # duplicate the suggestion.
    with dbmod.connection() as conn:
        conn.autocommit = True
        conn.execute("update job_cursors set line_no = 0 where job_name='describe'")

    collect(job, ctx)

    with dbmod.connection() as conn:
        conn.autocommit = True
        n = conn.execute(
            "select count(*) from suggestions where photo_id = %s and kind='description'",
            (p1,),
        ).fetchone()[0]
        assert n == 1


# ---- fix-up 1 --------------------------------------------------------------


def _summary(job_name: str, *, total: int = 0, done: int = 0,
              running: bool = False) -> BatchSummary:
    return BatchSummary(
        job_name=job_name, total=total, done=done, failed=0,
        pending=max(total - done, 0), eta_seconds=None, running=running,
        blackout_active=False, raw={},
    )


class SummaryOnlyClient:
    """Fake InferenceClient with a preset per-job summary map."""

    def __init__(self, summaries: dict[str, BatchSummary],
                  raises: dict[str, Exception] | None = None) -> None:
        self._summaries = summaries
        self._raises = raises or {}

    def summary(self, job_name: str) -> BatchSummary:
        if job_name in self._raises:
            raise self._raises[job_name]
        return self._summaries.get(job_name, _summary(job_name))


def test_reconcile_promotes_failed_row_when_mini_has_data(phase6):
    """The old start_from_inbox timeout wrote status='failed' even though
    the mini had accepted the batch. Reconciliation upgrades it."""
    with dbmod.connection() as conn:
        conn.autocommit = True
        # Simulate the pre-fix state: a failed row exists locally.
        run_id = dbmod.start_job_run(
            conn, job_name="classify",
            params={"phase": "hand_over"},
        )
        dbmod.finish_job_run(
            conn, job_run_id=run_id, status="failed",
            stats={"error": "Read timed out"},
        )
    assert has_handover("classify") is True
    assert has_successful_handover("classify") is False

    client = SummaryOnlyClient({
        "classify": _summary("classify", total=500, done=42, running=True),
    })
    out = reconcile_handovers(client, job_names=["classify"])
    assert out["classify"] == "promoted"
    assert has_successful_handover("classify") is True


def test_reconcile_no_op_when_local_row_already_handed_over(phase6):
    with dbmod.connection() as conn:
        conn.autocommit = True
        run_id = dbmod.start_job_run(
            conn, job_name="describe", params={"phase": "hand_over"},
        )
        dbmod.finish_job_run(conn, job_run_id=run_id, status="handed_over",
                              stats={})

    client = SummaryOnlyClient({
        "describe": _summary("describe", total=100, done=0, running=True),
    })
    out = reconcile_handovers(client, job_names=["describe"])
    assert out["describe"] == "ok"
    # Only one row for the job — no duplicate insert.
    with dbmod.connection() as conn:
        conn.autocommit = True
        n = conn.execute(
            "select count(*) from job_runs where job_name = 'describe'"
        ).fetchone()[0]
        assert n == 1


def test_reconcile_marks_no_data_when_mini_is_empty(phase6):
    client = SummaryOnlyClient({
        "classify": _summary("classify", total=0, running=False),
    })
    out = reconcile_handovers(client, job_names=["classify"])
    assert out["classify"] == "no_data"


def test_reconcile_marks_unreachable_when_summary_raises(phase6):
    client = SummaryOnlyClient(
        summaries={}, raises={"describe": RuntimeError("service down")},
    )
    out = reconcile_handovers(client, job_names=["describe"])
    assert out["describe"] == "unreachable"


def test_has_handover_true_after_hand_over_row_written(phase6):
    assert has_handover("classify") is False
    with dbmod.connection() as conn:
        conn.autocommit = True
        dbmod.start_job_run(conn, job_name="classify", params={})
    assert has_handover("classify") is True


def test_selector_excludes_done_at_current_model_and_prompt_version(phase6):
    from photoarchive.jobs.describe import DescribeSelector

    with dbmod.connection() as conn:
        conn.autocommit = False
        wp = str(phase6.WORKING_DIR / "sel-test.jpg")
        p_done = insert_photo(conn, working_path=wp + ".1")
        p_pending = insert_photo(conn, working_path=wp + ".2")
        p_stale_model = insert_photo(conn, working_path=wp + ".3")
        p_stale_prompt = insert_photo(conn, working_path=wp + ".4")
        conn.execute(
            """
            insert into photo_job_status
              (photo_id, job_name, model, prompt_version, status, completed_at)
            values (%s, 'describe', 'm', 'v1', 'done', now())
            """,
            (p_done,),
        )
        conn.execute(
            """
            insert into photo_job_status
              (photo_id, job_name, model, prompt_version, status, completed_at)
            values (%s, 'describe', 'm-old', 'v1', 'done', now())
            """,
            (p_stale_model,),
        )
        conn.execute(
            """
            insert into photo_job_status
              (photo_id, job_name, model, prompt_version, status, completed_at)
            values (%s, 'describe', 'm', 'v0', 'done', now())
            """,
            (p_stale_prompt,),
        )
        conn.commit()

    with dbmod.connection() as conn:
        conn.autocommit = True
        items = DescribeSelector().select(conn, model="m", prompt_version="v1")
    ids = {int(it.ref) for it in items}
    assert p_pending in ids
    assert p_stale_model in ids
    assert p_stale_prompt in ids
    assert p_done not in ids
