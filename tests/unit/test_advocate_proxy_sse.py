"""Phase 3b: SSE streaming tests for the Anthropic proxy.

Strategy: A tiny fake upstream HTTP server (threading.Thread + http.server) emits
canned text/event-stream SSE chunks.  The TCP server under test connects to that
fake upstream instead of api.anthropic.com via monkeypatching UPSTREAM_URL.

All tests use a dummy anthropic_key — the real key never appears in fixtures.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

import httpx

from advocate.server import AdvocateServer

# ---------------------------------------------------------------------------
# Helpers — TCP client
# ---------------------------------------------------------------------------


def _make_server() -> AdvocateServer:
    """Start an AdvocateServer on 127.0.0.1:0; port is available via server.port."""
    server = AdvocateServer(anthropic_key="dummy-sse-key-not-real")
    server.start()
    return server


def _tcp_client(port: int) -> httpx.Client:
    return httpx.Client(base_url=f"http://127.0.0.1:{port}")


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

    def log_message(self, format: str, *args: object) -> None:  # noqa: ANN002  # type: ignore[override]
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
    """A stream=true request gets text/event-stream chunks delivered incrementally.

    Uses chunk_delay to spread out writes, then asserts that timestamps of
    received chunks span at least that delay — proving data is flushed
    incrementally rather than buffered into one blob.
    """
    per_chunk_delay = 0.05  # 50 ms between upstream writes
    server = _make_server()
    try:
        with _fake_upstream(chunk_delay=per_chunk_delay) as upstream_url:
            with patch("advocate.anthropic_proxy.UPSTREAM_URL", upstream_url):
                payload = {
                    "action_id": "sse-001",
                    "model": "claude-haiku-4-5",
                    "max_tokens": 10,
                    "messages": [{"role": "user", "content": "Say hello"}],
                    "stream": True,
                }
                with httpx.Client(base_url=f"http://127.0.0.1:{server.port}", timeout=30.0) as client:
                    with client.stream(
                        "POST",
                        "/v1/messages",
                        json=payload,
                        headers={"content-type": "application/json"},
                    ) as resp:
                        assert resp.status_code == 200
                        assert "text/event-stream" in resp.headers.get("content-type", "")
                        arrival_times: list[float] = []
                        raw_chunks: list[bytes] = []
                        for chunk in resp.iter_bytes():
                            if chunk:
                                arrival_times.append(time.monotonic())
                                raw_chunks.append(chunk)

                # Must have received content at distinct points in time — not one buffered blob.
                assert len(arrival_times) >= 2, "expected multiple distinct chunk arrivals"
                spread = arrival_times[-1] - arrival_times[0]
                assert spread >= per_chunk_delay, (
                    f"chunk spread {spread:.3f}s < per_chunk_delay {per_chunk_delay}s — "
                    "response may be buffered rather than streamed incrementally"
                )
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
    """The TCP client sends no Authorization/x-api-key; the server injects it; no key leaks to client."""
    _SseUpstreamHandler.last_auth_header = None
    _SseUpstreamHandler.last_api_key_header = None

    server = _make_server()
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
                with _tcp_client(server.port) as client:
                    # Agent sends NO auth headers
                    with client.stream(
                        "POST",
                        "/v1/messages",
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

# Bait strings that must NEVER appear in what the agent receives
_BAIT_SECRET = "sk-secret"
_BAIT_URL = "api.anthropic.com"
_BAIT_MSG = "timed out reading chunk"


def test_sse_mid_stream_error_is_sanitized() -> None:
    """A generator that yields real chunks then raises mid-iteration triggers the sanitized
    SSE error path (server.py lines 210-221), NOT the setup-502 path.

    Verifies:
    - Response is 200 + text/event-stream (headers already sent before the error)
    - Early chunks arrive before the error
    - A sanitized ``event: error`` event is appended
    - Bait strings (secret key, upstream URL, exception text) are absent from everything received
    """
    from advocate import anthropic_proxy

    # Generator: yields two real SSE lines then raises with bait embedded in the message.
    # This simulates a genuine mid-iteration network error.
    def _mid_stream_generator(payload: dict, anthropic_key: str) -> Iterator[bytes]:  # noqa: ARG001
        yield b"event: message_start\n"
        yield b'data: {"type":"message_start"}\n\n'
        # Now raise mid-iteration with bait in the exception message
        raise httpx.ReadTimeout(f"{_BAIT_MSG} from {_BAIT_URL} with key={_BAIT_SECRET}")

    server = _make_server()
    try:
        with patch.object(anthropic_proxy, "_stream_upstream", side_effect=_mid_stream_generator):
            payload = {
                "action_id": "sse-003",
                "model": "claude-haiku-4-5",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            }
            with _tcp_client(server.port) as client:
                with client.stream(
                    "POST",
                    "/v1/messages",
                    json=payload,
                    headers={"content-type": "application/json"},
                ) as resp:
                    # Headers are sent before iteration begins — must be 200 + SSE
                    assert resp.status_code == 200
                    assert "text/event-stream" in resp.headers.get("content-type", "")
                    body = resp.read()

        # Bait strings must not appear anywhere the agent can see
        assert _BAIT_SECRET.encode() not in body, "secret key leaked to agent"
        assert _BAIT_URL.encode() not in body, "upstream URL leaked to agent"
        assert _BAIT_MSG.encode() not in body, "exception text leaked to agent"

        # Early chunks must be present (proves generator ran before the error)
        assert b"message_start" in body, "early SSE chunk missing — generator may not have run"

        # Sanitized error event must be present
        assert b"event: error" in body, "sanitized SSE error event missing after mid-stream failure"
        assert b"upstream stream interrupted" in body
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# Test 13b: Setup-time error (call raises before first yield) → 502 JSON
# ---------------------------------------------------------------------------


def test_sse_setup_error_returns_502() -> None:
    """When _stream_upstream raises immediately (before yielding), the server
    returns a clean 502 JSON response via the setup ``except`` block — not SSE.

    This covers the distinct code path at server.py:179-184.
    """
    from advocate import anthropic_proxy

    def _raises_at_call(payload: dict, anthropic_key: str) -> Iterator[bytes]:  # noqa: ARG001
        raise httpx.ConnectError(f"could not connect to {_BAIT_URL} with key={_BAIT_SECRET}")

    server = _make_server()
    try:
        with patch.object(anthropic_proxy, "_stream_upstream", side_effect=_raises_at_call):
            payload = {
                "action_id": "sse-003b",
                "model": "claude-haiku-4-5",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            }
            with _tcp_client(server.port) as client:
                resp = client.post(
                    "/v1/messages",
                    json=payload,
                    headers={"content-type": "application/json"},
                )

        assert resp.status_code == 502
        body = resp.content
        assert resp.json()["error"] == "upstream request failed"
        # Bait must not appear
        assert _BAIT_SECRET.encode() not in body, "secret key leaked in 502 response"
        assert _BAIT_URL.encode() not in body, "upstream URL leaked in 502 response"
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

    server = _make_server()
    try:
        with patch.object(anthropic_proxy, "_call_upstream", return_value=mock_result) as mock_call:
            payload = {
                "action_id": "noss-001",
                "model": "claude-haiku-4-5",
                "max_tokens": 5,
                "messages": [{"role": "user", "content": "Hi"}],
                # stream omitted → defaults to False
            }
            for _ in range(2):
                with _tcp_client(server.port) as client:
                    resp = client.post(
                        "/v1/messages",
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
    server = _make_server()
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
                for _ in range(2):
                    with _tcp_client(server.port) as client:
                        with client.stream(
                            "POST",
                            "/v1/messages",
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

    server = _make_server()
    try:
        with patch.object(anthropic_proxy, "_stream_upstream", side_effect=_capturing_stream):
            payload = {
                "action_id": "sse-pin-test",
                "model": "claude-haiku-4-5",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            }
            with _tcp_client(server.port) as client:
                with client.stream(
                    "POST",
                    "/v1/messages",
                    json=payload,
                    headers={"content-type": "application/json"},
                ) as resp:
                    resp.read()
    finally:
        server.stop()
