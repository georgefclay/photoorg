"""The mini picks its own work back up: no client, no laptop, after a restart."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from app import batch as batchmod
from app import inbox, jobqueue
from app.main import app
from tests.conftest import TEST_TOKEN, make_image_bytes


def stage(job_name: str, refs: list[str]) -> None:
    for ref in refs:
        inbox.store(job_name, ref, make_image_bytes())


def test_default_endpoints_cover_the_planned_jobs():
    assert jobqueue.DEFAULT_ENDPOINTS == {
        "transcribe_backs": "transcribe-back",
        "detect_faces": "detect-faces",
        "classify": "classify",
        "describe": "describe",
        "estimate_date": "estimate-date",
    }
    # Backs first, faces next, the multi-day descriptive work last.
    assert jobqueue.PRIORITY[0] == "transcribe_backs"
    assert jobqueue.PRIORITY[1] == "detect_faces"
    assert jobqueue.PRIORITY[-1] == "estimate_date"


def test_resumable_is_in_priority_order(shared_root):
    stage("estimate_date", ["1"])
    stage("transcribe_backs", ["2"])
    stage("describe", ["3"])
    stage("detect_faces", ["4"])
    assert jobqueue.resumable() == [
        "transcribe_backs",
        "detect_faces",
        "describe",
        "estimate_date",
    ]


def test_a_job_with_no_known_endpoint_is_skipped(shared_root):
    stage("something_unplanned", ["1"])
    assert jobqueue.resumable() == []


def test_a_registered_job_name_gets_its_endpoint_back(shared_root):
    stage("something_unplanned", ["1"])
    jobqueue.register("something_unplanned", "describe")
    assert jobqueue.endpoint_for("something_unplanned") == "describe"
    assert jobqueue.resumable() == ["something_unplanned"]


def test_queue_file_survives_a_reread(shared_root):
    jobqueue.register("describe", "describe", status="running")
    jobqueue.mark("describe", "completed")
    assert jobqueue.load()["describe"]["status"] == "completed"
    assert jobqueue.queue_path().name == "queue.json"


def test_a_corrupt_queue_file_is_not_fatal(shared_root):
    jobqueue.queue_path().write_text("{not json", encoding="utf-8")
    assert jobqueue.load() == {}


def test_pending_refs_excludes_finished_work(shared_root):
    stage("describe", ["1", "2", "3"])
    assert jobqueue.pending_refs("describe") == ["1", "2", "3"]
    jobqueue.results_path("describe").write_text(
        '{"ref": "1", "ok": true}\n{"ref": "2", "ok": false}\n', encoding="utf-8"
    )
    # A failed ref is still pending: it gets another go.
    assert jobqueue.pending_refs("describe") == ["2", "3"]


def test_service_restart_resumes_the_job_with_no_client(shared_root, mock_vlm):
    """The whole point: upload, lose the process, and the work still finishes."""
    stage("describe", ["11", "12", "13"])
    jobqueue.register("describe", "describe", status="running")

    # Half of it had already been done before the crash.
    jobqueue.results_path("describe").write_text(
        '{"ref": "11", "ok": true, "result": {"text": "done earlier"}}\n',
        encoding="utf-8",
    )
    batchmod.registry._jobs.clear()
    assert jobqueue.pending_refs("describe") == ["12", "13"]

    headers = {"Authorization": f"Bearer {TEST_TOKEN}"}
    with TestClient(app) as client:  # entering the context runs startup
        summary = {}
        for _ in range(200):
            summary = client.get("/batch/results/describe/summary", headers=headers).json()
            if summary["pending"] == 0 and not summary["running"]:
                break
            time.sleep(0.05)

    assert summary["pending"] == 0
    assert summary["done"] == 3
    # Appended to the same file, not restarted from scratch.
    refs = batchmod.completed_refs(jobqueue.results_path("describe"))
    assert refs == ["11", "12", "13"]
    assert jobqueue.load()["describe"]["status"] == "completed"


def test_startup_does_nothing_when_there_is_no_work(shared_root, mock_vlm):
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["batches"]["running"] == 0


def test_health_reports_inbox_and_blackout(shared_root, mock_vlm, monkeypatch):
    stage("describe", ["11", "12"])
    monkeypatch.setenv("BATCH_BLACKOUT", "Tue 04:30-07:30;Fri 04:30-07:30")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            body = client.get("/health").json()
        assert body["blackout"]["windows"] == ["Tue 04:30-07:30", "Fri 04:30-07:30"]
        assert "active" in body["blackout"]
        assert body["config"]["max_image_edge_by_endpoint"]["describe"] == 1024
        assert body["config"]["max_image_edge_by_endpoint"]["transcribe-back"] == 1536
    finally:
        get_settings.cache_clear()


async def test_a_client_started_job_still_closes_out_the_queue(shared_root, mock_vlm):
    """queue.json must be updated even when no supervisor is watching."""
    import asyncio

    import httpx

    from app.main import app

    stage("describe", ["21", "22"])
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
        timeout=30.0,
    )
    async with client:
        async with client.stream(
            "POST", "/batch/describe", json={"job_name": "describe", "from_inbox": True}
        ) as response:
            await response.aread()

        for _ in range(60):
            if jobqueue.load().get("describe", {}).get("status") == "completed":
                break
            await asyncio.sleep(0.05)

    assert jobqueue.load()["describe"]["status"] == "completed"
