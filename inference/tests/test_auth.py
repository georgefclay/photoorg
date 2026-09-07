import pytest

ENDPOINTS = [
    ("post", "/classify"),
    ("post", "/transcribe-back"),
    ("post", "/describe"),
    ("post", "/estimate-date"),
    ("post", "/detect-faces"),
    ("post", "/match-faces"),
    ("post", "/batch/describe"),
    ("get", "/batch/status/anything"),
]


@pytest.mark.parametrize("method,path", ENDPOINTS)
def test_no_token_is_401(client, method, path):
    response = getattr(client, method)(path)
    assert response.status_code == 401


@pytest.mark.parametrize("method,path", ENDPOINTS)
def test_wrong_token_is_401(client, method, path):
    response = getattr(client, method)(path, headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401


def test_malformed_scheme_is_401(client):
    response = client.post("/classify", headers={"Authorization": "Token abc"})
    assert response.status_code == 401


def test_health_needs_no_token(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "vlm" in body and "memory" in body and "batches" in body
    assert body["vlm"]["model"]
    assert "queue_depth" in body["vlm"]
    assert "uptime_s" in body
