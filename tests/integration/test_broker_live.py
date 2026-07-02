"""Live integration test: broker forwards to real Anthropic API.

Requires secrets.json at the repo root with a valid #anthropic_key.
Run with: pytest tests/integration/test_broker_live.py -v -s
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
SECRETS_FILE = REPO_ROOT / "secrets.json"


def _load_anthropic_key() -> str:
    """Load the real Anthropic API key from secrets.json (loaded at runtime, never echoed)."""
    if not SECRETS_FILE.exists():
        pytest.skip("secrets.json not present — skipping live integration test")
    with open(SECRETS_FILE, encoding="utf-8") as fh:
        secrets = json.load(fh)
    key = secrets.get("parameters", {}).get("#anthropic_key", "")
    if not key:
        pytest.skip("No #anthropic_key in secrets.json")
    return key


def test_broker_live_anthropic():
    """Start the broker with the real key; make one model call via loopback; verify 2xx."""
    import sys

    sys.path.insert(0, str(REPO_ROOT / "src"))

    from advocate.server import AdvocateServer

    key = _load_anthropic_key()

    server = AdvocateServer(key)
    server.start()
    port = server.port

    payload = json.dumps(
        {
            "action_id": "live-test-001",
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "Say 'ok'"}],
        }
    ).encode()

    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/messages",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = json.loads(exc.read())
    finally:
        server.stop()

    print(f"\nBroker live test: status={status} body={json.dumps(body)[:300]}")
    assert status == 200, f"Expected 200, got {status}: {body}"
    assert "content" in body or "error" not in body
