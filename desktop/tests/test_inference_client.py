"""Contract tests for the LAN inference client. All HTTP is mocked;
no real network, no service required."""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock

import pytest
import requests

from photoarchive.inference_client import (
    ENDPOINT_TRANSCRIBE_BACK,
    FatalAuthError,
    HealthStatus,
    LanInferenceClient,
    RefImage,
    ServiceUnavailable,
)


def _resp(status: int, body: dict | str | bytes = b"", *, is_json: bool = True):
    r = MagicMock(spec=requests.Response)
    r.status_code = status
    r.request = MagicMock()
    r.request.method = "GET"
    r.url = "http://mock/x"
    if isinstance(body, (dict, list)):
        r.json.return_value = body
        r.text = json.dumps(body)
    elif isinstance(body, bytes):
        r.text = body.decode("utf-8", errors="replace")
        r.json.side_effect = ValueError("not json")
    else:
        r.text = body
        try:
            r.json.return_value = json.loads(body)
        except (TypeError, ValueError):
            r.json.side_effect = ValueError("not json")
    return r


def _client_with_session(session_mock, **kwargs) -> LanInferenceClient:
    return LanInferenceClient(
        "http://mock", "test-token",
        session=session_mock,
        backoff_base_s=0.001,   # fast tests
        **kwargs,
    )


def test_client_401_is_fatal():
    session = MagicMock(spec=requests.Session)
    session.request.return_value = _resp(401, "unauthorised")
    client = _client_with_session(session)
    with pytest.raises(FatalAuthError):
        client.inbox("classify")


def test_client_retries_on_503_then_succeeds():
    session = MagicMock(spec=requests.Session)
    session.request.side_effect = [
        _resp(503, "temporarily down"),
        _resp(503, "still down"),
        _resp(200, {"total": 10, "pending": 3, "with_results": 7}),
    ]
    client = _client_with_session(session, max_retries=3)
    status = client.inbox("classify")
    assert status.total == 10
    assert status.pending == 3
    assert session.request.call_count == 3


def test_client_gives_up_after_max_retries():
    session = MagicMock(spec=requests.Session)
    session.request.return_value = _resp(503, "down")
    client = _client_with_session(session, max_retries=2)
    with pytest.raises(ServiceUnavailable):
        client.inbox("classify")
    assert session.request.call_count == 3  # initial + 2 retries


def test_client_retries_on_connection_error():
    session = MagicMock(spec=requests.Session)
    session.request.side_effect = [
        requests.ConnectionError("refused"),
        _resp(200, {"total": 0, "pending": 0, "with_results": 0}),
    ]
    client = _client_with_session(session, max_retries=3)
    status = client.inbox("classify")
    assert status.total == 0
    assert session.request.call_count == 2


def test_upload_sends_multipart_with_ref_filename(tmp_path):
    session = MagicMock(spec=requests.Session)
    session.request.return_value = _resp(200, {"accepted": 2, "rejected": []})
    client = _client_with_session(session)

    from PIL import Image
    p1 = tmp_path / "img1.jpg"
    p2 = tmp_path / "img2.jpg"
    Image.new("RGB", (100, 80), (200, 100, 50)).save(p1, format="JPEG")
    Image.new("RGB", (100, 80), (50, 200, 100)).save(p2, format="JPEG")

    result = client.upload(
        "classify",
        [RefImage(ref="42", path=p1), RefImage(ref="43", path=p2)],
        endpoint_edge=1024,
    )
    assert result.accepted == 2
    # Inspect the multipart the client built.
    call = session.request.call_args
    assert call.kwargs["files"] is not None
    files = call.kwargs["files"]
    # Two file parts, each named "files" (as the service expects), with
    # filename equal to `<ref>.jpg`.
    refs = sorted(f[1][0] for f in files)
    assert refs == ["42.jpg", "43.jpg"]


def test_call_endpoint_carries_bearer_token():
    session = MagicMock(spec=requests.Session)
    session.request.return_value = _resp(200, {
        "ref": "9", "model": "m", "prompt_version": "classify.v1",
        "elapsed_ms": 100, "result": {"label": "photo", "confidence": 0.9},
    })
    client = _client_with_session(session)
    body = client.call_endpoint("classify", RefImage(ref="9", path=None,
                                                    prepared_bytes=b"payload"))
    assert body["ref"] == "9"
    call = session.request.call_args
    assert call.kwargs["headers"]["Authorization"] == "Bearer test-token"


def test_results_after_streams_ndjson_with_correct_line_numbers():
    lines = [
        json.dumps({"ref": "1", "ok": True, "model": "m", "prompt_version": "v1",
                    "elapsed_ms": 100, "result": {"label": "photo"}}),
        json.dumps({"ref": "2", "ok": True, "model": "m", "prompt_version": "v1",
                    "elapsed_ms": 110, "result": {"label": "document"}}),
    ]
    body = "\n".join(lines)

    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def iter_lines(self, decode_unicode=True):
            for L in lines:
                yield L
        def __enter__(self): return self
        def __exit__(self, *_): return False
        request = MagicMock(method="GET"); url = "http://mock/x"; text = body

    session = MagicMock(spec=requests.Session)
    session.get.return_value = FakeResp()
    client = _client_with_session(session)
    out = list(client.results_after("classify", after=7))
    assert [r.line_no for r in out] == [8, 9]
    assert [r.ref for r in out] == ["1", "2"]
    assert out[0].result["label"] == "photo"


def test_sweep_and_cancel_hit_correct_paths():
    session = MagicMock(spec=requests.Session)
    session.request.side_effect = [
        _resp(200, {"removed": 3}),
        _resp(200, {"cancelled": True}),
    ]
    client = _client_with_session(session)
    assert client.sweep("classify", done_only=True) == 3
    assert client.cancel("classify") is True


def test_health_absorbs_unreachable_service_without_raising():
    session = MagicMock(spec=requests.Session)
    session.get.side_effect = requests.ConnectionError("nope")
    client = _client_with_session(session)
    h = client.health()
    assert isinstance(h, HealthStatus)
    assert h.ok is False
