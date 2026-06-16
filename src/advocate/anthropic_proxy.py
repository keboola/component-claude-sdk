from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

UPSTREAM_URL = "https://api.anthropic.com"  # HARD-CODED — never taken from agent input

# Process-lifetime idempotency cache: single-job, intentionally no eviction/TTL.
# Only successful (2xx) results are stored; transient errors fall through so retries re-attempt upstream.
_idempotency_cache: dict[str, tuple[int, dict]] = {}

_UPSTREAM_TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0)


def handle_request(payload: dict, anthropic_key: str) -> tuple[int, dict]:
    """Forward a validated payload to Anthropic, injecting the API key.

    Idempotency: if ``payload['action_id']`` has been seen before **and that
    result was a success (2xx)**, return the cached result immediately without
    a second upstream call.  Non-2xx results are NOT cached so a retry with
    the same ``action_id`` re-attempts upstream.

    Args:
        payload: Validated request dict (must contain ``action_id``).
        anthropic_key: Real Anthropic API key supplied by the server; never
            sourced from ``payload``.

    Returns:
        ``(status_code, response_body_dict)`` tuple.
    """
    action_id: str = payload["action_id"]
    if action_id in _idempotency_cache:
        log.debug("action_id %s: returning cached result", action_id)
        return _idempotency_cache[action_id]

    try:
        result = _call_upstream(payload, anthropic_key)
    except Exception:  # noqa: BLE001
        log.warning("_call_upstream raised for action_id=%s", action_id, exc_info=True)
        return 502, {"error": "upstream request failed"}

    status, _ = result
    if 200 <= status < 300:
        _idempotency_cache[action_id] = result
    return result


def _call_upstream(payload: dict, anthropic_key: str) -> tuple[int, dict]:
    """Make the actual HTTP call to Anthropic.

    The upstream URL is the module-level constant ``UPSTREAM_URL`` — it is
    NEVER taken from ``payload``.

    Failures (network errors, timeouts, non-JSON responses) are caught and
    returned as a generic ``(502, {"error": "upstream request failed"})`` so
    the agent gets a clean error and no internal detail leaks over the socket.

    Args:
        payload: Validated request dict; ``action_id`` is stripped before sending.
        anthropic_key: Real Anthropic API key.

    Returns:
        ``(status_code, response_body_dict)`` tuple.
    """
    headers = {
        "x-api-key": anthropic_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {k: v for k, v in payload.items() if k != "action_id"}
    try:
        with httpx.Client() as client:
            resp = client.post(
                f"{UPSTREAM_URL}/v1/messages",
                headers=headers,
                json=body,
                timeout=_UPSTREAM_TIMEOUT,
            )
        return resp.status_code, resp.json()
    except httpx.HTTPError as exc:
        log.warning("upstream HTTP error for action_id=%s: %s", payload.get("action_id"), type(exc).__name__)
        return 502, {"error": "upstream request failed"}
    except Exception:  # noqa: BLE001 — includes JSONDecodeError on non-JSON 5xx body
        log.warning("upstream unexpected error for action_id=%s", payload.get("action_id"), exc_info=True)
        return 502, {"error": "upstream request failed"}
