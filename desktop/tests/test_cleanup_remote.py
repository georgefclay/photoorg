"""Remote enhance: the provider interface, the null default, and the Claid
client against a mock HTTP server.

Nothing here touches the network beyond 127.0.0.1, and nothing in Phase 7
spends money: `CLEANUP_REMOTE_PROVIDER` defaults to `null`, which disables the
E key with a tooltip.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from PIL import Image

from photoarchive.config import Settings
from photoarchive.modes.cleanup.remote import (
    ClaidProvider, NullProvider, RemoteError, RemoteUnavailable, build_provider,
)


def _settings(tmp_path: Path, **overrides) -> Settings:
    kwargs = dict(
        MASTER_ROOTS="dummy=Z:\\",
        WORKING_DIR=tmp_path / "working",
        QUARANTINE_DIR=tmp_path / "quarantine",
        MANUAL_FIX_DIR=tmp_path / "manual-fix",
        THUMBS_DIR=tmp_path / "thumbs",
        CLEANUP_DIR=tmp_path / "cleanup",
        DATABASE_URL="postgresql://x/y",
        INFERENCE_URL="http://x", INFERENCE_TOKEN="x",
        WEB_API_URL="http://x", WEB_API_TOKEN="x",
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


# --------------------------------------------------------------------------
# Config → provider
# --------------------------------------------------------------------------

def test_the_default_provider_is_null_and_says_why(tmp_path):
    provider = build_provider(_settings(tmp_path))
    assert isinstance(provider, NullProvider)
    assert provider.available is False
    assert "CLEANUP_REMOTE_PROVIDER" in (provider.unavailable_reason() or "")
    assert provider.cost_estimate(10) == 0.0
    with pytest.raises(RemoteUnavailable):
        provider.submit(tmp_path / "nope.jpg")


def test_claid_without_a_key_is_unavailable_not_broken(tmp_path):
    provider = build_provider(_settings(tmp_path, CLEANUP_REMOTE_PROVIDER="claid"))
    assert isinstance(provider, ClaidProvider)
    assert provider.available is False
    assert "CLAID_API_KEY" in (provider.unavailable_reason() or "")


def test_an_unknown_provider_is_refused_at_config_load(tmp_path):
    with pytest.raises(ValueError, match="CLEANUP_REMOTE_PROVIDER"):
        _settings(tmp_path, CLEANUP_REMOTE_PROVIDER="magic")


def test_cost_estimate_scales(tmp_path):
    provider = build_provider(_settings(
        tmp_path, CLEANUP_REMOTE_PROVIDER="claid", CLAID_API_KEY="k",
        CLAID_COST_PER_OP_USD=0.03))
    assert provider.cost_estimate(1) == pytest.approx(0.03)
    assert provider.cost_estimate(100) == pytest.approx(3.0)


# --------------------------------------------------------------------------
# Mock server
# --------------------------------------------------------------------------

class _MockClaid(BaseHTTPRequestHandler):
    """Enough of Claid's shape to exercise submit → poll → download."""

    mode = "inline"          # 'inline' | 'async' | 'fail' | 'unauthorized'
    polls_before_ready = 1
    _poll_count = 0
    received_auth: str | None = None
    received_bytes = 0

    def log_message(self, *a):  # keep pytest output clean
        pass

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        type(self).received_auth = self.headers.get("Authorization")
        length = int(self.headers.get("Content-Length") or 0)
        type(self).received_bytes = length
        self.rfile.read(length)
        if type(self).mode == "unauthorized":
            return self._json(401, {"error": "bad key"})
        if type(self).mode == "fail":
            return self._json(500, {"error": "boom"})
        if type(self).mode == "inline":
            return self._json(200, {"data": {
                "id": "job-1",
                "output": {"tmp_url": f"http://{self.headers['Host']}/result.jpg"},
            }})
        # async: no output yet
        type(self)._poll_count = 0
        return self._json(200, {"data": {"id": "job-2", "status": "processing"}})

    def do_GET(self):
        if self.path == "/result.jpg":
            buf = _jpeg_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(buf)))
            self.end_headers()
            self.wfile.write(buf)
            return
        if self.path.startswith("/v1-beta1/image/requests/"):
            cls = type(self)
            cls._poll_count += 1
            if cls._poll_count >= cls.polls_before_ready:
                return self._json(200, {"data": {
                    "status": "done",
                    "output": {"tmp_url": f"http://{self.headers['Host']}/result.jpg"},
                }})
            return self._json(200, {"data": {"status": "processing"}})
        self._json(404, {"error": "not found"})


def _jpeg_bytes() -> bytes:
    import io
    import numpy as np
    arr = (np.arange(64 * 48 * 3, dtype=np.uint8) % 255).reshape(48, 64, 3)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "JPEG", quality=90)
    return buf.getvalue()


@pytest.fixture
def mock_claid():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MockClaid)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _MockClaid.mode = "inline"
    _MockClaid.polls_before_ready = 1
    _MockClaid._poll_count = 0
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _provider(base: str, tmp_path: Path, **kw) -> ClaidProvider:
    return ClaidProvider(api_key="test-key", base_url=base,
                         poll_interval_s=0.01, poll_timeout_s=3.0, **kw)


def _upload(tmp_path: Path) -> Path:
    p = tmp_path / "upload.jpg"
    p.write_bytes(_jpeg_bytes())
    return p


def test_submit_and_download_an_inline_result(mock_claid, tmp_path):
    provider = _provider(mock_claid, tmp_path)
    assert provider.available
    job = provider.submit(_upload(tmp_path))
    assert job.provider == "claid"
    assert job.job_ref == "job-1"
    assert job.cost_estimate_usd == pytest.approx(0.03)
    assert _MockClaid.received_auth == "Bearer test-key"
    assert _MockClaid.received_bytes > 0

    out = provider.await_result(job, tmp_path / "out.jpg")
    assert out.exists()
    with Image.open(out) as im:
        assert im.size == (64, 48)


def test_an_async_job_is_polled_until_it_is_ready(mock_claid, tmp_path):
    _MockClaid.mode = "async"
    _MockClaid.polls_before_ready = 3
    provider = _provider(mock_claid, tmp_path)
    job = provider.submit(_upload(tmp_path))
    assert job.job_ref == "job-2"
    assert job.details.get("result_url") is None

    out = provider.await_result(job, tmp_path / "out.jpg")
    assert out.exists()
    assert _MockClaid._poll_count >= 3


def test_a_401_is_reported_as_unavailable_not_a_transient_error(mock_claid, tmp_path):
    _MockClaid.mode = "unauthorized"
    provider = _provider(mock_claid, tmp_path)
    with pytest.raises(RemoteUnavailable, match="401"):
        provider.submit(_upload(tmp_path))


def test_a_server_error_is_a_remote_error(mock_claid, tmp_path):
    _MockClaid.mode = "fail"
    provider = _provider(mock_claid, tmp_path)
    with pytest.raises(RemoteError, match="500"):
        provider.submit(_upload(tmp_path))


def test_a_job_that_never_finishes_times_out(mock_claid, tmp_path):
    _MockClaid.mode = "async"
    _MockClaid.polls_before_ready = 10_000
    provider = ClaidProvider(api_key="k", base_url=mock_claid,
                             poll_interval_s=0.01, poll_timeout_s=0.05)
    job = provider.submit(_upload(tmp_path))
    with pytest.raises(RemoteError, match="timed out"):
        provider.await_result(job, tmp_path / "out.jpg")
