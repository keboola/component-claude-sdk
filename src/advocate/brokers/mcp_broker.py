"""MCP broker — Advocate-side launcher and JSON-RPC proxy for stdio MCP servers.

The agent never holds MCP secrets.  This broker:

1. Launches each ``McpStdioServer`` as a subprocess **inside the Advocate**,
   injecting the server's ``env`` secrets into the subprocess environment.
2. Exposes a single ``handle_request`` function that the UDS server calls when
   the agent sends a POST to ``/v1/mcp/<server_name>``.
3. Forwards the JSON-RPC request body to the MCP subprocess via stdin, reads
   one response line from stdout, and returns it to the agent.

Security invariants maintained here:
- The ``env`` dict of a ``McpStdioServer`` is injected into the subprocess env
  server-side; the agent sees neither the key names nor the values.
- The upstream subprocess path/command is taken from **config** (trusted), never
  from the agent request.
- All untrusted agent input is rejected before it can influence the subprocess
  launch.

For ``McpRemoteServer`` (HTTP/SSE), the broker forwards the JSON-RPC call as an
HTTP POST to the server URL, injecting ``headers`` (Bearer token etc.)
server-side.

Phase 5 note: the claude CLI's MCP config will point each server's transport at
the Advocate UDS rather than at the real subprocess/URL, so the CLI never holds
the secrets.  See Phase 5 investigation in docs/superpowers/plans/.
"""

from __future__ import annotations

import json
import logging
import os
import re
import select
import subprocess
import threading
from typing import TYPE_CHECKING

import httpx

from advocate.idempotency import get as _idem_get
from advocate.idempotency import store as _idem_store

if TYPE_CHECKING:
    from configuration import McpRemoteServer, McpStdioServer

log = logging.getLogger(__name__)

# JSON-RPC method name — only printable ASCII, no shell-special chars, max 128.
_METHOD_RE = re.compile(r"^[a-zA-Z0-9_./-]+$")
_ACTION_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

_ALLOWED_FIELDS = frozenset({"action_id", "server_name", "method", "params", "id"})
_UPSTREAM_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)

# Seconds to wait for a stdio subprocess to produce a response line.
# Matches the HTTP read timeout so all transports have a consistent budget.
_STDIO_READ_TIMEOUT_S: float = 30.0

# Per-server subprocess handles (Advocate lifetime, not per-request).
_procs: dict[str, subprocess.Popen] = {}
_proc_locks: dict[str, threading.Lock] = {}


def validate(raw: dict) -> tuple[dict | None, str | None]:
    """Validate an untrusted MCP RPC request from the agent.

    Returns ``(validated, None)`` on success or ``(None, error_message)``.
    """
    extra = set(raw.keys()) - _ALLOWED_FIELDS
    if extra:
        return None, f"unexpected field(s): {sorted(extra)}"

    action_id = raw.get("action_id")
    if not isinstance(action_id, str) or not action_id or len(action_id) > 128:
        return None, "action_id: required non-empty string, max 128 chars"
    if not _ACTION_ID_RE.match(action_id):
        return None, "action_id: only [a-zA-Z0-9_-] allowed"

    server_name = raw.get("server_name")
    if not isinstance(server_name, str) or not server_name or len(server_name) > 128:
        return None, "server_name: required non-empty string, max 128 chars"
    # Only allow characters safe in config keys — no shell metacharacters.
    if not _ACTION_ID_RE.match(server_name):
        return None, "server_name: only [a-zA-Z0-9_-] allowed"

    method = raw.get("method")
    if not isinstance(method, str) or not method or len(method) > 128:
        return None, "method: required non-empty string, max 128 chars"
    if not _METHOD_RE.match(method):
        return None, "method: only printable ASCII [a-zA-Z0-9_./-] allowed"

    # ``params`` is optional; if present it must be a JSON-serialisable dict.
    params = raw.get("params")
    if params is not None and not isinstance(params, dict):
        return None, "params: must be a JSON object when present"

    # ``id`` is the JSON-RPC request id (int or str); optional for notifications.
    rpc_id = raw.get("id")
    if rpc_id is not None and not isinstance(rpc_id, (int, str)):
        return None, "id: must be int or str when present"

    return dict(raw), None


def handle_request(
    payload: dict,
    mcp_configs: dict[str, McpStdioServer | McpRemoteServer],
) -> tuple[int, dict]:
    """Dispatch a validated MCP RPC to the appropriate server.

    Args:
        payload: Validated dict from :func:`validate`.
        mcp_configs: Mapping of server name → config object (held by the
            Advocate; never sourced from agent input).

    Returns:
        ``(status_code, response_body_dict)`` tuple.
    """
    action_id: str = payload["action_id"]
    cached = _idem_get("mcp", action_id)
    if cached is not None:
        return cached

    server_name: str = payload["server_name"]
    cfg = mcp_configs.get(server_name)
    if cfg is None:
        # Never echo the server_name to the agent in a message that could leak
        # configured server names — a generic "not found" is sufficient.
        return 404, {"error": "MCP server not found"}

    try:
        from configuration import McpRemoteServer, McpStdioServer  # noqa: PLC0415

        if isinstance(cfg, McpStdioServer):
            result = _call_stdio(payload, cfg)
        elif isinstance(cfg, McpRemoteServer):
            result = _call_remote(payload, cfg)
        else:
            log.warning("unknown MCP server config type for action_id=%s", action_id)
            return 500, {"error": "internal server error"}
    except Exception:  # noqa: BLE001
        log.warning("MCP broker error for action_id=%s", action_id, exc_info=True)
        return 502, {"error": "MCP request failed"}

    status, body = result
    _idem_store("mcp", action_id, status, body)
    return result


# ---------------------------------------------------------------------------
# stdio transport — subprocess launched with injected secrets
# ---------------------------------------------------------------------------


def _get_or_launch_proc(cfg: McpStdioServer) -> subprocess.Popen:
    """Return the running subprocess for ``cfg``, launching it if needed.

    The ``cfg.env`` secrets are injected into the subprocess environment here,
    server-side.  The agent never sees them.
    """
    name = cfg.name
    if name not in _proc_locks:
        _proc_locks[name] = threading.Lock()

    with _proc_locks[name]:
        proc = _procs.get(name)
        if proc is None or proc.poll() is not None:
            # Build the subprocess env: inherit the Advocate's env, then overlay
            # the server-specific secrets.  NEVER forward this env to the agent.
            env = {**os.environ, **cfg.env}
            log.debug("launching MCP stdio server %s: %s %s", name, cfg.command, cfg.args)
            proc = subprocess.Popen(  # noqa: S603
                [cfg.command, *cfg.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                # DEVNULL prevents a server writing >~64KB to stderr from
                # blocking the stdout readline via pipe buffer deadlock.
                stderr=subprocess.DEVNULL,
                env=env,
                text=False,
            )
            _procs[name] = proc
        return proc


def _evict_proc(name: str) -> None:
    """Terminate and remove a broken/timed-out subprocess from ``_procs``."""
    proc = _procs.pop(name, None)
    if proc is not None:
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            log.warning("could not terminate MCP server %s during eviction", name, exc_info=True)


def _call_stdio(
    payload: dict,
    cfg: McpStdioServer,
) -> tuple[int, dict]:
    """Send one JSON-RPC request to a stdio MCP server and return the response.

    The stdout read is bounded by ``_STDIO_READ_TIMEOUT_S`` using
    ``select.select``.  If the subprocess does not respond in time it is
    evicted so the next call relaunches a fresh one.
    """
    proc = _get_or_launch_proc(cfg)

    rpc = {
        "jsonrpc": "2.0",
        "method": payload["method"],
        "id": payload.get("id", 1),
    }
    if payload.get("params") is not None:
        rpc["params"] = payload["params"]

    line = (json.dumps(rpc) + "\n").encode()

    with _proc_locks[cfg.name]:
        if proc.stdin is None or proc.stdout is None:
            return 502, {"error": "MCP request failed"}
        try:
            proc.stdin.write(line)
            proc.stdin.flush()
        except OSError:
            log.warning("stdio MCP server %s write error", cfg.name, exc_info=True)
            _evict_proc(cfg.name)
            return 502, {"error": "MCP request failed"}

        # Bound the read with select so a hung server does not block the lock
        # indefinitely and wedge every subsequent request for this server.
        ready, _, _ = select.select([proc.stdout], [], [], _STDIO_READ_TIMEOUT_S)
        if not ready:
            log.warning(
                "stdio MCP server %s timed out after %.1fs — evicting",
                cfg.name,
                _STDIO_READ_TIMEOUT_S,
            )
            _evict_proc(cfg.name)
            return 502, {"error": "MCP request failed"}

        try:
            response_line = proc.stdout.readline()
        except OSError:
            log.warning("stdio MCP server %s read error", cfg.name, exc_info=True)
            _evict_proc(cfg.name)
            return 502, {"error": "MCP request failed"}

    if not response_line:
        log.warning("stdio MCP server %s returned empty response", cfg.name)
        _evict_proc(cfg.name)
        return 502, {"error": "MCP request failed"}

    try:
        resp_obj = json.loads(response_line)
    except json.JSONDecodeError:
        log.warning("stdio MCP server %s returned non-JSON", cfg.name)
        return 502, {"error": "MCP request failed"}

    return 200, resp_obj


# ---------------------------------------------------------------------------
# HTTP/SSE transport — Bearer token injected server-side
# ---------------------------------------------------------------------------


def _call_remote(
    payload: dict,
    cfg: McpRemoteServer,
) -> tuple[int, dict]:
    """Forward a JSON-RPC call to an HTTP/SSE MCP server with injected headers.

    The ``cfg.headers`` (containing Bearer tokens etc.) are injected here,
    server-side.  The agent never sees them.

    The destination URL is taken from **config** (trusted), never from the agent.
    """
    rpc = {
        "jsonrpc": "2.0",
        "method": payload["method"],
        "id": payload.get("id", 1),
    }
    if payload.get("params") is not None:
        rpc["params"] = payload["params"]

    # Inject secret headers server-side; agent supplied none.
    headers = {
        "content-type": "application/json",
        **cfg.headers,
    }

    try:
        with httpx.Client() as client:
            resp = client.post(cfg.url, headers=headers, json=rpc, timeout=_UPSTREAM_TIMEOUT)
        return resp.status_code, resp.json()
    except httpx.HTTPError:
        log.warning("remote MCP server %s HTTP error", cfg.name, exc_info=True)
        return 502, {"error": "MCP request failed"}
    except Exception:  # noqa: BLE001
        log.warning("remote MCP server %s unexpected error", cfg.name, exc_info=True)
        return 502, {"error": "MCP request failed"}


# ---------------------------------------------------------------------------
# Lifecycle helpers (called by the Advocate on shutdown)
# ---------------------------------------------------------------------------


def shutdown_all() -> None:
    """Terminate all running MCP subprocess servers."""
    for name, proc in list(_procs.items()):
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            log.warning("could not terminate MCP server %s", name, exc_info=True)
    _procs.clear()
