"""Marked-slow integration: the real models, the real sample images.

    .venv/bin/python -m pytest tests/test_integration_slow.py -m slow -s

Skipped when inference/samples/ is empty. The samples are family photos and are
gitignored; George drops them in by hand.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.normalize import CLASSIFY_LABELS, DATE_PRECISIONS

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
COLOUR = SAMPLES / "colour.jpg"
BW = SAMPLES / "bw.jpg"
BACK = SAMPLES / "back.jpg"

pytestmark = pytest.mark.slow


def _require(path: Path) -> bytes:
    if not path.exists():
        pytest.skip(f"{path.name} not in inference/samples/")
    return path.read_bytes()


def _post(client, auth, route: str, path: Path) -> dict:
    payload = _require(path)
    response = client.post(
        route,
        headers=auth,
        files={"file": (path.name, payload, "image/jpeg")},
        data={"ref": f"{route.strip('/')}:{path.stem}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    print(f"\n--- {route} on {path.name} ({body['elapsed_ms']} ms) ---")
    print(json.dumps(body, indent=2, ensure_ascii=False)[:2000])
    assert set(body) == {"ref", "model", "elapsed_ms", "prompt_version", "result"}
    assert body["result"].get("error") != "unparseable", "model did not return JSON"
    return body


@pytest.mark.parametrize("sample", [COLOUR, BW, BACK])
def test_classify(client, auth, sample):
    result = _post(client, auth, "/classify", sample)["result"]
    assert result["label"] in CLASSIFY_LABELS
    assert 0.0 <= result["confidence"] <= 1.0
    assert result["reason"]


def test_transcribe_back(client, auth):
    result = _post(client, auth, "/transcribe-back", BACK)["result"]
    assert isinstance(result["text"], str)
    assert isinstance(result["names"], list)
    for date in result["parsed_dates"]:
        assert set(date) == {"text", "iso", "precision"}
        assert date["precision"] in DATE_PRECISIONS
    assert 0.0 <= result["confidence"] <= 1.0


@pytest.mark.parametrize("sample", [COLOUR, BW])
def test_describe(client, auth, sample):
    result = _post(client, auth, "/describe", sample)["result"]
    assert result["text"]
    assert len(result["text"].split()) <= 30
    assert 1 <= len(result["tags"]) <= 10


@pytest.mark.parametrize("sample", [COLOUR, BW])
def test_estimate_date(client, auth, sample):
    result = _post(client, auth, "/estimate-date", sample)["result"]
    assert result["year_max"] - result["year_min"] + 1 >= 3
    assert isinstance(result["is_scan_of_print"], bool)
    assert result["reasoning"]


@pytest.mark.parametrize("sample", [COLOUR, BW])
def test_detect_faces(client, auth, sample):
    body = _post(client, auth, "/detect-faces", sample)
    result = body["result"]
    from PIL import Image, ImageOps

    with Image.open(sample) as raw:
        width, height = ImageOps.exif_transpose(raw).size
    assert (result["image_w"], result["image_h"]) == (width, height)
    for face in result["faces"]:
        assert len(face["embedding"]) == 512
        box = face["bbox"]
        # Boxes are in the ORIGINAL image's pixels, so they must fit inside it.
        assert -1 <= box["x"] <= width and -1 <= box["y"] <= height
        assert box["w"] > 0 and box["h"] > 0
        assert box["x"] + box["w"] <= width + 1
        assert box["y"] + box["h"] <= height + 1


def test_match_faces_against_real_embeddings(client, auth):
    body = _post(client, auth, "/detect-faces", COLOUR)
    faces = body["result"]["faces"]
    if len(faces) < 2:
        pytest.skip("need at least two faces in colour.jpg")

    probe = faces[0]["embedding"]
    response = client.post(
        "/match-faces",
        headers=auth,
        json={
            "ref": "real",
            "embedding": probe,
            "references": [
                {"person_id": index, "embedding": face["embedding"]}
                for index, face in enumerate(faces)
            ],
        },
    )
    assert response.status_code == 200
    matches = response.json()["result"]["matches"]
    print("\n--- /match-faces distances ---")
    print(json.dumps(matches, indent=2))
    # A face matches itself exactly, and that must rank first.
    assert matches[0]["person_id"] == 0
    assert matches[0]["distance"] < 1e-4
    assert matches == sorted(matches, key=lambda m: m["distance"])
