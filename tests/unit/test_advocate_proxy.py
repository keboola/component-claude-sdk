from __future__ import annotations

import os
import tempfile
import time
from unittest.mock import patch

import httpx
import pytest
import vcr as vcrpy

from advocate.server import AdvocateServer

MY_VCR = vcrpy.VCR(
    cassette_library_dir="tests/unit/cassettes",
    record_mode="none",
    match_on=["method", "scheme", "host", "port", "path", "body"],
    ignore_hosts=["localhost"],
)

_VALID_PAYLOAD = {
    "action_id": "test-001",
    "model": "claude-haiku-4-5",
    "max_tokens": 5,
    "messages": [{"role": "user", "content": "Say hello"}],
}


def _short_sock_path(name: str = "adv.sock") -> str:
    """Return a short socket path under /tmp to stay within AF_UNIX 104-char limit."""
    d = tempfile.mkdtemp(prefix="adv_", dir="/tmp")  # noqa: S108
    return os.path.join(d, name)


def _make_server(tmp_path: pytest.TempPathFactory) -> tuple[AdvocateServer, str]:  # noqa: ARG001
    sock_path = _short_sock_path()
    server = AdvocateServer(sock_path, anthropic_key="dummy-key-for-test")
    server.start()
    time.sleep(0.05)  # let the server thread bind
    return server, sock_path


def _uds_client(sock_path: str) -> httpx.Client:
    transport = httpx.HTTPTransport(uds=sock_path)
    return httpx.Client(transport=transport)


# ---------------------------------------------------------------------------
# Test 1: Schema validation — rejects unexpected fields
# ---------------------------------------------------------------------------

def test_schema_rejects_extra_fields(tmp_path: pytest.TempPathFactory) -> None:
    """Server returns 400 for any unexpected top-level key."""
    server, sock_path = _make_server(tmp_path)
    try:
        payload = dict(_VALID_PAYLOAD)
        payload["upstream_override"] = "https://evil.example.com"
        with _uds_client(sock_path) as client:
            resp = client.post(
                "http://localhost/v1/messages",
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

def test_schema_rejects_upstream_override(tmp_path: pytest.TempPathFactory) -> None:
    """Sending a 'base_url' or 'upstream' field is rejected with 400."""
    server, sock_path = _make_server(tmp_path)
    try:
        for evil_field in ("base_url", "upstream", "api_base"):
            payload = dict(_VALID_PAYLOAD)
            payload[evil_field] = "https://evil.example.com"
            with _uds_client(sock_path) as client:
                resp = client.post(
                    "http://localhost/v1/messages",
                    json=payload,
                    headers={"content-type": "application/json"},
                )
            assert resp.status_code == 400, f"expected 400 for field '{evil_field}'"
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# Test 3: Proxy turn completes via UDS with no real key (VCR replay)
# ---------------------------------------------------------------------------

@MY_VCR.use_cassette("anthropic_proxy_turn.json")
def test_proxy_completes_turn_via_uds(tmp_path: pytest.TempPathFactory) -> None:  # noqa: ARG001
    """A Python UDS client (no real key) completes a model turn through the proxy."""
    # Clear idempotency cache so VCR cassette is used fresh
    from advocate import anthropic_proxy
    anthropic_proxy._idempotency_cache.clear()

    sock_path = _short_sock_path("vcr.sock")
    server = AdvocateServer(sock_path, anthropic_key="dummy-key-for-test")
    server.start()
    time.sleep(0.05)
    try:
        payload = dict(_VALID_PAYLOAD)
        transport = httpx.HTTPTransport(uds=sock_path)
        with httpx.Client(transport=transport) as client:
            resp = client.post(
                "http://localhost/v1/messages",
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
# Test 4: action_id idempotency — no double upstream call
# ---------------------------------------------------------------------------

def test_action_id_idempotency_no_double_upstream_call(tmp_path: pytest.TempPathFactory) -> None:
    """Replaying the same action_id returns cached result; upstream called exactly once."""
    from advocate import anthropic_proxy
    anthropic_proxy._idempotency_cache.clear()

    mock_result = (200, {"id": "msg_mock", "role": "assistant", "content": [{"type": "text", "text": "Hi"}]})

    server, sock_path = _make_server(tmp_path)
    try:
        with patch.object(anthropic_proxy, "_call_upstream", return_value=mock_result) as mock_call:
            for _ in range(2):
                with _uds_client(sock_path) as client:
                    resp = client.post(
                        "http://localhost/v1/messages",
                        json=_VALID_PAYLOAD,
                        headers={"content-type": "application/json"},
                    )
                assert resp.status_code == 200
            assert mock_call.call_count == 1, f"expected 1 upstream call, got {mock_call.call_count}"
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# Test 5: Server does chmod 0o777 after bind
# ---------------------------------------------------------------------------

def test_server_socket_chmod_777(tmp_path: pytest.TempPathFactory) -> None:  # noqa: ARG001
    """AdvocateServer sets socket permissions to 0o777 after binding."""
    import stat

    sock_path = _short_sock_path("perm.sock")
    server = AdvocateServer(sock_path, anthropic_key="dummy")
    server.start()
    time.sleep(0.05)
    try:
        mode = oct(stat.S_IMODE(os.stat(sock_path).st_mode))
        assert mode == oct(0o777), f"expected 0o777, got {mode}"
    finally:
        server.stop()
