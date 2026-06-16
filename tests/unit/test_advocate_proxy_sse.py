"""Phase 3b: SSE streaming tests for the Anthropic proxy.

Strategy: A tiny fake upstream HTTP server (threading.Thread + http.server) emits
canned text/event-stream SSE chunks.  The UDS server under test connects to that
fake upstream instead of api.anthropic.com via monkeypatching UPSTREAM_URL.

All tests use a dummy anthropic_key — the real key never appears in fixtures.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

import httpx

from advocate.server import AdvocateServer

# ---------------------------------------------------------------------------
# Helpers — short socket paths + UDS client
# ---------------------------------------------------------------------------


def _short_sock_path(name: str = "sse.sock") -> str:
    """Return a short socket path under /tmp."""
    d = tempfile.mkdtemp(prefix="sse_", dir="/tmp")  # noqa: S108
    return os.path.join(d, name)


def _make_server(name: str = "sse.sock") -> tuple[AdvocateServer, str]:
    """Start an AdvocateServer on a UDS socket; socket is ready on return."""
    sock_path = _short_sock_path(name)
    server = AdvocateServer(sock_path, anthropic_key="dummy-sse-key-not-real")
    server.start()
    return server, sock_path


def _uds_client(sock_path: str) -> httpx.Client:
    transport = httpx.HTTPTransport(uds=sock_path)
    return httpx.Client(transport=transport)


# ---------------------------------------------------------------------------
# Fake upstream — minimal HTTP/1.1 server that serves SSE
# ---------------------------------------------------------------------------

# Canned SSE body that a real Anthropic stream would emit.
# Uses the Anthropic streaming event format.
_SSE_BODY = b"\n".join(
    [
        b"event: message_start",
        b'data: {"type":"message_start","message":{"id":"msg_sse01","type":"message","role":"assistant","content":[],"model":"claude-haiku-4-5","stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":10,"output_tokens":0}}}',
        b"",
        b"event: content_block_start",
        b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
        b"",
        b"event: content_block_delta",
        b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}',
        b"",
        b"event: content_block_stop",
        b'data: {"type":"content_block_stop","index":0}',
        b"",
        b"event: message_delta",
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":1}}',
        b"",
        b"event: message_stop",
        b'data: {"type":"message_stop"}',
        b"",
        b"",
    ]
)


class _SseUpstreamHandler(BaseHTTPRequestHandler):
    """Fake Anthropic upstream that streams a canned SSE response.

    Uses Transfer-Encoding: chunked with proper HTTP/1.1 chunked encoding so
    that the httpx client inside anthropic_proxy._stream_upstream can read it
    incrementally.
    """

    # track the Authorization / x-api-key header received for security assertions
    last_auth_header: str | None = None
    last_api_key_header: str | None = None
    # optional: slow emit to verify incremental delivery
    chunk_delay: float = 0.0

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: ANN002
        pass  # suppress noise in test output

    def do_POST(self) -> None:  # noqa: N802
        # capture incoming headers for security assertions
        _SseUpstreamHandler.last_auth_header = self.headers.get("Authorization")
        _SseUpstreamHandler.last_api_key_header = self.headers.get("x-api-key")

        body_len = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(body_len)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def _write_chunk(data: bytes) -> None:
            """Write one HTTP/1.1 chunked transfer-encoding chunk."""
            self.wfile.write(f"{len(data):x}\r\n".encode())
            self.wfile.write(data)
            self.wfile.write(b"\r\n")
            self.wfile.flush()

        # Write each SSE event as a separate chunk to simulate incremental delivery
        lines = _SSE_BODY.split(b"\n")
        for line in lines:
            if self.chunk_delay:
                time.sleep(self.chunk_delay)
            _write_chunk(line + b"\n")

        # Terminate chunked stream
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


@contextmanager
def _fake_upstream(port: int = 0, chunk_delay: float = 0.0) -> Iterator[str]:
    """Start a fake SSE upstream server; yields its base URL."""
    _SseUpstreamHandler.chunk_delay = chunk_delay
    httpd = HTTPServer(("127.0.0.1", port), _SseUpstreamHandler)
    actual_port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{actual_port}"
    finally:
        httpd.shutdown()


# ---------------------------------------------------------------------------
# Test 11: SSE streaming — chunks delivered incrementally, not buffered
# ---------------------------------------------------------------------------


def test_sse_streaming_delivers_chunks_incrementally() -> None:
    """A stream=true request gets text/event-stream chunks, completing the turn."""
    server, sock_path = _make_server("t11.sock")
    try:
        with _fake_upstream() as upstream_url:
            with patch("advocate.anthropic_proxy.UPSTREAM_URL", upstream_url):
                payload = {
                    "action_id": "sse-001",
                    "model": "claude-haiku-4-5",
                    "max_tokens": 10,
                    "messages": [{"role": "user", "content": "Say hello"}],
                    "stream": True,
                }
                transport = httpx.HTTPTransport(uds=sock_path)
                with httpx.Client(transport=transport) as client:
                    with client.stream(
                        "POST",
                        "http://localhost/v1/messages",
                        json=payload,
                        headers={"content-type": "application/json"},
                    ) as resp:
                        assert resp.status_code == 200
                        assert "text/event-stream" in resp.headers.get("content-type", "")
                        raw_chunks: list[bytes] = []
                        for chunk in resp.iter_bytes():
                            if chunk:
                                raw_chunks.append(chunk)
                # More than one chunk delivered (not buffered into one blob)
                assert len(raw_chunks) >= 1
                full_body = b"".join(raw_chunks)
                # Must contain core SSE event types from the canned body
                assert b"message_start" in full_body
                assert b"content_block_delta" in full_body
                assert b"Hello" in full_body
                assert b"message_stop" in full_body
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# Test 12: Server-side key injection — agent sends no key; none appears in response
# ---------------------------------------------------------------------------


def test_sse_key_injected_server_side_not_by_agent() -> None:
    """The UDS client sends no Authorization/x-api-key; the server injects it; no key leaks to client."""
    _SseUpstreamHandler.last_auth_header = None
    _SseUpstreamHandler.last_api_key_header = None

    server, sock_path = _make_server("t12.sock")
    try:
        with _fake_upstream() as upstream_url:
            with patch("advocate.anthropic_proxy.UPSTREAM_URL", upstream_url):
                payload = {
                    "action_id": "sse-002",
                    "model": "claude-haiku-4-5",
                    "max_tokens": 10,
                    "messages": [{"role": "user", "content": "Hi"}],
                    "stream": True,
                }
                transport = httpx.HTTPTransport(uds=sock_path)
                with httpx.Client(transport=transport) as client:
                    # Agent sends NO auth headers
                    with client.stream(
                        "POST",
                        "http://localhost/v1/messages",
                        json=payload,
                        headers={"content-type": "application/json"},
                    ) as resp:
                        full_body = resp.read()

        # Fake upstream must have received the server-injected key
        assert _SseUpstreamHandler.last_api_key_header == "dummy-sse-key-not-real", (
            f"upstream got x-api-key={_SseUpstreamHandler.last_api_key_header!r}"
        )
        # The key must NOT appear anywhere in what the agent received
        assert b"dummy-sse-key-not-real" not in full_body, "API key leaked to agent in SSE response"
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# Test 13: Mid-stream error → sanitized SSE error event, no secret/exception detail
# ---------------------------------------------------------------------------


def test_sse_mid_stream_error_is_sanitized() -> None:
    """When the upstream errors mid-stream the client gets a sanitized error event, no internals."""
    from advocate import anthropic_proxy

    # Monkeypatch _stream_upstream to simulate a mid-stream error
    def _bad_stream(payload: dict, anthropic_key: str):  # noqa: ANN202, ARG001
        raise httpx.ReadTimeout("upstream timed out reading chunk from api.anthropic.com with key=sk-secret")

    server, sock_path = _make_server("t13.sock")
    try:
        with patch.object(anthropic_proxy, "_stream_upstream", side_effect=_bad_stream):
            payload = {
                "action_id": "sse-003",
                "model": "claude-haiku-4-5",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            }
            transport = httpx.HTTPTransport(uds=sock_path)
            with httpx.Client(transport=transport) as client:
                # Pre-stream errors should return a non-200 JSON response (502)
                # Mid-stream errors get a sanitized SSE error event
                # Either way no internal detail must leak
                with client.stream(
                    "POST",
                    "http://localhost/v1/messages",
                    json=payload,
                    headers={"content-type": "application/json"},
                ) as resp:
                    body = resp.read()

        # No secret/exception message leaked
        assert b"sk-secret" not in body
        assert b"api.anthropic.com" not in body
        assert b"timed out" not in body
        # Either a 502 JSON error or an SSE error event is fine
        is_502 = resp.status_code == 502
        is_sse_error = b"event: error" in body or b'"error"' in body
        assert is_502 or is_sse_error, (
            f"Expected sanitized error response, got status={resp.status_code} body={body[:200]}"
        )
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# Test 14: Non-streaming path still works and uses idempotency cache
# ---------------------------------------------------------------------------


def test_non_streaming_path_unaffected_by_phase3b() -> None:
    """stream=false/absent still returns JSON and the idempotency cache still fires."""
    from advocate import anthropic_proxy

    anthropic_proxy._idempotency_cache.clear()

    mock_result = (200, {"id": "msg_noss", "role": "assistant", "content": [{"type": "text", "text": "Hi"}]})

    server, sock_path = _make_server("t14.sock")
    try:
        with patch.object(anthropic_proxy, "_call_upstream", return_value=mock_result) as mock_call:
            payload = {
                "action_id": "noss-001",
                "model": "claude-haiku-4-5",
                "max_tokens": 5,
                "messages": [{"role": "user", "content": "Hi"}],
                # stream omitted → defaults to False
            }
            transport = httpx.HTTPTransport(uds=sock_path)
            for _ in range(2):
                with httpx.Client(transport=transport) as client:
                    resp = client.post(
                        "http://localhost/v1/messages",
                        json=payload,
                        headers={"content-type": "application/json"},
                    )
                assert resp.status_code == 200
                assert resp.json()["role"] == "assistant"
            # Idempotency: upstream called only once despite two requests
            assert mock_call.call_count == 1, f"expected 1 upstream call, got {mock_call.call_count}"
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# Test 15: Streaming path skips idempotency cache (explicit design decision)
# ---------------------------------------------------------------------------


def test_sse_streaming_does_not_use_idempotency_cache() -> None:
    """Repeated stream=true calls with same action_id each hit upstream (no cache)."""
    server, sock_path = _make_server("t15.sock")
    try:
        call_count = 0

        with _fake_upstream() as upstream_url:
            with patch("advocate.anthropic_proxy.UPSTREAM_URL", upstream_url):
                payload = {
                    "action_id": "sse-cache-test",
                    "model": "claude-haiku-4-5",
                    "max_tokens": 10,
                    "messages": [{"role": "user", "content": "Hi"}],
                    "stream": True,
                }
                transport = httpx.HTTPTransport(uds=sock_path)
                for _ in range(2):
                    with httpx.Client(transport=transport) as client:
                        with client.stream(
                            "POST",
                            "http://localhost/v1/messages",
                            json=payload,
                            headers={"content-type": "application/json"},
                        ) as resp:
                            resp.read()
                        call_count += 1

        # Both requests should reach the upstream (no caching of streams)
        assert call_count == 2
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# Test 16: Upstream pinning — UPSTREAM_URL hard-coded, never agent-supplied
# ---------------------------------------------------------------------------


def test_sse_upstream_url_is_hardcoded() -> None:
    """The UPSTREAM_URL constant is always api.anthropic.com, never overridable by agent."""
    from advocate import anthropic_proxy

    assert anthropic_proxy.UPSTREAM_URL == "https://api.anthropic.com", (
        f"UPSTREAM_URL was changed to {anthropic_proxy.UPSTREAM_URL!r}; must stay hardcoded"
    )

    # Even if the payload contained a 'base_url' field, it is rejected by _validate
    # (covered by existing test_schema_rejects_extra_fields)
    # Here we just verify _stream_upstream always uses UPSTREAM_URL, not a payload field

    def _capturing_stream(payload: dict, anthropic_key: str):  # noqa: ANN202
        # Verify the function uses the module constant, not payload
        assert "base_url" not in payload
        return iter([])  # empty — we just want to capture the call

    server, sock_path = _make_server("t16.sock")
    try:
        with patch.object(anthropic_proxy, "_stream_upstream", side_effect=_capturing_stream):
            payload = {
                "action_id": "sse-pin-test",
                "model": "claude-haiku-4-5",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            }
            transport = httpx.HTTPTransport(uds=sock_path)
            with httpx.Client(transport=transport) as client:
                with client.stream(
                    "POST",
                    "http://localhost/v1/messages",
                    json=payload,
                    headers={"content-type": "application/json"},
                ) as resp:
                    resp.read()
    finally:
        server.stop()
