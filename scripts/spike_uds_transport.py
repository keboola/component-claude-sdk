"""Phase 5a spike: prove how the bundled ``claude`` CLI routes model calls.

Tests THREE routing mechanisms in order:
  1. ANTHROPIC_BASE_URL=http://127.0.0.1:<port>  (TCP — does routing work at all?)
  2. ANTHROPIC_BASE_URL=unix://%2F<uds_path>     (unix:// Bun URL — does CLI accept it?)
  3. ANTHROPIC_UNIX_SOCKET=<uds_path>            (peer-session channel — NOT expected to route API)

Run with:
    cd /Users/matyasjirat/VSCodeProjects/Keboola/component-claude-sdk
    .venv/bin/python scripts/spike_uds_transport.py [tcp|unix_url|unix_socket]

    Defaults to running all three in sequence.

What it does
------------
For each mechanism:
  - Starts a stub server (TCP or UDS) that intercepts HTTP requests and
    returns a minimal valid Anthropic Messages API response.
  - Runs a minimal ``claude`` turn with env vars set for that mechanism.
  - Reports whether the stub received the request: CONFIRMED / NOT CONFIRMED.

This is a THROW-AWAY harness.  No real Anthropic key is used.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import BaseServer

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
)
log = logging.getLogger("spike")

# ---------------------------------------------------------------------------
# Minimal stub Anthropic response — must look like a real Messages API reply
# so the CLI stops retrying.
# ---------------------------------------------------------------------------
_STUB_RESPONSE = {
    "id": "msg_spike_stub_001",
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": "SPIKE-STUB: transport confirmed."}],
    "model": "claude-haiku-4-5-20251001",
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 10, "output_tokens": 8},
}


# ---------------------------------------------------------------------------
# Shared handler
# ---------------------------------------------------------------------------


class _StubHandler(BaseHTTPRequestHandler):
    """Logs every request; returns a valid Anthropic stub for /v1/messages."""

    server: _UnixStubServer | _TcpStubServer  # type: ignore[assignment]

    def log_message(self, format: str, *args: object) -> None:  # noqa: ANN002  # type: ignore[override]
        pass  # suppress default stderr

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return {}

    def do_POST(self) -> None:  # noqa: N802
        body = self._read_body()
        # Redact any key-like headers before logging
        safe_hdrs = {k: ("***" if "key" in k.lower() or "auth" in k.lower() else v) for k, v in self.headers.items()}
        log.info("[STUB] POST %-20s  headers=%s  body_model=%s", self.path, safe_hdrs, body.get("model"))
        self.server.received_requests.append(
            {"method": "POST", "path": self.path, "headers": dict(self.headers), "body_keys": list(body.keys())}
        )
        if self.path.rstrip("/") == "/v1/messages":
            self._respond(200, _STUB_RESPONSE)
        else:
            self._respond(404, {"error": f"stub: unknown path {self.path}"})

    def do_GET(self) -> None:  # noqa: N802
        log.info("[STUB] GET %s", self.path)
        self.server.received_requests.append({"method": "GET", "path": self.path})
        self._respond(404, {"error": "stub: GET not handled"})

    def _respond(self, status: int, body: dict) -> None:
        encoded = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


# ---------------------------------------------------------------------------
# TCP stub server
# ---------------------------------------------------------------------------


class _TcpStubServer(HTTPServer):
    """HTTPServer on a random localhost TCP port."""

    def __init__(self) -> None:
        self.received_requests: list[dict] = []
        super().__init__(("127.0.0.1", 0), _StubHandler)

    @property
    def port(self) -> int:
        return self.server_address[1]


# ---------------------------------------------------------------------------
# UDS stub server
# ---------------------------------------------------------------------------


class _UnixStubServer(HTTPServer):
    """HTTPServer bound to a Unix domain socket."""

    allow_reuse_address = True

    def __init__(self, sock_path: str) -> None:
        self.received_requests: list[dict] = []
        BaseServer.__init__(self, sock_path, _StubHandler)
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if Path(sock_path).exists():
            Path(sock_path).unlink()
        self.socket.bind(sock_path)
        os.chmod(sock_path, 0o777)  # noqa: S103
        self.server_activate()

    def server_bind(self) -> None:
        pass

    def server_close(self) -> None:
        self.socket.close()


# ---------------------------------------------------------------------------
# SDK query runner
# ---------------------------------------------------------------------------


async def _run_query(agent_env: dict[str, str]) -> tuple[list, BaseException | None]:
    """Run one minimal ``claude`` turn with the given env overrides."""
    from claude_agent_sdk import query
    from claude_agent_sdk.types import ClaudeAgentOptions

    options = ClaudeAgentOptions(
        tools=[],
        allowed_tools=[],
        permission_mode="dontAsk",
        env=agent_env,
        max_turns=1,
    )

    messages: list = []
    error: BaseException | None = None
    try:
        async for msg in query(prompt="Reply: CONFIRMED", options=options):
            messages.append(msg)
            log.info("[SDK] %s / %s", type(msg).__name__, getattr(msg, "subtype", "—"))
    except Exception as exc:  # noqa: BLE001
        error = exc
        log.warning("[SDK] raised: %s: %s", type(exc).__name__, exc)
    return messages, error


# ---------------------------------------------------------------------------
# Common env base (no real key, writable caches)
# ---------------------------------------------------------------------------

_CACHE_DIRS = ["/tmp/spike-home", "/tmp/spike-uv", "/tmp/spike-npm", "/tmp/spike-xdg"]

_BASE_ENV = {
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "HOME": "/tmp/spike-home",
    "UV_CACHE_DIR": "/tmp/spike-uv",
    "NPM_CONFIG_CACHE": "/tmp/spike-npm",
    "XDG_CACHE_HOME": "/tmp/spike-xdg",
}


def _ensure_dirs() -> None:
    for d in _CACHE_DIRS:
        os.makedirs(d, exist_ok=True)


# ---------------------------------------------------------------------------
# Test 1: TCP (ANTHROPIC_BASE_URL=http://127.0.0.1:<port>)
# ---------------------------------------------------------------------------


def test_tcp() -> dict:
    """Prove CLI routes API calls when ANTHROPIC_BASE_URL points to a TCP stub."""
    print("\n" + "=" * 72)
    print("TEST 1: ANTHROPIC_BASE_URL → TCP stub (http://127.0.0.1:<port>)")
    print("=" * 72)

    srv = _TcpStubServer()
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    log.info("TCP stub on 127.0.0.1:%d", srv.port)

    _ensure_dirs()
    env = {
        **_BASE_ENV,
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{srv.port}",
        "ANTHROPIC_API_KEY": "sk-ant-spike-dummy-tcp",
    }

    messages, error = asyncio.run(_run_query(env))

    srv.shutdown()
    srv.server_close()
    t.join(timeout=5)

    return _report_result("TCP", srv.received_requests, messages, error)


# ---------------------------------------------------------------------------
# Test 2: unix:// URL (ANTHROPIC_BASE_URL=unix://%2F<path>)
# ---------------------------------------------------------------------------


def test_unix_url(tmpdir: str) -> dict:
    """Probe whether ANTHROPIC_BASE_URL accepts a unix:// URL."""
    print("\n" + "=" * 72)
    print("TEST 2: ANTHROPIC_BASE_URL → unix:// URL scheme")
    print("=" * 72)

    uds_path = os.path.join(tmpdir, "advocate_url.sock")
    srv = _UnixStubServer(uds_path)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    log.info("UDS stub on %s", uds_path)

    # Bun supports unix:// URLs with URL-encoded path:
    # unix://%2Fpath%2Fto%2Fsocket  (forward slashes encoded as %2F)
    encoded_path = urllib.parse.quote(uds_path, safe="")
    unix_url = f"unix://{encoded_path}"
    log.info("unix:// URL: %s", unix_url)

    _ensure_dirs()
    env = {
        **_BASE_ENV,
        "ANTHROPIC_BASE_URL": unix_url,
        "ANTHROPIC_API_KEY": "sk-ant-spike-dummy-unix-url",
    }

    messages, error = asyncio.run(_run_query(env))

    srv.shutdown()
    srv.server_close()
    t.join(timeout=5)

    return _report_result("unix:// URL", srv.received_requests, messages, error)


# ---------------------------------------------------------------------------
# Test 3: ANTHROPIC_UNIX_SOCKET (expected: peer-session channel, NOT API proxy)
# ---------------------------------------------------------------------------


def test_unix_socket(tmpdir: str) -> dict:
    """Confirm ANTHROPIC_UNIX_SOCKET does NOT route API calls."""
    print("\n" + "=" * 72)
    print("TEST 3: ANTHROPIC_UNIX_SOCKET (peer-session channel)")
    print("=" * 72)

    uds_path = os.path.join(tmpdir, "advocate_sock.sock")
    srv = _UnixStubServer(uds_path)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    log.info("UDS stub on %s", uds_path)

    _ensure_dirs()
    env = {
        **_BASE_ENV,
        # Per the binary: ANTHROPIC_UNIX_SOCKET is the claude-ssh peer channel.
        # Set ANTHROPIC_BASE_URL to localhost TCP so the CLI has SOMEWHERE to
        # send API calls — we just want to see if UDS gets hit too.
        "ANTHROPIC_UNIX_SOCKET": uds_path,
        "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
        "ANTHROPIC_API_KEY": "sk-ant-spike-dummy-unix-sock",
    }

    # Short timeout: we expect retries to a real endpoint (dummy key), not UDS hits.
    # We'll wait for just a few messages then cancel.
    messages, error = asyncio.run(_run_query_with_timeout(env, timeout=15.0))

    srv.shutdown()
    srv.server_close()
    t.join(timeout=5)

    return _report_result("ANTHROPIC_UNIX_SOCKET", srv.received_requests, messages, error)


async def _run_query_with_timeout(agent_env: dict[str, str], timeout: float) -> tuple[list, BaseException | None]:
    """Run the query but cancel after ``timeout`` seconds."""
    from claude_agent_sdk import query
    from claude_agent_sdk.types import ClaudeAgentOptions

    options = ClaudeAgentOptions(
        tools=[],
        allowed_tools=[],
        permission_mode="dontAsk",
        env=agent_env,
        max_turns=1,
    )

    messages: list = []
    error: BaseException | None = None
    try:
        async with asyncio.timeout(timeout):
            async for msg in query(prompt="Reply: CONFIRMED", options=options):
                messages.append(msg)
                log.info("[SDK] %s / %s", type(msg).__name__, getattr(msg, "subtype", "—"))
    except TimeoutError:
        log.info("[SDK] cancelled after %.0fs (expected for this test)", timeout)
        error = TimeoutError(f"cancelled after {timeout}s")
    except Exception as exc:  # noqa: BLE001
        error = exc
        log.warning("[SDK] raised: %s: %s", type(exc).__name__, exc)
    return messages, error


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _report_result(
    name: str,
    received: list[dict],
    messages: list,
    error: BaseException | None,
) -> dict:
    print(f"\n--- {name} result ---")
    print(f"  Stub received {len(received)} request(s)")
    for i, r in enumerate(received, 1):
        safe_hdrs = {
            k: ("***" if "key" in k.lower() or "auth" in k.lower() else v) for k, v in r.get("headers", {}).items()
        }
        print(f"  [{i}] {r['method']} {r['path']}  body_keys={r.get('body_keys', [])}")
        print(f"       headers: {safe_hdrs}")

    model_hit = any(r.get("path", "").startswith("/v1/messages") for r in received)
    verdict = "CONFIRMED" if model_hit else "NOT CONFIRMED"
    print(f"  SDK messages: {len(messages)}  error: {type(error).__name__ if error else None}")
    print(f"  VERDICT: {verdict}")

    return {
        "name": name,
        "requests": len(received),
        "model_hit": model_hit,
        "verdict": verdict,
        "messages": len(messages),
        "error": str(error) if error else None,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    filter_test = sys.argv[1] if len(sys.argv) > 1 else "all"

    import claude_agent_sdk

    log.info("SDK version: %s", claude_agent_sdk.__version__)

    cli_path = Path(claude_agent_sdk.__file__).resolve().parent / "_bundled" / "claude"
    log.info("Bundled CLI: %s  (exists=%s)", cli_path, cli_path.exists())

    results = []

    with tempfile.TemporaryDirectory(prefix="spike-advocate-") as tmpdir:
        if filter_test in ("all", "tcp"):
            results.append(test_tcp())

        if filter_test in ("all", "unix_url"):
            results.append(test_unix_url(tmpdir))

        if filter_test in ("all", "unix_socket"):
            results.append(test_unix_socket(tmpdir))

    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)
    for r in results:
        print(f"  {r['name']:30s}  {r['verdict']:15s}  requests={r['requests']}  msgs={r['messages']}")
    print("=" * 72)


if __name__ == "__main__":
    main()
