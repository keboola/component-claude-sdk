from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterator

import httpx

from advocate.idempotency import get as _idem_get
from advocate.idempotency import store as _idem_store

log = logging.getLogger(__name__)

UPSTREAM_URL = "https://api.anthropic.com"  # HARD-CODED — never taken from agent input

# Fields that must be stripped before forwarding to Anthropic.
# ``action_id`` is a broker-internal idempotency key.
# ``context_management`` is a Claude Code CLI internal field that the Anthropic
# Messages API rejects with 400 "Extra inputs are not permitted".
_STRIP_FIELDS: frozenset[str] = frozenset({"action_id", "context_management"})

# Timeout for non-streaming calls.
_UPSTREAM_TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0)

# Timeout for streaming calls: read timeout governs inter-chunk gaps.
# Long model turns may have multi-second gaps between chunks; 120s is generous but bounded.
_UPSTREAM_STREAM_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0)


def _server_idem_key(payload: dict, query_string: str) -> str:
    """Derive a content-addressed idempotency key from the forwarded request.

    Keyed on the request content the broker actually sends upstream (with
    broker/CLI-internal fields stripped) plus the query string — NOT the
    agent-supplied ``action_id``. Mirrors the GitHub/MCP brokers (MED-3): an
    agent cannot pre-seed an ``action_id`` and have a later, different request
    return a stale cached body, because the key is derived server-side from the
    actual content rather than from an agent-controlled token.
    """
    body = {k: v for k, v in payload.items() if k not in _STRIP_FIELDS}
    canonical = json.dumps([body, query_string], sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def handle_request(payload: dict, anthropic_key: str, *, query_string: str = "") -> tuple[int, dict]:
    """Forward a validated non-streaming payload to Anthropic, injecting the API key.

    Idempotency: keyed on a server-derived hash of the forwarded request content
    (NOT the agent-supplied ``action_id``), matching the GitHub/MCP brokers
    (MED-3). If an identical request was already served with a success (2xx),
    the cached result is returned without a second upstream call. Non-2xx
    results are NOT cached, so a retry re-attempts upstream.

    Args:
        payload: Validated request dict (must contain ``action_id``).  Must
            have ``stream=False`` or ``stream`` absent.
        anthropic_key: Real Anthropic API key supplied by the server; never
            sourced from ``payload``.
        query_string: Query string from the original request path (e.g.
            ``"beta=true"``), forwarded to the upstream as-is.

    Returns:
        ``(status_code, response_body_dict)`` tuple.
    """
    idem_key = _server_idem_key(payload, query_string)
    cached = _idem_get("anthropic", idem_key)
    if cached is not None:
        return cached

    try:
        result = _call_upstream(payload, anthropic_key, query_string=query_string)
    except Exception:  # noqa: BLE001
        log.warning("_call_upstream raised (anthropic structured path)", exc_info=True)
        return 502, {"error": "upstream request failed"}

    status, body = result
    # store() only persists 2xx — transient errors fall through so a retry re-attempts upstream.
    _idem_store("anthropic", idem_key, status, body)
    return result


def handle_request_passthrough(payload: dict, anthropic_key: str, *, query_string: str = "") -> tuple[int, dict]:
    """Forward a raw (no ``action_id``) Anthropic API payload to the upstream.

    Used for the transparent-proxy path where the Claude Code CLI sends
    standard Anthropic API requests directly via ``ANTHROPIC_BASE_URL``.
    No validation, no idempotency cache — the payload is forwarded as-is
    (``action_id`` absent, all Anthropic fields preserved).

    Security properties maintained:
    - Upstream URL is hard-pinned to ``UPSTREAM_URL`` (never from payload).
    - Real API key is injected server-side; the caller never sees it.
    - Transient errors return a generic 502 with no internal detail.

    Args:
        payload: Raw Anthropic API request dict.  May contain any Anthropic
            fields; ``action_id`` is absent.
        anthropic_key: Real Anthropic API key supplied by the server.
        query_string: Query string from the original request path (e.g.
            ``"beta=true"``), forwarded to the upstream as-is.

    Returns:
        ``(status_code, response_body_dict)`` tuple.
    """
    try:
        return _call_upstream(payload, anthropic_key, query_string=query_string)
    except Exception:  # noqa: BLE001
        log.warning("_call_upstream raised in passthrough mode", exc_info=True)
        return 502, {"error": "upstream request failed"}


def _call_upstream(payload: dict, anthropic_key: str, *, query_string: str = "") -> tuple[int, dict]:
    """Make the actual HTTP call to Anthropic (non-streaming).

    The upstream URL is the module-level constant ``UPSTREAM_URL`` — it is
    NEVER taken from ``payload``.

    Failures (network errors, timeouts, non-JSON responses) are caught and
    returned as a generic ``(502, {"error": "upstream request failed"})`` so
    the agent gets a clean error and no internal detail leaks over the socket.

    Args:
        payload: Request dict; ``action_id`` is stripped before sending.
        anthropic_key: Real Anthropic API key.
        query_string: Query string to append to the upstream URL (e.g.
            ``"beta=true"``).  Never taken from ``payload``; sourced from the
            original request path parsed server-side.

    Returns:
        ``(status_code, response_body_dict)`` tuple.
    """
    headers = {
        "x-api-key": anthropic_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    # Strip broker-internal and CLI-internal fields before forwarding; set
    # stream=false so upstream always receives a clear non-streaming signal.
    body = {k: v for k, v in payload.items() if k not in _STRIP_FIELDS}
    body["stream"] = False

    # Append the original query string (e.g. ?beta=true) if present.
    upstream_url = f"{UPSTREAM_URL}/v1/messages"
    if query_string:
        upstream_url = f"{upstream_url}?{query_string}"

    try:
        with httpx.Client() as client:
            resp = client.post(
                upstream_url,
                headers=headers,
                json=body,
                timeout=_UPSTREAM_TIMEOUT,
            )
        log.info(
            "upstream response: status=%d model=%s",
            resp.status_code,
            body.get("model", "?"),
        )
        if resp.status_code >= 400:
            # Log only the status at WARNING; the raw upstream body is an
            # unbounded content sink, so keep it at DEBUG.
            log.warning("upstream error status=%d", resp.status_code)
            log.debug("upstream error body: %s", resp.text[:500])
        return resp.status_code, resp.json()
    except httpx.HTTPError as exc:
        log.warning("upstream HTTP error for action_id=%s: %s", payload.get("action_id"), type(exc).__name__)
        return 502, {"error": "upstream request failed"}
    except Exception:  # noqa: BLE001 — includes JSONDecodeError on non-JSON 5xx body
        log.warning("upstream unexpected error for action_id=%s", payload.get("action_id"), exc_info=True)
        return 502, {"error": "upstream request failed"}


def _stream_upstream(payload: dict, anthropic_key: str, *, query_string: str = "") -> Iterator[bytes]:
    """Yield raw SSE byte chunks from the Anthropic upstream.

    The upstream URL is the module-level constant ``UPSTREAM_URL`` — NEVER
    taken from ``payload``.  The real ``anthropic_key`` is injected here,
    server-side; the agent has no visibility into it.

    Callers are responsible for catching errors from iteration.  The generator
    itself only raises on setup failure (before the first byte is yielded);
    mid-stream errors propagate as ``httpx.HTTPError`` or ``Exception`` during
    iteration so the caller can emit a sanitized SSE error event.

    Args:
        payload: Request dict; ``action_id`` is stripped before sending.
        anthropic_key: Real Anthropic API key (injected server-side).
        query_string: Query string to append to the upstream URL (e.g.
            ``"beta=true"``).  Never taken from ``payload``; sourced from the
            original request path parsed server-side.

    Yields:
        Raw SSE bytes as received from the upstream, chunk by chunk.
    """
    headers = {
        "x-api-key": anthropic_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
        "accept": "text/event-stream",
    }
    body = {k: v for k, v in payload.items() if k not in _STRIP_FIELDS}

    # Append the original query string (e.g. ?beta=true) if present.
    upstream_url = f"{UPSTREAM_URL}/v1/messages"
    if query_string:
        upstream_url = f"{upstream_url}?{query_string}"

    with httpx.Client() as client:
        with client.stream(
            "POST",
            upstream_url,
            headers=headers,
            json=body,
            timeout=_UPSTREAM_STREAM_TIMEOUT,
        ) as resp:
            resp.raise_for_status()
            yield from resp.iter_bytes()
