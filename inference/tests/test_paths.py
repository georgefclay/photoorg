"""The `path` variant must never read outside SHARED_ROOT."""

from __future__ import annotations

import pytest

from app.config import get_settings


def test_path_variant_disabled_when_shared_root_empty(client, auth, mock_vlm):
    assert get_settings().shared_root_path is None
    response = client.post("/classify", headers=auth, json={"ref": "a", "path": "x.jpg"})
    assert response.status_code == 400
    assert "SHARED_ROOT" in response.json()["detail"]


def test_relative_path_inside_root_works(client, auth, mock_vlm, shared_root):
    response = client.post("/classify", headers=auth, json={"ref": "a", "path": "img0.jpg"})
    assert response.status_code == 200
    assert response.json()["ref"] == "a"


def test_absolute_path_inside_root_works(client, auth, mock_vlm, shared_root):
    response = client.post(
        "/classify", headers=auth, json={"ref": "a", "path": str(shared_root / "img1.jpg")}
    )
    assert response.status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "../outside.jpg",
        "sub/../../outside.jpg",
        "/etc/hosts",
        "~/.ssh/id_rsa",
    ],
)
def test_escape_attempts_are_rejected(client, auth, mock_vlm, shared_root, tmp_path, path):
    (tmp_path / "outside.jpg").write_bytes(b"whatever")
    response = client.post("/classify", headers=auth, json={"ref": "a", "path": path})
    assert response.status_code == 400
    assert "outside SHARED_ROOT" in response.json()["detail"]


def test_symlink_out_of_root_is_rejected(client, auth, mock_vlm, shared_root, tmp_path):
    from tests.conftest import make_image_bytes

    outside = tmp_path / "secret.jpg"
    outside.write_bytes(make_image_bytes())
    (shared_root / "link.jpg").symlink_to(outside)

    response = client.post("/classify", headers=auth, json={"ref": "a", "path": "link.jpg"})
    assert response.status_code == 400
    assert "outside SHARED_ROOT" in response.json()["detail"]


def test_missing_file_inside_root_is_404(client, auth, mock_vlm, shared_root):
    response = client.post("/classify", headers=auth, json={"ref": "a", "path": "nope.jpg"})
    assert response.status_code == 404


def test_empty_path_is_400(client, auth, mock_vlm, shared_root):
    response = client.post("/classify", headers=auth, json={"ref": "a", "path": "   "})
    assert response.status_code == 400


def test_multipart_path_variant_also_honours_the_root(client, auth, mock_vlm, shared_root):
    response = client.post(
        "/classify", headers=auth, data={"ref": "a", "path": "../outside.jpg"}
    )
    assert response.status_code == 400
