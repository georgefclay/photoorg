"""Upload, inbox listing, results collection, and resume after a restart."""

from __future__ import annotations

import json

import httpx

from app import batch as batchmod
from app import inbox, jobqueue
from app.main import app
from tests.conftest import TEST_TOKEN, make_image_bytes


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
        timeout=30.0,
    )


def _files(refs, ext: str = ".jpg"):
    return [("files", (f"{ref}{ext}", make_image_bytes(), "image/jpeg")) for ref in refs]


async def test_upload_then_list(shared_root):
    async with _client() as client:
        response = await client.post(
            "/batch/upload/describe", files=_files(["11", "12", "13"])
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["stored"] == 3
        assert body["refs"] == ["11", "12", "13"]
        assert body["bytes"] > 0
        assert body["held"] == 3

        listing = (await client.get("/batch/inbox/describe")).json()
        assert listing["count"] == 3
        assert listing["refs"] == ["11", "12", "13"]
        assert listing["pending"] == 3
        assert listing["done"] == 0
        assert listing["endpoint"] == "describe"


async def test_upload_is_idempotent(shared_root):
    async with _client() as client:
        await client.post("/batch/upload/describe", files=_files(["11"]))
        await client.post("/batch/upload/describe", files=_files(["11"]))
        listing = (await client.get("/batch/inbox/describe")).json()
        assert listing["count"] == 1


async def test_explicit_refs_override_filenames(shared_root):
    async with _client() as client:
        response = await client.post(
            "/batch/upload/describe",
            files=[("files", ("00004711_a1b2c3d4.jpg", make_image_bytes(), "image/jpeg"))],
            data={"refs": "4711"},
        )
        assert response.json()["refs"] == ["4711"]
        assert inbox.refs("describe") == ["4711"]


async def test_mismatched_refs_count_is_400(shared_root):
    async with _client() as client:
        response = await client.post(
            "/batch/upload/describe",
            files=_files(["a", "b"]),
            data={"refs": ["only-one"]},
        )
        assert response.status_code == 400
        assert "must match" in response.json()["detail"]


async def test_bad_job_name_is_refused(shared_root):
    async with _client() as client:
        response = await client.post("/batch/upload/..", files=_files(["a"]))
        assert response.status_code in (400, 404)


async def test_a_traversing_filename_cannot_escape_the_inbox(shared_root):
    """The ref is the filename's stem, so "../escape.jpg" is just "escape"."""
    async with _client() as client:
        response = await client.post(
            "/batch/upload/describe",
            files=[("files", ("../../escape.jpg", make_image_bytes(), "image/jpeg"))],
        )
        assert response.status_code == 200
        assert response.json()["refs"] == ["escape"]

    stored = inbox.paths("describe")["escape"]
    assert stored.parent == shared_root / "inbox" / "describe"
    assert not (shared_root.parent / "escape.jpg").exists()


async def test_a_ref_that_is_only_dots_is_refused(shared_root):
    async with _client() as client:
        response = await client.post(
            "/batch/upload/describe", files=_files([".."], ext="")
        )
        assert response.status_code == 400
        assert inbox.refs("describe") == []


async def test_no_files_is_400(shared_root):
    async with _client() as client:
        response = await client.post("/batch/upload/describe", data={"refs": "1"})
        assert response.status_code in (400, 415)


async def test_run_from_inbox_and_collect(shared_root, mock_vlm):
    async with _client() as client:
        await client.post("/batch/upload/describe", files=_files(["11", "12"]))

        async with client.stream(
            "POST", "/batch/describe", json={"job_name": "describe", "from_inbox": True}
        ) as response:
            assert response.status_code == 200
            body = (await response.aread()).decode()
        lines = [json.loads(l) for l in body.splitlines() if l.strip()]

        assert lines[0]["job_name"] == "describe"
        assert lines[0]["total"] == 2
        assert [l["ref"] for l in lines[1:-1]] == ["11", "12"]
        assert lines[-1]["status"] == "completed"

        # Results are keyed by job_name, not a random id.
        assert jobqueue.results_path("describe").name == "describe.ndjson"

        summary = (await client.get("/batch/results/describe/summary")).json()
        assert summary["done"] == 2
        assert summary["failed"] == 0
        assert summary["pending"] == 0
        assert summary["held_in_inbox"] == 2

        collected = await client.get("/batch/results/describe")
        got = [json.loads(l) for l in collected.text.splitlines() if l.strip()]
        assert [g["ref"] for g in got if not g.get("summary")] == ["11", "12"]

        # The caller keeps its own cursor.
        after = await client.get("/batch/results/describe?after=1")
        assert len(after.text.splitlines()) == len(collected.text.splitlines()) - 1


async def test_resume_appends_to_the_same_file(shared_root, mock_vlm):
    async with _client() as client:
        await client.post("/batch/upload/describe", files=_files(["11", "12"]))
        async with client.stream(
            "POST", "/batch/describe", json={"job_name": "describe", "from_inbox": True}
        ) as response:
            await response.aread()

        # More work arrives for the same job.
        await client.post("/batch/upload/describe", files=_files(["13"]))
        batchmod.registry._jobs.clear()  # as if the service had restarted

        listing = (await client.get("/batch/inbox/describe")).json()
        assert listing["pending"] == 1

        async with client.stream(
            "POST", "/batch/describe", json={"job_name": "describe", "from_inbox": True}
        ) as response:
            second = (await response.aread()).decode()
        lines = [json.loads(l) for l in second.splitlines() if l.strip()]
        assert lines[0]["total"] == 1
        assert lines[0]["skipped"] == 2
        assert lines[0]["resumed"] is True

        summary = (await client.get("/batch/results/describe/summary")).json()
        assert summary["done"] == 3  # one file, all three refs
        refs = batchmod.completed_refs(jobqueue.results_path("describe"))
        assert refs == ["11", "12", "13"]


async def test_nothing_pending_is_404(shared_root, mock_vlm):
    async with _client() as client:
        response = await client.post(
            "/batch/describe", json={"job_name": "describe", "from_inbox": True}
        )
        assert response.status_code == 404
        assert "Nothing pending" in response.json()["detail"]


async def test_from_inbox_without_job_name_is_400(shared_root):
    async with _client() as client:
        response = await client.post("/batch/describe", json={"from_inbox": True})
        assert response.status_code == 400


async def test_sweep_removes_only_finished_inputs(shared_root, mock_vlm):
    async with _client() as client:
        await client.post("/batch/upload/describe", files=_files(["11", "12"]))
        async with client.stream(
            "POST", "/batch/describe", json={"job_name": "describe", "from_inbox": True}
        ) as response:
            await response.aread()
        await client.post("/batch/upload/describe", files=_files(["13"]))

        refused = await client.delete("/batch/inbox/describe")
        assert refused.status_code == 400  # a sweep must be asked for explicitly

        swept = (await client.delete("/batch/inbox/describe?done=true")).json()
        assert sorted(swept["refs"]) == ["11", "12"]
        assert inbox.refs("describe") == ["13"]  # pending input untouched


async def test_delete_one_ref(shared_root):
    async with _client() as client:
        await client.post("/batch/upload/describe", files=_files(["11", "12"]))
        assert (await client.delete("/batch/inbox/describe/11")).status_code == 200
        assert inbox.refs("describe") == ["12"]
        assert (await client.delete("/batch/inbox/describe/99")).status_code == 404


async def test_inbox_needs_shared_root(mock_vlm):
    async with _client() as client:
        response = await client.get("/batch/inbox/describe")
        assert response.status_code == 400
        assert "SHARED_ROOT" in response.json()["detail"]
