"""The mini's /batch/upload reply is {"stored": N, "refs": [...], ...}.
The client must count `stored` (fix-up 11 follow-up: every hand-over was
recorded as uploaded=0 because the client only looked for "accepted")."""

from __future__ import annotations

from pathlib import Path

from photoarchive.inference_client.base import RefImage
from photoarchive.inference_client.lan import LanInferenceClient


def _client_with_reply(monkeypatch, reply: dict) -> LanInferenceClient:
    client = LanInferenceClient("http://mock", "tok")
    monkeypatch.setattr(client, "_request_json", lambda *a, **kw: reply)
    return client


def _img(ref: str) -> RefImage:
    return RefImage(ref=ref, path=Path("unused.jpg"), prepared_bytes=b"\xff\xd8\xff")


def test_upload_counts_stored_from_mini_reply(monkeypatch):
    client = _client_with_reply(
        monkeypatch, {"job_name": "classify", "stored": 3, "bytes": 30, "refs": ["1", "2", "3"], "held": 3},
    )
    res = client.upload("classify", [_img("1"), _img("2"), _img("3")], endpoint_edge=1024)
    assert res.accepted == 3
    assert res.rejected == []


def test_upload_falls_back_to_accepted_key(monkeypatch):
    client = _client_with_reply(monkeypatch, {"accepted": 2, "rejected": ["9"]})
    res = client.upload("classify", [_img("1"), _img("2"), _img("9")], endpoint_edge=1024)
    assert res.accepted == 2
    assert res.rejected == ["9"]


def test_upload_empty_list_never_calls_service(monkeypatch):
    calls = []
    client = LanInferenceClient("http://mock", "tok")
    monkeypatch.setattr(client, "_request_json", lambda *a, **kw: calls.append(a) or {})
    res = client.upload("classify", [], endpoint_edge=1024)
    assert res.accepted == 0 and calls == []
