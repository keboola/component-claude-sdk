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
    pass

log = logging.getLogger(__name__)

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
    validated["stream"] = False  # Phase 2: non-streaming only
    return validated, None


class _Handler(BaseHTTPRequestHandler):
    """HTTP request handler for the UDS Advocate server."""

    server: _UnixServer  # type: ignore[assignment]

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: ANN002
        log.debug(fmt, *args)

    def do_POST(self) -> None:  # noqa: N802
        """Handle POST /v1/messages."""
        if self.path != "/v1/messages":
            self._respond(404, {"error": "not found"})
            return

        ct = self.headers.get("Content-Type", "")
        if not ct.startswith("application/json"):
            self._respond(400, {"error": "Content-Type must be application/json"})
            return

        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length)
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            self._respond(400, {"error": f"invalid JSON: {exc}"})
            return

        if not isinstance(payload, dict):
            self._respond(400, {"error": "body must be a JSON object"})
            return

        validated, error = _validate(payload)
        if error:
            self._respond(400, {"error": error})
            return

        from advocate import anthropic_proxy  # local import to keep modules decoupled

        status, body = anthropic_proxy.handle_request(validated, self.server.anthropic_key)
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

    def __init__(self, sock_path: str, handler: type, anthropic_key: str) -> None:
        self.anthropic_key = anthropic_key
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

    def __init__(self, sock_path: str, anthropic_key: str) -> None:
        """Initialise the server.

        Args:
            sock_path: Path for the Unix domain socket.
            anthropic_key: Real Anthropic API key (never written to the socket).
        """
        self._sock_path = sock_path
        self._anthropic_key = anthropic_key
        self._server: _UnixServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Bind the Unix domain socket, chmod 0o777, then start serving in a daemon thread."""
        self._server = _UnixServer(self._sock_path, _Handler, self._anthropic_key)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        log.info("AdvocateServer started on %s", self._sock_path)

    def stop(self) -> None:
        """Shut down the server and background thread."""
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)
        log.info("AdvocateServer stopped")
