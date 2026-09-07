"""Blackout windows: the mini is George's machine before it is a batch runner."""

import asyncio
import datetime as dt

import httpx
import pytest

from app import batch as batchmod
from app import blackout, inbox
from app.config import get_settings
from app.main import app
from tests.conftest import TEST_TOKEN, make_image_bytes

SPEC = "Tue 04:30-07:30;Fri 04:30-07:30"


def at(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text)


def test_parses_the_documented_format():
    windows = blackout.parse(SPEC)
    assert len(windows) == 2
    assert windows[0].weekday == 1  # Tuesday
    assert windows[0].start == dt.time(4, 30)
    assert windows[1].weekday == 4  # Friday
    assert blackout.describe(SPEC) == ["Tue 04:30-07:30", "Fri 04:30-07:30"]


@pytest.mark.parametrize(
    "moment,blacked_out",
    [
        ("2026-09-08 04:29", False),  # Tue, one minute early
        ("2026-09-08 04:30", True),   # Tue, window opens
        ("2026-09-08 06:00", True),
        ("2026-09-08 07:29", True),
        ("2026-09-08 07:30", False),  # Tue, window closes
        ("2026-09-11 06:00", True),   # Fri
        ("2026-09-07 06:00", False),  # Mon
        ("2026-09-09 06:00", False),  # Wed
    ],
)
def test_window_boundaries(moment, blacked_out):
    assert (blackout.active_window(SPEC, at(moment)) is not None) is blacked_out


def test_empty_spec_never_blacks_out():
    assert blackout.parse("") == []
    assert blackout.active_window("", at("2026-09-08 06:00")) is None


def test_bad_clauses_are_skipped_not_fatal():
    windows = blackout.parse("Tue 04:30-07:30;garbage;Notaday 01:00-02:00;Fri 25:99-26:00")
    assert len(windows) == 1
    assert windows[0].weekday == 1


def test_full_day_names_and_case_are_accepted():
    assert blackout.parse("tuesday 04:30-07:30")[0].weekday == 1
    assert blackout.parse("FRI 4:30-7:30")[0].weekday == 4
    assert blackout.parse("FRI 4:30-7:30")[0].start == dt.time(4, 30)


def test_window_over_midnight():
    spec = "Fri 23:00-01:00"
    assert blackout.active_window(spec, at("2026-09-11 23:30")) is not None  # Fri night
    assert blackout.active_window(spec, at("2026-09-12 00:30")) is not None  # into Sat
    assert blackout.active_window(spec, at("2026-09-12 01:30")) is None


def test_ends_after_reports_when_work_may_resume():
    moment = at("2026-09-08 06:00")
    window = blackout.active_window(SPEC, moment)
    assert window.ends_after(moment) == at("2026-09-08 07:30")


def test_pause_reason_is_none_outside_a_window(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "batch_blackout", "")
    monkeypatch.setattr(settings, "batch_min_free_gb", 0.0)
    assert batchmod.pause_reason(settings) is None


def test_pause_reason_reports_low_memory(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "batch_blackout", "")
    monkeypatch.setattr(settings, "batch_min_free_gb", 10_000.0)
    reason = batchmod.pause_reason(settings)
    assert reason is not None and "free" in reason


async def test_a_running_batch_stands_down_and_comes_back(shared_root, mock_vlm, monkeypatch):
    """The batch finishes the item in flight, sleeps, and resumes on its own."""
    settings = get_settings()
    now = dt.datetime.now()
    day = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][now.weekday()]
    window = (
        f"{day} {(now - dt.timedelta(hours=1)):%H:%M}-"
        f"{(now + dt.timedelta(hours=1)):%H:%M}"
    )
    monkeypatch.setattr(settings, "batch_blackout", window)
    monkeypatch.setattr(settings, "batch_pause_poll_s", 0.05)

    for ref in ["1", "2", "3"]:
        inbox.store("describe", ref, make_image_bytes())

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
        timeout=30.0,
    )

    async def run():
        async with client:
            async with client.stream(
                "POST", "/batch/describe", json={"job_name": "describe", "from_inbox": True}
            ) as response:
                await response.aread()

    task = asyncio.create_task(run())
    try:
        job = None
        for _ in range(200):
            job = batchmod.registry.get("describe")
            if job is not None and job.pause_reason:
                break
            await asyncio.sleep(0.01)

        assert job is not None
        assert job.pause_reason is not None
        assert "blackout" in job.pause_reason
        assert job.done == 0  # nothing ran inside the window

        monkeypatch.setattr(settings, "batch_blackout", "")  # window closes
        await asyncio.wait_for(task, timeout=10)
        assert job.status == "completed"
        assert job.done == 3
    finally:
        if not task.done():
            task.cancel()


SHIPPED = "Mon 23:30-01:30;Thu 23:30-01:30;Tue 09:30-10:45;Fri 09:30-10:45"


@pytest.mark.parametrize(
    "moment,blacked_out,why",
    [
        ("2026-09-08 00:00", True, "ac-publish-cycle starts, Tuesday"),
        ("2026-09-08 00:48", True, "latest observed publish-cycle finish"),
        ("2026-09-08 01:30", False, "publish window closes"),
        ("2026-09-08 10:11", True, "ac-linkedin-cycle, Tuesday"),
        ("2026-09-11 00:35", True, "ac-publish-cycle, Friday"),
        ("2026-09-11 10:11", True, "ac-linkedin-cycle, Friday"),
        ("2026-09-09 00:30", False, "Wednesday, nothing scheduled"),
        ("2026-09-07 05:10", False, "Monday weekly report is not worth an hour"),
        ("2026-09-08 03:00", False, "the old guessed window covered nothing real"),
    ],
)
def test_the_windows_that_ship_cover_the_real_jobs(moment, blacked_out, why):
    assert (blackout.active_window(SHIPPED, at(moment)) is not None) is blacked_out, why


def test_a_window_opening_before_midnight_belongs_to_the_next_day():
    # "Mon 23:30-01:30" is how the Tuesday 00:00 publish cycle gets its margin.
    assert blackout.active_window(SHIPPED, at("2026-09-07 23:30")) is not None
    assert blackout.active_window(SHIPPED, at("2026-09-07 23:29")) is None
