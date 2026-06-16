from __future__ import annotations

import json
import logging
import os
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import types

    from configuration import McpRemoteServer, McpStdioServer

log = logging.getLogger(__name__)

# Reject bodies larger than this before reading — prevents a rogue agent from
# pinning the server thread with a multi-GB payload.
_MAX_BODY_BYTES = 5_000_000

_ACTION_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_MODEL_RE = re.compile(r"^[a-zA-Z0-9._-]+$")

_ALLOWED_FIELDS = frozenset({"action_id", "model", "max_tokens", "messages", "stream", "system"})


def _validate(raw: dict) -> tuple[dict | None, str | None]:
    """Validate the untrusted request payload.

    Returns (validated_dict, None) on success or (None, error_message) on failure.
    """
    extra = set(raw.keys()) - _ALLOWED_FIELDS
    if extra:
        return None, f"unexpected field(s): {sorted(extra)}"

    action_id = raw.get("action_id")
    if not isinstance(action_id, str) or not action_id or len(action_id) > 128:
        return None, "action_id: required non-empty string, max 128 chars"
    if not _ACTION_ID_RE.match(action_id):
        return None, "action_id: only [a-zA-Z0-9_-] allowed"

    model = raw.get("model")
    if not isinstance(model, str) or not model or len(model) > 100:
        return None, "model: required non-empty string, max 100 chars"
    if not _MODEL_RE.match(model):
        return None, "model: only [a-zA-Z0-9._-] allowed"

    max_tokens = raw.get("max_tokens")
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or not (1 <= max_tokens <= 32768):
        return None, "max_tokens: required int, 1..32768"

    messages = raw.get("messages")
    if not isinstance(messages, list) or not messages or len(messages) > 100:
        return None, "messages: required non-empty list, max 100 items"
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            return None, f"messages[{i}]: must be an object"
        if msg.get("role") not in {"user", "assistant"}:
            return None, f"messages[{i}].role: must be 'user' or 'assistant'"
        content = msg.get("content")
        if not isinstance(content, str) or len(content) > 128000:
            return None, f"messages[{i}].content: must be str, max 128000 chars"

    system = raw.get("system")
    if system is not None:
        if not isinstance(system, str) or len(system) > 64000:
            return None, "system: must be str, max 64000 chars"

    stream = raw.get("stream")
    if stream is not None and not isinstance(stream, bool):
        return None, "stream: must be bool"

    validated = dict(raw)
    # Phase 3b: stream=True is now honoured for SSE passthrough.
    # Absent/false → non-streaming JSON path with idempotency cache.
    return validated, None


class _Handler(BaseHTTPRequestHandler):
    """HTTP request handler for the UDS Advocate server."""

    server: _UnixServer  # type: ignore[assignment]

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: ANN002
        log.debug(fmt, *args)

    def do_POST(self) -> None:  # noqa: N802
        """Dispatch POST requests to the appropriate broker handler."""
        if self.path == "/v1/messages":
            self._handle_anthropic()
        elif self.path == "/v1/mcp":
            self._handle_mcp()
        elif self.path == "/v1/github":
            self._handle_github()
        else:
            self._respond(404, {"error": "not found"})

    def _read_json_body(self) -> tuple[dict | None, int | None, str | None]:
        """Read, size-check, and parse the request body as JSON.

        Returns ``(payload, None, None)`` on success, or
        ``(None, status_code, error_message)`` on failure.
        """
        ct = self.headers.get("Content-Type", "")
        if not ct.startswith("application/json"):
            return None, 400, "Content-Type must be application/json"

        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError:
            return None, 400, "Content-Length must be an integer"

        if length > _MAX_BODY_BYTES:
            return None, 413, "request body too large"

        raw_body = self.rfile.read(length)
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            return None, 400, f"invalid JSON: {exc}"

        if not isinstance(payload, dict):
            return None, 400, "body must be a JSON object"

        return payload, None, None

    def _handle_anthropic(self) -> None:
        """Handle POST /v1/messages — Anthropic proxy.

        Dispatches to the streaming or non-streaming path based on ``stream``
        in the validated payload.  Both paths hard-pin the upstream to
        ``UPSTREAM_URL`` and inject the real API key server-side.
        """
        payload, err_status, err_msg = self._read_json_body()
        if payload is None:
            self._respond(err_status, {"error": err_msg})
            return

        validated, error = _validate(payload)
        if error:
            self._respond(400, {"error": error})
            return
        assert validated is not None  # _validate returns (dict, None) or (None, str)

        from advocate import anthropic_proxy  # local import to keep modules decoupled  # noqa: PLC0415

        if validated.get("stream"):
            self._handle_anthropic_stream(validated, anthropic_proxy)
        else:
            try:
                status, body = anthropic_proxy.handle_request(validated, self.server.anthropic_key)
            except Exception:  # noqa: BLE001
                log.exception("unexpected error in handle_request")
                self._respond(500, {"error": "internal server error"})
                return
            self._respond(status, body)

    def _handle_anthropic_stream(self, validated: dict, anthropic_proxy: types.ModuleType) -> None:
        """Stream an SSE response from the Anthropic upstream back to the UDS client.

        Idempotency note: streamed responses are NOT cached in the action_id
        idempotency store.  A model call has no external side-effect beyond cost
        (unlike a GitHub write), so re-sending the same stream on retry is
        acceptable.  Caching a live SSE stream for replay would require buffering
        the entire response, negating the latency benefit of streaming — the
        trade-off clearly favours skip-cache here.

        Security properties preserved on the streaming path:
        - Upstream is hard-pinned to ``UPSTREAM_URL`` (never agent-supplied).
        - Real API key injected server-side by ``_stream_upstream``; agent never
          holds or sees it.
        - Mid-stream errors are caught and surfaced as a sanitized SSE error event;
          no upstream body, exception text, or secrets leak to the agent.
        - Timeout (``_UPSTREAM_STREAM_TIMEOUT``) governs inter-chunk gaps.

        Concurrency note: HTTPServer has no ThreadingMixIn, so this method runs
        in the single server thread.  A long stream therefore serializes all other
        broker requests for its duration.  Acceptable for the single-job/single-agent
        POC; revisit if future work adds concurrent agent calls.
        """
        try:
            chunk_iter = anthropic_proxy._stream_upstream(validated, self.server.anthropic_key)
        except Exception:  # noqa: BLE001
            log.warning("_stream_upstream setup error", exc_info=True)
            self._respond(502, {"error": "upstream request failed"})
            return

        # Send SSE response headers.  Transfer-Encoding: chunked requires us to
        # write each chunk in HTTP/1.1 chunked format: <hex-size>\r\n<data>\r\n,
        # terminated by 0\r\n\r\n — this lets the client receive data incrementally
        # without a known Content-Length.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def _write_chunk(data: bytes) -> None:
            """Write one HTTP/1.1 chunked transfer-encoding chunk."""
            self.wfile.write(f"{len(data):x}\r\n".encode())
            self.wfile.write(data)
            self.wfile.write(b"\r\n")
            self.wfile.flush()

        try:
            for chunk in chunk_iter:
                if chunk:
                    _write_chunk(chunk)
            # Terminate the chunked stream
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception:  # noqa: BLE001
            # Mid-stream error: emit a sanitized SSE error event so the agent
            # gets a structured signal rather than a truncated stream or silence.
            # No exception text, upstream URL, or key is included.
            log.warning("mid-stream error from upstream", exc_info=True)
            try:
                error_event = b'event: error\ndata: {"error": "upstream stream interrupted"}\n\n'
                _write_chunk(error_event)
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except OSError:
                pass  # client disconnected; nothing more we can do

    def _handle_mcp(self) -> None:
        """Handle POST /v1/mcp — MCP broker."""
        payload, err_status, err_msg = self._read_json_body()
        if payload is None:
            self._respond(err_status, {"error": err_msg})
            return

        from advocate.brokers import mcp_broker  # noqa: PLC0415

        validated, error = mcp_broker.validate(payload)
        if error:
            self._respond(400, {"error": error})
            return

        try:
            status, body = mcp_broker.handle_request(validated, self.server.mcp_configs)
        except Exception:  # noqa: BLE001
            log.exception("unexpected error in mcp handle_request")
            self._respond(500, {"error": "internal server error"})
            return
        self._respond(status, body)

    def _handle_github(self) -> None:
        """Handle POST /v1/github — GitHub/HTTP broker."""
        payload, err_status, err_msg = self._read_json_body()
        if payload is None:
            self._respond(err_status, {"error": err_msg})
            return

        from advocate.brokers import github_broker  # noqa: PLC0415

        validated, error = github_broker.validate(payload)
        if error:
            self._respond(400, {"error": error})
            return

        try:
            status, body = github_broker.handle_request(
                validated,
                self.server.github_token,
                allowed_destinations=self.server.github_allowed_destinations,
            )
        except Exception:  # noqa: BLE001
            log.exception("unexpected error in github handle_request")
            self._respond(500, {"error": "internal server error"})
            return
        self._respond(status, body)

    def _respond(self, status: int, body: dict) -> None:
        encoded = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class _UnixServer(HTTPServer):
    """HTTPServer bound to a Unix domain socket."""

    allow_reuse_address = True

    def __init__(
        self,
        sock_path: str,
        handler: type,
        anthropic_key: str,
        *,
        mcp_configs: dict[str, McpStdioServer | McpRemoteServer] | None = None,
        github_token: str = "",
        github_allowed_destinations: list[str] | None = None,
    ) -> None:
        self.anthropic_key = anthropic_key
        self.mcp_configs: dict[str, McpStdioServer | McpRemoteServer] = mcp_configs or {}
        self.github_token = github_token
        # None = all GitHub API paths allowed (Phase 4 will narrow via contract gate);
        # empty list = deny all.
        self.github_allowed_destinations: list[str] | None = github_allowed_destinations

        # Call BaseServer.__init__ to set up internal event/flag state (serve_forever needs it).
        # We cannot call HTTPServer.__init__ because that calls server_bind → socket.bind which
        # we want to do ourselves with AF_UNIX.
        from socketserver import BaseServer  # noqa: PLC0415

        BaseServer.__init__(self, sock_path, handler)
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.bind(sock_path)
        os.chmod(sock_path, 0o777)  # noqa: S103 — intentional world-writable socket for container-internal IPC
        self.server_activate()

    def server_bind(self) -> None:
        """No-op: socket is bound in __init__."""

    def server_close(self) -> None:
        self.socket.close()


class AdvocateServer:
    """UDS HTTP server for the Advocate Broker."""

    def __init__(
        self,
        sock_path: str,
        anthropic_key: str,
        *,
        mcp_configs: dict[str, McpStdioServer | McpRemoteServer] | None = None,
        github_token: str = "",
        github_allowed_destinations: list[str] | None = None,
    ) -> None:
        """Initialise the server.

        Args:
            sock_path: Path for the Unix domain socket.
            anthropic_key: Real Anthropic API key (never written to the socket).
            mcp_configs: Mapping of server name → MCP server config.  The
                configs hold secrets (``env``/``headers``) that are injected
                into the real MCP subprocess/request server-side.
            github_token: Real GitHub PAT (never written to the socket).
            github_allowed_destinations: Optional list of allowed GitHub API
                path prefixes.  ``None`` permits any path on ``api.github.com``.
        """
        self._sock_path = sock_path
        self._anthropic_key = anthropic_key
        self._mcp_configs = mcp_configs or {}
        self._github_token = github_token
        self._github_allowed_destinations = github_allowed_destinations
        self._server: _UnixServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Bind the Unix domain socket, chmod 0o777, then start serving in a daemon thread."""
        self._server = _UnixServer(
            self._sock_path,
            _Handler,
            self._anthropic_key,
            mcp_configs=self._mcp_configs,
            github_token=self._github_token,
            github_allowed_destinations=self._github_allowed_destinations,
        )
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        log.info("AdvocateServer started on %s", self._sock_path)

    def stop(self) -> None:
        """Shut down the server, background thread, and all MCP subprocesses."""
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)
        from advocate.brokers import mcp_broker  # noqa: PLC0415

        mcp_broker.shutdown_all()
        log.info("AdvocateServer stopped")
