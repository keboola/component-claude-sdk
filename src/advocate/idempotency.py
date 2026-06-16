"""Shared idempotency cache for all Advocate brokers.

Keys are ``(broker_type, action_id)`` pairs so an ``action_id`` value cannot
collide across different broker types (e.g. the same string used for both an
MCP call and a GitHub call produce independent cache entries).

Only successful (2xx) results are stored; transient errors fall through so
retries re-attempt upstream.

The cache is intentionally a module-level dict with no eviction/TTL — the
Advocate process lifetime equals one Keboola job, after which the process exits
and the cache is implicitly reclaimed.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# (broker_type, action_id) → (http_status, response_body_dict)
_cache: dict[tuple[str, str], tuple[int, dict]] = {}


def get(broker_type: str, action_id: str) -> tuple[int, dict] | None:
    """Return a cached success result, or ``None`` if not cached.

    Args:
        broker_type: A short discriminator, e.g. ``"anthropic"``, ``"mcp"``,
            ``"github"``.
        action_id: The per-call idempotency token from the agent.

    Returns:
        The cached ``(status, body)`` tuple, or ``None``.
    """
    key = (broker_type, action_id)
    result = _cache.get(key)
    if result is not None:
        log.debug("idempotency hit for broker=%s action_id=%s", broker_type, action_id)
    return result


def store(broker_type: str, action_id: str, status: int, body: dict) -> None:
    """Cache a result if it is a success (2xx).

    Non-2xx results are NOT stored — a retry with the same ``action_id`` will
    re-attempt the upstream call rather than pinning a transient error forever.

    Args:
        broker_type: Same discriminator used in :func:`get`.
        action_id: The per-call idempotency token.
        status: HTTP status code from the upstream or broker.
        body: Response body dict to cache.
    """
    if 200 <= status < 300:
        _cache[(broker_type, action_id)] = (status, body)
        log.debug("idempotency stored for broker=%s action_id=%s", broker_type, action_id)


def clear() -> None:
    """Clear the entire cache (used in tests to isolate test runs)."""
    _cache.clear()
