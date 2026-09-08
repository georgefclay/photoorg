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


# ---- fix-up 1 -----------------------------------------------------------


def test_health_token_ok_true_when_probe_accepts():
    """Fix-up 1.1: /health passes AND authenticated probe doesn't 401
    → token_ok=True."""
    session = MagicMock(spec=requests.Session)
    session.get.side_effect = [
        _resp(200, {"model_name": "vlm-1"}),   # /health
        _resp(404, "no such job"),             # /batch/inbox/_probe
    ]
    client = _client_with_session(session)
    h = client.health()
    assert h.ok is True
    assert h.token_ok is True


def test_health_token_ok_false_when_probe_401s():
    """Fix-up 1.1: /health passes, probe 401s → token_ok=False (amber dot)."""
    session = MagicMock(spec=requests.Session)
    session.get.side_effect = [
        _resp(200, {"model_name": "vlm-1"}),   # /health
        _resp(401, "unauthorised"),            # /batch/inbox/_probe
    ]
    client = _client_with_session(session)
    h = client.health()
    assert h.ok is True
    assert h.token_ok is False


def test_results_after_swallows_404_as_empty():
    """Fix-up 1.2: 404 on the results endpoint means "no results file yet"
    — yield nothing, don't raise."""
    class Resp404:
        status_code = 404
        def raise_for_status(self): pass
        def iter_lines(self, decode_unicode=True):
            return iter([])
        def __enter__(self): return self
        def __exit__(self, *_): return False
        request = MagicMock(method="GET"); url = "http://mock/x"; text = "no such job"

    session = MagicMock(spec=requests.Session)
    session.get.return_value = Resp404()
    client = _client_with_session(session)
    out = list(client.results_after("classify", after=0))
    assert out == []


def test_start_from_inbox_reads_only_first_line():
    """Fix-up 1.3: POST /batch/{endpoint} streams NDJSON for the life of the
    batch. The client must read exactly the header line and close the
    connection — not wait for the whole body."""
    lines_yielded: list[str] = []

    class StreamResp:
        status_code = 200
        def raise_for_status(self): pass
        def iter_lines(self, decode_unicode=True):
            first = json.dumps({"job_id": "abc123", "endpoint": "classify",
                                 "total": 500, "skipped": 0})
            lines_yielded.append(first)
            yield first
            # Second line "never arrives" — simulate the streaming hang.
            # If the client wrongly consumes past the header, the test
            # times out here.
            raise AssertionError(
                "client tried to read past the header line — it must stop after the first line"
            )
        def __enter__(self): return self
        def __exit__(self, *_): return False
        request = MagicMock(method="POST"); url = "http://mock/x"; text = ""

    session = MagicMock(spec=requests.Session)
    session.request.return_value = StreamResp()
    client = _client_with_session(session)
    result = client.start_from_inbox("classify", "classify")
    assert result.accepted is True
    assert result.raw["job_id"] == "abc123"
    assert lines_yielded == [
        json.dumps({"job_id": "abc123", "endpoint": "classify",
                    "total": 500, "skipped": 0}),
    ]
    # And it did so over a streaming request.
    call = session.request.call_args
    assert call.kwargs.get("stream") is True


def test_start_from_inbox_401_still_fatal():
    """Retry semantics: 401 on the streaming POST is fatal, same as
    everywhere else."""
    class Resp401:
        status_code = 401
        def raise_for_status(self): pass
        def iter_lines(self, decode_unicode=True):
            return iter([])
        def __enter__(self): return self
        def __exit__(self, *_): return False
        request = MagicMock(method="POST"); url = "http://mock/x"; text = ""

    session = MagicMock(spec=requests.Session)
    session.request.return_value = Resp401()
    client = _client_with_session(session)
    with pytest.raises(FatalAuthError):
        client.start_from_inbox("classify", "classify")
