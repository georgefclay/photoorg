"""Phase 9 fix-up 2 — one refused table must not cost every table after it.

The metadata stages run in a fixed order (… photo_masters → people → …
→ albums → … → suggestions → photo_groups). A stage that raised used to
abort the whole push, so when the VM was missing the Phase 7 migration
and `/sync/photo_masters` answered 500 over `region`, the three stages
George actually cared about — `albums`, `suggestions`, `photo_groups` —
never ran, and a corrected album name could not reach the VM no matter
how many times he pushed.

These tests need no database: `_push_meta_stage` is driven through a
stub connection, which is the whole point — the ordering rule is about
the loop, not about SQL.
"""

from __future__ import annotations

import pytest

from photoarchive.modes.sync.client import WebSyncError
from photoarchive.modes.sync.push import (
    PushStats,
    PushStageError,
    _push_meta_stage,
    _stage_guard,
)


class _StubCursor:
    """Returns one row for any query; records nothing else."""

    description = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._sql = sql

    def fetchall(self):
        return [{"id": 1}]


class _StubConn:
    def cursor(self, *a, **kw):
        return _StubCursor()


class _Client:
    """Fails for the named stages, succeeds for everything else."""

    def __init__(self, failing: set[str]):
        self.failing = failing
        self.seen: list[str] = []

    def push_batch(self, stage, chunk):
        self.seen.append(stage)
        if stage in self.failing:
            raise WebSyncError(f"{stage}: HTTP 500 <!doctype html> something went wrong")
        return {"upserted": len(chunk)}


def _marshal(row):
    return {"id": row["id"]}


def test_a_failed_stage_is_recorded_and_returns_zero():
    stats = PushStats()
    client = _Client({"photo_masters"})
    n = _push_meta_stage(
        _StubConn(), client, "photo_masters", "select 1", _marshal,
        None, None, stats=stats,
    )
    assert n == 0
    assert "photo_masters" in stats.failed_stages
    # The HTML error page is flattened to one line and truncated, so a 500
    # from Caddy cannot flood the log or the Jobs panel.
    msg = stats.failed_stages["photo_masters"]
    assert "\n" not in msg and len(msg) <= 300
    assert "HTTP 500" in msg


def test_later_stages_still_run_after_an_early_failure():
    """The regression that motivated this: photo_masters 500s, albums and
    suggestions must still be pushed."""
    stats = PushStats()
    client = _Client({"photo_masters"})
    order = ["photo_masters", "people", "albums", "suggestions", "photo_groups"]
    for stage in order:
        stats.tables[stage] = _push_meta_stage(
            _StubConn(), client, stage, "select 1", _marshal, None, None, stats=stats,
        )

    assert client.seen == order, "every stage must still be attempted"
    assert stats.tables["albums"] == 1
    assert stats.tables["suggestions"] == 1
    assert stats.tables["photo_masters"] == 0
    assert list(stats.failed_stages) == ["photo_masters"]


def test_without_stats_the_error_still_propagates():
    """Callers that want the old all-or-nothing behaviour (and the direct
    `client.push_batch` tests) are unchanged."""
    with pytest.raises(WebSyncError, match="HTTP 500"):
        _push_meta_stage(
            _StubConn(), _Client({"albums"}), "albums", "select 1", _marshal,
            None, None,
        )


def test_stage_guard_does_not_swallow_a_non_sync_error():
    """A bug in the marshaller is not a web refusal and must not be filed
    as one — only WebSyncError is caught."""
    stats = PushStats()
    with pytest.raises(ZeroDivisionError):
        with _stage_guard(stats, "albums"):
            raise ZeroDivisionError("marshaller bug")
    assert stats.failed_stages == {}


def test_push_stage_error_names_every_failed_stage():
    stats = PushStats()
    stats.failed_stages = {"photo_masters": "HTTP 500", "places": "HTTP 400"}
    err = PushStageError(stats)
    text = str(err)
    assert "photo_masters" in text and "places" in text
    assert err.stats is stats
    # It is still a WebSyncError, so existing `except WebSyncError` handlers
    # (the Jobs panel, run_push) keep working.
    assert isinstance(err, WebSyncError)
