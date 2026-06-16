from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

UPSTREAM_URL = "https://api.anthropic.com"  # HARD-CODED — never taken from agent input

_idempotency_cache: dict[str, tuple[int, dict]] = {}


def handle_request(payload: dict, anthropic_key: str) -> tuple[int, dict]:
    """Forward a validated payload to Anthropic, injecting the API key.

    Idempotency: if ``payload['action_id']`` has been seen before, return the
    cached result immediately without a second upstream call.

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

    result = _call_upstream(payload, anthropic_key)
    _idempotency_cache[action_id] = result
    return result


def _call_upstream(payload: dict, anthropic_key: str) -> tuple[int, dict]:
    """Make the actual HTTP call to Anthropic.

    The upstream URL is the module-level constant ``UPSTREAM_URL`` — it is
    NEVER taken from ``payload``.

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
    with httpx.Client() as client:
        resp = client.post(
            f"{UPSTREAM_URL}/v1/messages",
            headers=headers,
            json=body,
            timeout=60,
        )
    return resp.status_code, resp.json()
