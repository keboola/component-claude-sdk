from __future__ import annotations

import json
import logging
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import types

    from configuration import McpRemoteServer, McpStdioServer

log = logging.getLogger(__name__)

# Sentinel: when the server holds no contract envelope, gating is skipped
# (Phase 3 default behaviour preserved for tests that pre-date Phase 4).
_NO_CONTRACT = None

# Reject bodies larger than this before reading — prevents a rogue agent from
# pinning the server thread with a multi-GB payload.
_MAX_BODY_BYTES = 5_000_000

_ACTION_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_MODEL_RE = re.compile(r"^[a-zA-Z0-9._-]+$")

# Fields that the structured-agent path allows (action_id required).
_ALLOWED_FIELDS = frozenset({"action_id", "model", "max_tokens", "messages", "stream", "system"})

# Path suffixes that indicate a merge operation (case-insensitive).
_MERGE_PATH_SUFFIXES = ("/merge", "/merges")


def _github_repo_from_path(path: str) -> str | None:
    """Extract ``owner/repo`` from a GitHub REST path like ``/repos/owner/repo/...``.

    Returns ``None`` for paths that are not repo-scoped (e.g. ``/user``,
    ``/search/...``).  Used to feed the contract's ``scope.repos`` check so a
    GitHub call is bound to the declared repo, not just to ``api.github.com``.
    """
    parts = path.strip("/").split("/")
    if len(parts) >= 3 and parts[0] == "repos":
        return f"{parts[1]}/{parts[2]}"
    return None


def _github_write_branch(method: str, path: str, body: dict | None) -> str | None:
    """Return the target branch for a ref-targeting GitHub write, or ``None``.

    Recognises the REST shapes that create/move/delete a branch ref or commit to
    a branch — exactly the "push to a protected branch" vector:

    - ``PATCH``/``DELETE`` ``…/git/refs/heads/<branch>``  (update/force / delete ref)
    - ``POST`` ``…/git/refs``        with ``body.ref = "refs/heads/<branch>"``  (create ref)
    - ``PUT`` ``…/contents/<path>``  with ``body.branch = "<branch>"``           (commit to branch)

    Returns ``None`` for reads and for writes that do not name a branch (those
    remain bounded by capability + repo scope).  Branch-level gating of raw
    ``git`` CLI pushes is a CLI-routing follow-on (V0 brokers REST only).
    """
    if method == "GET":
        return None
    norm = path.rstrip("/")
    marker = "/git/refs/heads/"
    idx = norm.find(marker)
    if idx != -1:
        return norm[idx + len(marker) :] or None
    body = body or {}
    ref = body.get("ref")
    if isinstance(ref, str) and ref.startswith("refs/heads/"):
        return ref[len("refs/heads/") :] or None
    branch = body.get("branch")
    if isinstance(branch, str) and branch:
        return branch
    return None


def _github_capability(method: str, path: str) -> str:
    """Map an HTTP method + GitHub API path to the narrowest capability name.

    The mapping is intentionally conservative: destructive and irreversible
    operations require capabilities (``gh.delete``, ``gh.merge``) that the
    default derived contract does NOT grant, so they hard-deny by default.

    Mapping:
    - GET             → ``gh.read``
    - DELETE          → ``gh.delete``       (destructive — not covered by write_branch)
    - PUT/POST to a
      ``…/merge(s)``  → ``gh.merge``        (irreversible — not covered by write_branch)
    - other writes    → ``gh.write_branch``
    """
    if method == "GET":
        return "gh.read"
    if method == "DELETE":
        return "gh.delete"
    # Merge detection: path ends with /merge or /merges (e.g. /pulls/{n}/merge).
    norm_path = path.rstrip("/").lower()
    if method in ("PUT", "POST") and any(norm_path.endswith(s) for s in _MERGE_PATH_SUFFIXES):
        return "gh.merge"
    return "gh.write_branch"


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
    """HTTP request handler for the TCP Advocate server."""

    server: _TcpServer  # type: ignore[assignment]

    def log_message(self, format: str, *args: object) -> None:  # noqa: ANN002  # type: ignore[override]
        log.debug(format, *args)

    def do_POST(self) -> None:  # noqa: N802
        """Dispatch POST requests to the appropriate broker handler.

        The Claude Code CLI appends query parameters to the path
        (e.g. ``/v1/messages?beta=true``).  We strip the query string for
        routing only; the query string is forwarded to the upstream unchanged.
        """
        log.info("broker: POST %s", self.path)
        # Strip query string for routing (preserve for upstream forwarding).
        parsed = urllib.parse.urlparse(self.path)
        route_path = parsed.path
        if route_path == "/v1/messages":
            self._handle_anthropic()
        elif route_path == "/v1/mcp":
            self._handle_mcp()
        elif route_path == "/v1/github":
            self._handle_github()
        else:
            log.warning("broker: unhandled path %s — returning 404", self.path)
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

        Two request modes are supported:

        **Structured-agent mode** (``action_id`` present in body): enforces the
        strict field allowlist and idempotency semantics.  Used by code that
        calls the broker directly as a structured RPC (test suite, future SDK
        integration).

        **Transparent-proxy mode** (no ``action_id``): passes the full payload
        straight through to the Anthropic upstream.  Used when the Claude Code
        CLI hits the broker via ``ANTHROPIC_BASE_URL`` — it sends standard
        Anthropic API payloads with no ``action_id``.  The query string from
        the original request path (e.g. ``?beta=true``) is forwarded unchanged.

        Both paths hard-pin the upstream to ``UPSTREAM_URL`` and inject the
        real API key server-side.  The agent never sees or controls the key.
        """
        payload, err_status, err_msg = self._read_json_body()
        if payload is None:
            assert err_status is not None  # _read_json_body guarantees: payload None ↔ status set
            self._respond(err_status, {"error": err_msg})
            return

        # Gate the Anthropic endpoint too (HIGH-4) — MCP/GitHub both gate; this
        # path must not be the one ungated hole.  The upstream is hard-pinned to
        # api.anthropic.com so the blast radius is cost/quota (not exfil), but a
        # contract that does not carry the ``anthropic`` capability (tampered /
        # mis-derived) must fail closed rather than silently inject the real key.
        # No-op when no contract is configured (Phase 3 backward compat).
        from advocate.contract import DEST_ANTHROPIC  # noqa: PLC0415

        if not self._check_gate("anthropic", DEST_ANTHROPIC):
            return

        from advocate import anthropic_proxy  # local import to keep modules decoupled  # noqa: PLC0415

        # Preserve the query string from the original request path so upstream
        # features that rely on it (e.g. ?beta=true) continue to work.
        parsed = urllib.parse.urlparse(self.path)
        query_string = parsed.query  # e.g. "beta=true" or ""

        if "action_id" in payload:
            # Structured-agent path: enforce strict validation + idempotency.
            validated, error = _validate(payload)
            if error:
                self._respond(400, {"error": error})
                return
            assert validated is not None  # _validate returns (dict, None) or (None, str)
            if validated.get("stream"):
                self._handle_anthropic_stream(validated, anthropic_proxy, query_string=query_string)
            else:
                try:
                    status, body = anthropic_proxy.handle_request(
                        validated, self.server.anthropic_key, query_string=query_string
                    )
                except Exception:  # noqa: BLE001
                    log.exception("unexpected error in handle_request")
                    self._respond(500, {"error": "internal server error"})
                    return
                self._respond(status, body)
        else:
            # Transparent-proxy path: forward full payload to Anthropic as-is.
            # No action_id → no idempotency cache; safe because model calls are
            # idempotent from the user's perspective (same input → same cost).
            log.debug("broker: transparent-proxy request for model=%s", payload.get("model", "?"))
            if payload.get("stream"):
                self._handle_anthropic_stream(payload, anthropic_proxy, query_string=query_string)
            else:
                try:
                    status, body = anthropic_proxy.handle_request_passthrough(
                        payload, self.server.anthropic_key, query_string=query_string
                    )
                except Exception:  # noqa: BLE001
                    log.exception("unexpected error in handle_request_passthrough")
                    self._respond(500, {"error": "internal server error"})
                    return
                self._respond(status, body)

    def _handle_anthropic_stream(
        self, validated: dict, anthropic_proxy: types.ModuleType, *, query_string: str = ""
    ) -> None:
        """Stream an SSE response from the Anthropic upstream back to the TCP client.

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
            chunk_iter = anthropic_proxy._stream_upstream(
                validated, self.server.anthropic_key, query_string=query_string
            )
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

    def _check_gate(
        self,
        capability: str,
        destination: str,
        *,
        scope_repo: str | None = None,
        write_branch: str | None = None,
    ) -> bool:
        """Run the contract gate for a single RPC.  Return True if allowed.

        The gate is a no-op **only** when BOTH ``contract_envelope`` and
        ``contract_signing_secret`` are ``None`` — i.e. the server was
        intentionally started without a contract (Phase 3 backward compat).

        If either is present without the other, the gate FAILS CLOSED (denies
        the request) rather than silently allowing it.  This ensures a wiring
        slip (e.g. passing an envelope but forgetting the secret) is loud and
        safe rather than quietly fail-open.

        Returns ``True`` if the action passes (or no contract is configured);
        responds with 403 and returns ``False`` if denied.
        """
        envelope = self.server.contract_envelope
        secret = self.server.contract_signing_secret

        # No contract configured → gate is a no-op (Phase 3 default).
        if envelope is _NO_CONTRACT and secret is None:
            return True

        # Envelope present but no secret (or vice versa) → wiring slip → deny.
        if envelope is _NO_CONTRACT or secret is None:
            log.error(
                "gate: envelope/secret mismatch — one is set but not the other; "
                "failing closed to avoid a silently unenforced gate"
            )
            self._respond(403, {"error": "contract configuration error"})
            return False

        from advocate import contract as _contract  # noqa: PLC0415
        from advocate.gate import GateDenial, check_action  # noqa: PLC0415

        if not _contract.verify_contract(envelope, secret):
            log.warning("gate: contract signature verification failed — denying request")
            self._respond(403, {"error": "contract verification failed"})
            return False

        result = check_action(
            envelope["contract"],
            capability=capability,
            destination=destination,
            scope_repo=scope_repo,
            write_branch=write_branch,
        )
        if isinstance(result, GateDenial):
            self._respond(403, {"error": result.reason})
            return False
        return True

    def _handle_mcp(self) -> None:
        """Handle POST /v1/mcp — MCP broker."""
        payload, err_status, err_msg = self._read_json_body()
        if payload is None:
            assert err_status is not None  # _read_json_body guarantees: payload None ↔ status set
            self._respond(err_status, {"error": err_msg})
            return

        from advocate.brokers import mcp_broker  # noqa: PLC0415

        validated, error = mcp_broker.validate(payload)
        if error:
            self._respond(400, {"error": error})
            return
        assert validated is not None  # mcp_broker.validate returns (dict, None) or (None, str)

        # Gate: derive capability from server_name (mcp.<name>), destination
        # from the configured server URL (or "mcp-stdio" for local servers).
        server_name: str = validated.get("server_name", "")
        cap = f"mcp.{server_name}"
        cfg = self.server.mcp_configs.get(server_name)
        if cfg is not None:
            from configuration import McpRemoteServer  # noqa: PLC0415

            dest = cfg.url if isinstance(cfg, McpRemoteServer) else f"mcp-stdio:{server_name}"
        else:
            dest = f"mcp-stdio:{server_name}"

        if not self._check_gate(cap, dest):
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
            assert err_status is not None  # _read_json_body guarantees: payload None ↔ status set
            self._respond(err_status, {"error": err_msg})
            return

        from advocate.brokers import github_broker  # noqa: PLC0415

        validated, error = github_broker.validate(payload)
        if error:
            self._respond(400, {"error": error})
            return
        assert validated is not None  # github_broker.validate returns (dict, None) or (None, str)

        # Gate: map HTTP method + path to the narrowest capability that covers
        # the operation.  The default contract grants only gh.read and
        # gh.write_branch, so destructive ops (gh.delete, gh.merge) hard-deny
        # unless the contract was explicitly widened.
        #
        # Mapping:
        #   GET                           → gh.read
        #   DELETE                        → gh.delete   (NOT write_branch — destructive)
        #   PUT/POST to …/merge(s)        → gh.merge    (NOT write_branch — irreversible)
        #   POST/PATCH/PUT (other writes) → gh.write_branch
        method: str = validated.get("method", "GET")
        path: str = validated.get("path", "")
        cap = _github_capability(method, path)
        dest = f"{github_broker.GITHUB_API_HOST}{path}"
        # HIGH-3: bind the call to the contract's repo scope and writable-branch
        # scope, not just to api.github.com.  scope_repo enforces scope.repos;
        # write_branch enforces scope.writable_branches on ref-targeting writes.
        scope_repo = _github_repo_from_path(path)
        write_branch = _github_write_branch(method, path, validated.get("body"))

        if not self._check_gate(cap, dest, scope_repo=scope_repo, write_branch=write_branch):
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


class _TcpServer(HTTPServer):
    """HTTPServer bound to loopback TCP — never 0.0.0.0; no external exposure."""

    # Bind to loopback only — never 0.0.0.0; no external exposure.
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler: type,
        anthropic_key: str,
        *,
        mcp_configs: dict[str, McpStdioServer | McpRemoteServer] | None = None,
        github_token: str = "",
        github_allowed_destinations: list[str] | None = None,
        contract_envelope: dict | None = None,
        contract_signing_secret: bytes | None = None,
    ) -> None:
        self.anthropic_key = anthropic_key
        self.mcp_configs: dict[str, McpStdioServer | McpRemoteServer] = mcp_configs or {}
        self.github_token = github_token
        # None = all GitHub API paths allowed (when no contract is set);
        # the contract gate narrows this when a contract_envelope is provided.
        self.github_allowed_destinations: list[str] | None = github_allowed_destinations
        # Phase 4: signed Intent Contract + secret for per-request gating.
        # _NO_CONTRACT (None) = gate is a no-op (Phase 3 default; preserves
        # backward compatibility for tests that pre-date Phase 4).
        self.contract_envelope: dict | None = contract_envelope
        self.contract_signing_secret: bytes | None = contract_signing_secret

        HTTPServer.__init__(self, server_address, handler)


class AdvocateServer:
    """Loopback TCP HTTP server for the Advocate Broker."""

    def __init__(
        self,
        anthropic_key: str,
        *,
        mcp_configs: dict[str, McpStdioServer | McpRemoteServer] | None = None,
        github_token: str = "",
        github_allowed_destinations: list[str] | None = None,
        contract_envelope: dict | None = None,
        contract_signing_secret: bytes | None = None,
    ) -> None:
        """Initialise the server.

        Args:
            anthropic_key: Real Anthropic API key (never written to the socket).
            mcp_configs: Mapping of server name → MCP server config.  The
                configs hold secrets (``env``/``headers``) that are injected
                into the real MCP subprocess/request server-side.
            github_token: Real GitHub PAT (never written to the socket).
            github_allowed_destinations: Optional list of allowed GitHub API
                path prefixes.  ``None`` permits any path on ``api.github.com``.
            contract_envelope: Signed Intent Contract envelope from
                :func:`~advocate.contract.sign_contract`.  When ``None`` the
                gate is a no-op (backward-compatible default).
            contract_signing_secret: The per-invocation HMAC secret used to
                sign ``contract_envelope``.  Must be provided together with
                ``contract_envelope``; ignored when ``contract_envelope`` is
                ``None``.
        """
        # Validate envelope/secret are either both set or both absent.
        # A mismatch is a programming error — fail loudly at construction time
        # rather than silently at the first gated request.
        if (contract_envelope is None) != (contract_signing_secret is None):
            raise ValueError(
                "contract_envelope and contract_signing_secret must both be "
                "provided together or both omitted; "
                "providing one without the other would silently disable the gate"
            )
        self._anthropic_key = anthropic_key
        self._mcp_configs = mcp_configs or {}
        self._github_token = github_token
        self._github_allowed_destinations = github_allowed_destinations
        self._contract_envelope = contract_envelope
        self._contract_signing_secret = contract_signing_secret
        self._server: _TcpServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Bind to 127.0.0.1:0 (OS-assigned port), then start serving in a daemon thread."""
        self._server = _TcpServer(
            ("127.0.0.1", 0),
            _Handler,
            self._anthropic_key,
            mcp_configs=self._mcp_configs,
            github_token=self._github_token,
            github_allowed_destinations=self._github_allowed_destinations,
            contract_envelope=self._contract_envelope,
            contract_signing_secret=self._contract_signing_secret,
        )
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        log.info("AdvocateServer started on 127.0.0.1:%d", self.port)

    @property
    def port(self) -> int:
        """Return the bound TCP port (available after start() is called)."""
        if self._server is None:
            raise RuntimeError("server not started")
        return self._server.server_address[1]

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
