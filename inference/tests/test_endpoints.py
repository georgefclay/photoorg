import json

import pytest

VLM_ROUTES = ["/classify", "/transcribe-back", "/describe", "/estimate-date"]


@pytest.mark.parametrize("route", VLM_ROUTES)
def test_envelope_shape_and_ref_echo(client, auth, upload, mock_vlm, route):
    payload = upload("photo-4711")
    response = client.post(route, headers=auth, **payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"ref", "model", "elapsed_ms", "prompt_version", "result"}
    assert body["ref"] == "photo-4711"
    assert body["model"] == "mock-vlm"
    assert isinstance(body["elapsed_ms"], int)
    assert body["prompt_version"].endswith(".v1")
    assert isinstance(body["result"], dict)


def test_classify_label_vocabulary(client, auth, upload, mock_vlm):
    mock_vlm["result"] = {"label": "Back Of Print", "confidence": 1.4, "reason": "ink"}
    body = client.post("/classify", headers=auth, **upload()).json()
    assert body["result"]["label"] == "back_of_print"
    assert body["result"]["confidence"] == 1.0  # clamped


def test_classify_unknown_label_becomes_other(client, auth, upload, mock_vlm):
    mock_vlm["result"] = {"label": "polaroid", "confidence": 0.5, "reason": "?"}
    result = client.post("/classify", headers=auth, **upload()).json()["result"]
    assert result["label"] == "other"
    assert result["raw_label"] == "polaroid"


def test_transcribe_back_shape(client, auth, upload, mock_vlm):
    result = client.post("/transcribe-back", headers=auth, **upload()).json()["result"]
    assert result["parsed_dates"] == [
        {"text": "Mar 62", "iso": "1962-03-01", "precision": "month"}
    ]
    assert result["names"] == ["Peggy"]
    assert 0.0 <= result["confidence"] <= 1.0


def test_transcribe_back_drops_bad_iso_and_precision(client, auth, upload, mock_vlm):
    mock_vlm["result"] = {
        "text": "Easter 1962",
        "parsed_dates": [
            {"text": "Easter 1962", "iso": "1962", "precision": "holiday"},
            {"text": "", "iso": "1962-01-01", "precision": "year"},
        ],
        "names": ["Peggy", "  "],
        "confidence": 0.8,
    }
    result = client.post("/transcribe-back", headers=auth, **upload()).json()["result"]
    assert result["parsed_dates"] == [
        {"text": "Easter 1962", "iso": None, "precision": "unknown"}
    ]
    assert result["names"] == ["Peggy"]


def test_describe_truncates_to_thirty_words(client, auth, upload, mock_vlm):
    mock_vlm["result"] = {"text": " ".join(["word"] * 45), "tags": ["A", "a", "b"]}
    result = client.post("/describe", headers=auth, **upload()).json()["result"]
    assert len(result["text"].split()) == 30
    assert result["truncated"] is True
    assert result["tags"] == ["a", "b"]


def test_estimate_date_range_is_widened(client, auth, upload, mock_vlm):
    mock_vlm["result"] = {
        "year_min": 1965,
        "year_max": 1965,
        "confidence": 0.9,
        "reasoning": "hairstyle",
        "is_scan_of_print": False,
    }
    result = client.post("/estimate-date", headers=auth, **upload()).json()["result"]
    assert result["year_max"] - result["year_min"] + 1 >= 3
    assert result["widened"] is True


def test_estimate_date_low_confidence_widens_to_ten(client, auth, upload, mock_vlm):
    mock_vlm["result"] = {
        "year_min": 1965,
        "year_max": 1966,
        "confidence": 0.2,
        "reasoning": "unsure",
        "is_scan_of_print": False,
    }
    result = client.post("/estimate-date", headers=auth, **upload()).json()["result"]
    assert result["year_max"] - result["year_min"] + 1 >= 10


def test_unparseable_reply_is_reported_not_raised(client, auth, upload, mock_vlm):
    mock_vlm["parsed"] = False
    mock_vlm["raw"] = "I think this is a photo of a dog."
    response = client.post("/describe", headers=auth, **upload())
    assert response.status_code == 200
    assert response.json()["result"] == {
        "error": "unparseable",
        "raw": "I think this is a photo of a dog.",
    }


def test_model_crash_is_503(client, auth, upload, mock_vlm):
    from app.vlm import VLMUnavailable

    mock_vlm["exception"] = VLMUnavailable("metal out of memory")
    response = client.post("/classify", headers=auth, **upload())
    assert response.status_code == 503
    assert "reloaded" in response.json()["detail"]


def test_missing_ref_is_400(client, auth, image_bytes, mock_vlm):
    response = client.post(
        "/classify", headers=auth, files={"file": ("x.jpg", image_bytes, "image/jpeg")}
    )
    assert response.status_code == 400


def test_wrong_content_type_is_415(client, auth, mock_vlm):
    response = client.post(
        "/classify", headers={**auth, "Content-Type": "text/plain"}, content=b"hello"
    )
    assert response.status_code == 415


def test_undecodable_upload_is_400(client, auth, mock_vlm):
    response = client.post(
        "/classify",
        headers=auth,
        files={"file": ("x.jpg", b"not an image", "image/jpeg")},
        data={"ref": "r"},
    )
    assert response.status_code == 400


def test_detect_faces_reports_original_dimensions(client, auth, mock_faces):
    from app.config import get_settings
    from tests.conftest import make_image_bytes

    big = make_image_bytes(width=3072, height=2304)
    assert get_settings().max_image_edge == 1536
    body = client.post(
        "/detect-faces",
        headers=auth,
        files={"file": ("big.jpg", big, "image/jpeg")},
        data={"ref": "big"},
    ).json()
    assert body["ref"] == "big"
    assert body["model"] == "mock-faces"
    assert body["prompt_version"] is None
    # Dimensions are the ORIGINAL file's, not the downscaled copy the model saw.
    assert body["result"]["image_w"] == 3072
    assert body["result"]["image_h"] == 2304
    face = body["result"]["faces"][0]
    assert set(face) == {"bbox", "det_score", "embedding", "landmarks"}
    assert set(face["bbox"]) == {"x", "y", "w", "h"}
    assert len(face["embedding"]) == 512


def test_match_faces_ranks_by_distance(client, auth):
    body = client.post(
        "/match-faces",
        headers=auth,
        json={
            "ref": "face-9",
            "embedding": [1.0, 0.0, 0.0],
            "references": [
                {"person_id": 7, "embedding": [0.0, 1.0, 0.0]},
                {"person_id": 3, "embedding": [1.0, 0.0, 0.0]},
                {"person_id": 5, "embedding": [0.8, 0.6, 0.0]},
            ],
            "top_k": 2,
        },
    ).json()
    matches = body["result"]["matches"]
    assert [m["person_id"] for m in matches] == [3, 5]
    assert matches[0]["distance"] == pytest.approx(0.0, abs=1e-6)
    assert body["result"]["reference_count"] == 3
    assert body["result"]["dimensions"] == 3
    assert body["ref"] == "face-9"


def test_match_faces_dimension_mismatch_is_400(client, auth):
    response = client.post(
        "/match-faces",
        headers=auth,
        json={
            "ref": "f",
            "embedding": [1.0, 0.0],
            "references": [{"person_id": 1, "embedding": [1.0, 0.0, 0.0]}],
        },
    )
    assert response.status_code == 400


def test_match_faces_with_no_references_returns_empty(client, auth):
    body = client.post(
        "/match-faces", headers=auth, json={"ref": "f", "embedding": [1.0, 0.0], "references": []}
    ).json()
    assert body["result"]["matches"] == []


def test_slow_model_is_504(client, auth, upload, monkeypatch):
    import asyncio

    from app.vlm import vlm_engine

    async def hangs(prompt_name, image, *, high_priority=True, max_tokens=None, timeout=None):
        raise asyncio.TimeoutError

    monkeypatch.setattr(vlm_engine, "run_json", hangs)
    response = client.post("/classify", headers=auth, **upload())
    assert response.status_code == 504
    assert "did not answer" in response.json()["detail"]
