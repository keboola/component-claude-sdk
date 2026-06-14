"""The ``testConnection`` sync action logic (spec §5.3).

Validates ``#anthropic_key`` with ONE cheap in-process Anthropic Messages API
call (Haiku, 1 token) — NOT by spawning the agent loop. Because this is a plain
in-process HTTP request it is the one place real Anthropic traffic is
VCR-recordable (the agent loop's CLI subprocess is not — spec §7).
"""

from __future__ import annotations

import logging

from keboola.component.exceptions import UserException
from keboola.component.sync_actions import ValidationResult
from keboola.http_client import HttpClient

ANTHROPIC_API_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
# Cheapest model for the validation ping; 1 token keeps cost negligible.
TEST_MODEL = "claude-haiku-4-5"
# Bound the connection test so a hung endpoint fails fast rather than blocking the UI.
REQUEST_TIMEOUT_S = 15


def check_anthropic_connection(anthropic_key: str, http_client: HttpClient | None = None) -> ValidationResult:
    """Make one cheap Messages call to validate the key.

    Returns a ``ValidationResult`` for the UI on success; raises
    ``UserException`` on an auth/connection failure so the UI shows the error.
    """
    if not anthropic_key:
        raise UserException("No #anthropic_key provided; cannot test the connection.")

    client = http_client or HttpClient(ANTHROPIC_API_URL)
    headers = {
        "x-api-key": anthropic_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    payload = {
        "model": TEST_MODEL,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "ping"}],
    }

    try:
        response = client.post_raw("/v1/messages", headers=headers, json=payload, timeout=REQUEST_TIMEOUT_S)
    except Exception as exc:
        # A connection error / exhausted RetryError must surface as a clean exit 1,
        # not an opaque exit 2.
        raise UserException(f"Could not reach the Anthropic API: {exc}") from exc

    if response.status_code == 200:
        logging.info("Anthropic connection test succeeded.")
        return ValidationResult("Connection to the Anthropic API succeeded.")
    if response.status_code in (401, 403):
        raise UserException("Anthropic API rejected the key (authentication failed). Check #anthropic_key.")
    raise UserException(
        f"Anthropic connection test failed with HTTP {response.status_code}: {response.text[:300]}"
    )
