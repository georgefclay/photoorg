"""Batch contract: NDJSON out, resumable by the caller, cancellable mid-run."""

from __future__ import annotations

import asyncio
import json

import httpx

from app import batch as batchmod
from app.main import app
from tests.conftest import TEST_TOKEN


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
        timeout=30.0,
    )


async def _collect(url: str, payload: dict) -> list[dict]:
    lines: list[dict] = []
    async with _client() as client:
        async with client.stream("POST", url, json=payload) as response:
            assert response.status_code == 200, await response.aread()
            assert response.headers["content-type"].startswith("application/x-ndjson")
            async for line in response.aiter_lines():
                if line.strip():
                    lines.append(json.loads(line))
    return lines


async def test_batch_streams_header_items_and_summary(shared_root, mock_vlm):
    items = [{"ref": f"r{i}", "path": f"img{i}.jpg"} for i in range(3)]
    lines = await _collect("/batch/describe", {"items": items})

    header = lines[0]
    assert header["endpoint"] == "describe"
    assert header["total"] == 3
    assert header["skipped"] == 0
    assert header["job_id"].startswith("describe-")

    results = lines[1:-1]
    assert [r["ref"] for r in results] == ["r0", "r1", "r2"]
    for result in results:
        assert result["ok"] is True
        assert result["model"] == "mock-vlm"
        assert result["prompt_version"] == "describe.v1"
        assert "text" in result["result"]
        assert isinstance(result["elapsed_ms"], int)

    summary = lines[-1]
    assert summary["summary"] is True
    assert summary["status"] == "completed"
    assert summary["done"] == 3
    assert summary["failed"] == 0


async def test_skip_refs_resumes_a_partial_run(shared_root, mock_vlm):
    items = [{"ref": f"r{i}", "path": f"img{i}.jpg"} for i in range(3)]
    lines = await _collect("/batch/describe", {"items": items, "skip_refs": ["r0", "r1"]})
    assert lines[0]["total"] == 1
    assert lines[0]["skipped"] == 2
    assert [line["ref"] for line in lines[1:-1]] == ["r2"]
    assert lines[-1]["done"] == 1


async def test_bad_item_fails_alone_and_the_batch_continues(shared_root, mock_vlm):
    items = [
        {"ref": "good", "path": "img0.jpg"},
        {"ref": "missing", "path": "nope.jpg"},
        {"ref": "good2", "path": "img1.jpg"},
    ]
    lines = await _collect("/batch/describe", {"items": items})
    results = {line["ref"]: line for line in lines[1:-1]}
    assert results["good"]["ok"] is True
    assert results["missing"]["ok"] is False
    assert "No such file" in results["missing"]["error"]
    assert results["good2"]["ok"] is True
    assert lines[-1]["done"] == 2
    assert lines[-1]["failed"] == 1


async def test_detect_faces_batch(shared_root, mock_faces):
    lines = await _collect(
        "/batch/detect-faces", {"items": [{"ref": "a", "path": "img0.jpg"}]}
    )
    result = lines[1]
    assert result["ok"] is True
    assert result["model"] == "mock-faces"
    assert len(result["result"]["faces"]) == 1


async def test_ndjson_file_survives_for_resume(shared_root, mock_vlm):
    items = [{"ref": f"r{i}", "path": f"img{i}.jpg"} for i in range(3)]
    lines = await _collect("/batch/describe", {"items": items})
    job_id = lines[0]["job_id"]

    # Exactly what a caller does after the service was killed and restarted.
    batchmod.registry._jobs.clear()
    recovered = batchmod.recover_from_disk(job_id)
    assert recovered["done"] == 3
    assert recovered["recovered_from_disk"] is True

    path = batchmod.get_settings().batch_dir / f"{job_id}.ndjson"
    assert batchmod.completed_refs(path) == ["r0", "r1", "r2"]


async def test_status_and_recovery_after_restart(shared_root, mock_vlm):
    items = [{"ref": "r0", "path": "img0.jpg"}]
    lines = await _collect("/batch/describe", {"items": items})
    job_id = lines[0]["job_id"]

    async with _client() as client:
        live = await client.get(f"/batch/status/{job_id}?include_refs=true")
        assert live.status_code == 200
        assert live.json()["status"] == "completed"
        assert live.json()["completed_refs"] == ["r0"]

        batchmod.registry._jobs.clear()
        after_restart = await client.get(f"/batch/status/{job_id}?include_refs=true")
        assert after_restart.status_code == 200
        body = after_restart.json()
        assert body["recovered_from_disk"] is True
        assert body["completed_refs"] == ["r0"]

        missing = await client.get("/batch/status/does-not-exist")
        assert missing.status_code == 404


async def test_cancel_stops_the_run_early(shared_root, mock_vlm, monkeypatch):
    """Cancel takes effect between items, and the summary says so."""
    from app.vlm import VLMOutput, vlm_engine

    async def slow_run_json(
        prompt_name, image, *, high_priority=True, max_tokens=None, timeout=None
    ):
        await asyncio.sleep(0.1)
        return VLMOutput(
            result={"text": "slow", "tags": ["a"]},
            prompt_version="describe.v1",
            raw="{}",
            parsed=True,
            attempts=1,
        )

    monkeypatch.setattr(vlm_engine, "run_json", slow_run_json)

    items = [{"ref": f"r{i}", "path": "img0.jpg"} for i in range(40)]

    async def consume() -> list[dict]:
        async with _client() as client:
            async with client.stream("POST", "/batch/describe", json={"items": items}) as response:
                body = await response.aread()
        return [json.loads(l) for l in body.decode().splitlines() if l.strip()]

    consumer = asyncio.create_task(consume())

    # Wait for the job to register, let a couple of items through, then cancel.
    job = None
    for _ in range(100):
        running = [j for j in batchmod.registry._jobs.values() if j.status == "running"]
        if running:
            job = running[0]
            break
        await asyncio.sleep(0.01)
    assert job is not None, "batch job never started"

    await asyncio.sleep(0.25)
    async with _client() as canceller:
        response = await canceller.post(f"/batch/cancel/{job.job_id}")
        assert response.status_code == 200
        assert response.json()["cancelling"] is True

    lines = await asyncio.wait_for(consumer, timeout=10)
    summary = lines[-1]
    assert summary["summary"] is True
    assert summary["status"] == "cancelled"
    assert 0 < summary["done"] < 40


async def test_cancel_unknown_job_is_404(shared_root):
    async with _client() as client:
        response = await client.post("/batch/cancel/nope")
        assert response.status_code == 404


async def test_unknown_batch_endpoint_is_404(shared_root):
    async with _client() as client:
        response = await client.post("/batch/summarise", json={"items": []})
        assert response.status_code == 404
        assert "describe" in response.json()["detail"]


async def test_batch_without_shared_root_is_400(mock_vlm):
    async with _client() as client:
        response = await client.post(
            "/batch/describe", json={"items": [{"ref": "a", "path": "x.jpg"}]}
        )
        assert response.status_code == 400
        assert "SHARED_ROOT" in response.json()["detail"]


async def test_batch_stream_reattaches_and_replays(shared_root, mock_vlm):
    items = [{"ref": f"r{i}", "path": f"img{i}.jpg"} for i in range(2)]
    lines = await _collect("/batch/describe", {"items": items})
    job_id = lines[0]["job_id"]

    async with _client() as client:
        async with client.stream("GET", f"/batch/stream/{job_id}") as response:
            assert response.status_code == 200
            replayed = [json.loads(l) for l in (await response.aread()).decode().splitlines() if l.strip()]
    assert [line["ref"] for line in replayed[:-1]] == ["r0", "r1"]
    assert replayed[-1]["summary"] is True
