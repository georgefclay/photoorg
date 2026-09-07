"""Contract tests run against a mocked model: no MLX, no downloads, no GPU."""

from __future__ import annotations

import io
import os
import tempfile
from pathlib import Path

import pytest

TEST_TOKEN = "test-token-do-not-use-in-anger"
_TMP_LOGS = Path(tempfile.mkdtemp(prefix="inference-test-logs-"))

# Environment beats the .env file in pydantic-settings, so this isolates the
# tests from whatever the machine's real .env says.
os.environ["INFERENCE_TOKEN"] = TEST_TOKEN
os.environ["LOG_DIR"] = str(_TMP_LOGS)
os.environ["SHARED_ROOT"] = ""
os.environ["MAX_IMAGE_EDGE"] = "1536"

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from app import batch as batchmod  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.faces import face_engine  # noqa: E402
from app.main import app  # noqa: E402
from app.vlm import VLMOutput, vlm_engine  # noqa: E402

get_settings.cache_clear()


@pytest.fixture
def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _clear_jobs():
    batchmod.registry._jobs.clear()
    yield
    batchmod.registry._jobs.clear()


def make_image_bytes(width: int = 400, height: int = 300, colour=(120, 90, 60)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.fixture
def image_bytes() -> bytes:
    return make_image_bytes()


@pytest.fixture
def upload(image_bytes):
    def _upload(ref: str = "ref-1"):
        return {"files": {"file": ("x.jpg", image_bytes, "image/jpeg")}, "data": {"ref": ref}}

    return _upload


@pytest.fixture
def shared_root(tmp_path, monkeypatch) -> Path:
    """Point SHARED_ROOT at a temp folder holding three real JPEGs."""
    root = tmp_path / "share"
    root.mkdir()
    for index in range(3):
        (root / f"img{index}.jpg").write_bytes(make_image_bytes(colour=(index * 40, 80, 200)))
    monkeypatch.setenv("SHARED_ROOT", str(root))
    get_settings.cache_clear()
    yield root
    get_settings.cache_clear()


@pytest.fixture
def mock_vlm(monkeypatch):
    """Replace the model with a canned reply. Returns a setter for the payload."""
    state: dict[str, object] = {
        "result": {
            "label": "photo",
            "confidence": 0.91,
            "reason": "people in a garden",
            "text": "Two children on a porch with a dog.",
            "tags": ["children", "porch", "dog"],
            "parsed_dates": [{"text": "Mar 62", "iso": "1962-03-01", "precision": "month"}],
            "names": ["Peggy"],
            "year_min": 1960,
            "year_max": 1968,
            "is_scan_of_print": True,
            "reasoning": "White print border and 1960s clothing.",
        },
        "parsed": True,
        "raw": "",
        "exception": None,
    }

    async def fake_run_json(
        prompt_name, image, *, high_priority=True, max_tokens=None, timeout=None
    ):
        if state["exception"] is not None:
            raise state["exception"]
        return VLMOutput(
            result=dict(state["result"]) if state["parsed"] else {"error": "unparseable", "raw": state["raw"]},
            prompt_version=f"{prompt_name}.v1",
            raw=state["raw"] or "{}",
            parsed=bool(state["parsed"]),
            attempts=1,
            prompt_tokens=1583,
            generation_tokens=52,
            peak_memory_gb=7.1,
        )

    monkeypatch.setattr(vlm_engine, "run_json", fake_run_json)
    monkeypatch.setattr(type(vlm_engine), "model_name", property(lambda self: "mock-vlm"))
    return state


@pytest.fixture
def mock_faces(monkeypatch):
    async def fake_detect(loaded):
        # One face in the middle of the model-space image; the route scales it back.
        return [
            {
                "bbox": {"x": 10.0, "y": 20.0, "w": 30.0, "h": 40.0},
                "det_score": 0.99,
                "embedding": [0.1] * 512,
                "landmarks": [[1.0, 2.0]] * 5,
            }
        ]

    monkeypatch.setattr(face_engine, "detect", fake_detect)
    monkeypatch.setattr(type(face_engine), "model_name", property(lambda self: "mock-faces"))
