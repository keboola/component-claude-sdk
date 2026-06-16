from __future__ import annotations

import socket
from unittest.mock import patch

import httpx
import vcr as vcrpy

from advocate.server import AdvocateServer

MY_VCR = vcrpy.VCR(
    cassette_library_dir="tests/unit/cassettes",
    record_mode="none",
    match_on=["method", "scheme", "host", "port", "path", "body"],
    ignore_hosts=["127.0.0.1"],
)

_VALID_PAYLOAD = {
    "action_id": "test-001",
    "model": "claude-haiku-4-5",
    "max_tokens": 5,
    "messages": [{"role": "user", "content": "Say hello"}],
}


def _make_server() -> AdvocateServer:
    """Start a server on 127.0.0.1:0; port is available via server.port after start()."""
    server = AdvocateServer(anthropic_key="dummy-key-for-test")
    server.start()
    return server


def _tcp_client(port: int) -> httpx.Client:
    return httpx.Client(base_url=f"http://127.0.0.1:{port}")


# ---------------------------------------------------------------------------
# Test 1: Schema validation — rejects unexpected fields
# ---------------------------------------------------------------------------


def test_schema_rejects_extra_fields() -> None:
    """Server returns 400 for any unexpected top-level key."""
    server = _make_server()
    try:
        payload = dict(_VALID_PAYLOAD)
        payload["upstream_override"] = "https://evil.example.com"
        with _tcp_client(server.port) as client:
            resp = client.post(
                "/v1/messages",
                json=payload,
                headers={"content-type": "application/json"},
            )
        assert resp.status_code == 400
        assert "unexpected field" in resp.json()["error"]
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# Test 2: Rejects agent-supplied upstream override (security critical)
# ---------------------------------------------------------------------------


def test_schema_rejects_upstream_override() -> None:
    """Sending a 'base_url' or 'upstream' field is rejected with 400."""
    server = _make_server()
    try:
        for evil_field in ("base_url", "upstream", "api_base"):
            payload = dict(_VALID_PAYLOAD)
            payload[evil_field] = "https://evil.example.com"
            with _tcp_client(server.port) as client:
                resp = client.post(
                    "/v1/messages",
                    json=payload,
                    headers={"content-type": "application/json"},
                )
            assert resp.status_code == 400, f"expected 400 for field '{evil_field}'"
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# Test 3: Proxy turn completes via TCP with no real key (VCR replay)
# ---------------------------------------------------------------------------


@MY_VCR.use_cassette("anthropic_proxy_turn.json")
def test_proxy_completes_turn_via_tcp() -> None:
    """A Python TCP client (no real key) completes a model turn through the proxy."""
    from advocate import anthropic_proxy

    anthropic_proxy._idempotency_cache.clear()

    server = AdvocateServer(anthropic_key="dummy-key-for-test")
    server.start()
    try:
        payload = dict(_VALID_PAYLOAD)
        with httpx.Client(base_url=f"http://127.0.0.1:{server.port}") as client:
            resp = client.post(
                "/v1/messages",
                json=payload,
                headers={"content-type": "application/json"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["role"] == "assistant"
        assert body["content"][0]["text"]
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# Test 4: action_id idempotency — no double upstream call on success
# ---------------------------------------------------------------------------


def test_action_id_idempotency_no_double_upstream_call() -> None:
    """Replaying the same action_id returns cached result; upstream called exactly once."""
    from advocate import anthropic_proxy

    anthropic_proxy._idempotency_cache.clear()

    mock_result = (200, {"id": "msg_mock", "role": "assistant", "content": [{"type": "text", "text": "Hi"}]})

    server = _make_server()
    try:
        with patch.object(anthropic_proxy, "_call_upstream", return_value=mock_result) as mock_call:
            for _ in range(2):
                with _tcp_client(server.port) as client:
                    resp = client.post(
                        "/v1/messages",
                        json=_VALID_PAYLOAD,
                        headers={"content-type": "application/json"},
                    )
                assert resp.status_code == 200
            assert mock_call.call_count == 1, f"expected 1 upstream call, got {mock_call.call_count}"
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# Test 6 (new): upstream httpx failure → sanitized 502, no internal detail leaked
# ---------------------------------------------------------------------------


def test_upstream_httpx_error_returns_sanitized_502() -> None:
    """An httpx network error from _call_upstream becomes a clean 502 with no detail."""
    from advocate import anthropic_proxy

    anthropic_proxy._idempotency_cache.clear()

    server = _make_server()
    try:
        with patch.object(
            anthropic_proxy,
            "_call_upstream",
            side_effect=httpx.ConnectError("connection refused to internal.example.com"),
        ):
            with _tcp_client(server.port) as client:
                resp = client.post(
                    "/v1/messages",
                    json=_VALID_PAYLOAD,
                    headers={"content-type": "application/json"},
                )
        assert resp.status_code == 502
        body = resp.json()
        # Generic message only — no internal host/exception details exposed to agent
        assert body["error"] == "upstream request failed"
        assert "internal.example.com" not in resp.text
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# Test 7 (new): non-JSON upstream body → sanitized 502
# ---------------------------------------------------------------------------


def test_upstream_non_json_body_returns_sanitized_502() -> None:
    """A non-JSON upstream response (e.g. 5xx HTML) becomes a clean 502."""
    from advocate import anthropic_proxy

    anthropic_proxy._idempotency_cache.clear()

    # Simulate resp.json() raising ValueError (JSONDecodeError) from _call_upstream
    def _bad_json(payload: dict, key: str) -> tuple[int, dict]:  # noqa: ARG001
        raise ValueError("No JSON object could be decoded")

    server = _make_server()
    try:
        with patch.object(anthropic_proxy, "_call_upstream", side_effect=_bad_json):
            with _tcp_client(server.port) as client:
                resp = client.post(
                    "/v1/messages",
                    json=_VALID_PAYLOAD,
                    headers={"content-type": "application/json"},
                )
        # handle_request catches _call_upstream exceptions and returns (502, sanitized)
        assert resp.status_code == 502
        assert resp.json()["error"] == "upstream request failed"
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# Test 8 (new): errored action_id is NOT cached — retry re-attempts upstream
# ---------------------------------------------------------------------------


def test_error_result_not_cached_retry_calls_upstream_again() -> None:
    """A 502 from upstream is NOT stored in the idempotency cache; retry hits upstream again."""
    from advocate import anthropic_proxy

    anthropic_proxy._idempotency_cache.clear()

    error_result = (502, {"error": "upstream request failed"})
    success_result = (200, {"id": "msg_ok", "role": "assistant", "content": [{"type": "text", "text": "Hi"}]})

    server = _make_server()
    try:
        # First call: upstream returns 502
        # Second call (same action_id): upstream returns 200 — proves the error wasn't pinned
        with patch.object(
            anthropic_proxy,
            "_call_upstream",
            side_effect=[error_result, success_result],
        ) as mock_call:
            with _tcp_client(server.port) as client:
                resp1 = client.post(
                    "/v1/messages",
                    json=_VALID_PAYLOAD,
                    headers={"content-type": "application/json"},
                )
            assert resp1.status_code == 502

            with _tcp_client(server.port) as client:
                resp2 = client.post(
                    "/v1/messages",
                    json=_VALID_PAYLOAD,
                    headers={"content-type": "application/json"},
                )
            assert resp2.status_code == 200
            assert mock_call.call_count == 2, (
                f"expected 2 upstream calls (error not cached), got {mock_call.call_count}"
            )
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# Test 9 (new): oversized Content-Length → 413 without reading the body
# ---------------------------------------------------------------------------


def test_oversized_content_length_returns_413() -> None:
    """A Content-Length over the cap is rejected 413 before the body is read."""
    server = _make_server()
    try:
        # Send a real request but with an inflated Content-Length header.
        # Use raw socket so we control the header precisely.
        body = b'{"action_id": "x", "model": "m", "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}'
        oversized = 5_000_001
        raw = (
            f"POST /v1/messages HTTP/1.1\r\n"
            f"Host: 127.0.0.1\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {oversized}\r\n"
            f"\r\n"
        ).encode() + body  # actual body is tiny; server should reject on header alone

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.connect(("127.0.0.1", server.port))
            sock.sendall(raw)
            response = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
                if b"\r\n\r\n" in response:
                    # Read enough to get the status line + body
                    break

        assert b"413" in response, f"expected 413 in response, got: {response[:200]}"
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# Test 10 (new): malformed Content-Length → 400
# ---------------------------------------------------------------------------


def test_malformed_content_length_returns_400() -> None:
    """A non-integer Content-Length header returns 400, not a ValueError crash."""
    server = _make_server()
    try:
        raw = (
            b"POST /v1/messages HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: not-a-number\r\n"
            b"\r\n"
        )

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.connect(("127.0.0.1", server.port))
            sock.sendall(raw)
            response = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
                if b"\r\n\r\n" in response:
                    break

        assert b"400" in response, f"expected 400 in response, got: {response[:200]}"
    finally:
        server.stop()
