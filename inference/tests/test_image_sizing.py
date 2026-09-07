"""Each endpoint downscales to its own edge: image tokens are the cost driver."""

from tests.conftest import make_image_bytes


def _post(client, auth, route, width=3000, height=2000):
    return client.post(
        route,
        headers=auth,
        files={"file": ("big.jpg", make_image_bytes(width, height), "image/jpeg")},
        data={"ref": "sizing"},
    )


def test_describe_runs_at_1024(client, auth, mock_vlm):
    assert _post(client, auth, "/describe").status_code == 200
    assert max(mock_vlm["last_image_size"]) == 1024


def test_classify_and_estimate_date_run_at_1024(client, auth, mock_vlm):
    _post(client, auth, "/classify")
    assert max(mock_vlm["last_image_size"]) == 1024
    _post(client, auth, "/estimate-date")
    assert max(mock_vlm["last_image_size"]) == 1024


def test_transcribe_back_keeps_1536_for_handwriting(client, auth, mock_vlm):
    assert _post(client, auth, "/transcribe-back").status_code == 200
    assert max(mock_vlm["last_image_size"]) == 1536


def test_aspect_ratio_is_preserved(client, auth, mock_vlm):
    _post(client, auth, "/describe", width=3000, height=1500)
    width, height = mock_vlm["last_image_size"]
    assert (width, height) == (1024, 512)


def test_small_images_are_not_upscaled(client, auth, mock_vlm):
    _post(client, auth, "/describe", width=400, height=300)
    assert mock_vlm["last_image_size"] == (400, 300)


def test_detect_faces_still_reports_original_size(client, auth, mock_faces):
    body = _post(client, auth, "/detect-faces", 3000, 2000).json()
    assert (body["result"]["image_w"], body["result"]["image_h"]) == (3000, 2000)


def test_overrides_are_configurable(monkeypatch):
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "max_image_edge_overrides", "describe=512")
    assert settings.edge_for("describe") == 512
    assert settings.edge_for("classify") == settings.max_image_edge


def test_malformed_override_clause_is_ignored(monkeypatch):
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "max_image_edge_overrides", "describe=;classify=1024;junk")
    assert settings.edge_overrides == {"classify": 1024}
