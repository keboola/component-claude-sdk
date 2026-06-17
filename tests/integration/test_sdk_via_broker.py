"""End-to-end integration test: SDK query loop via broker.

Tests the full path: claude_agent_sdk.query() → broker → Anthropic → result.

Requires secrets.json at the repo root with a valid #anthropic_key.
Run with: pytest tests/integration/test_sdk_via_broker.py -v -s
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
SECRETS_FILE = REPO_ROOT / "secrets.json"

_DUMMY_KEY = "sk-ant-dummy-key-00000000000000000000000000000000000000000000000000"


def _load_anthropic_key() -> str:
    if not SECRETS_FILE.exists():
        pytest.skip("secrets.json not present")
    with open(SECRETS_FILE, encoding="utf-8") as fh:
        secrets = json.load(fh)
    key = secrets.get("parameters", {}).get("#anthropic_key", "")
    if not key:
        pytest.skip("No #anthropic_key in secrets.json")
    return key


def test_sdk_query_via_broker() -> None:
    """Run one SDK query turn with ANTHROPIC_BASE_URL pointing at the broker."""
    import os
    import sys

    sys.path.insert(0, str(REPO_ROOT / "src"))

    from claude_agent_sdk import ClaudeAgentOptions, query

    from advocate.server import AdvocateServer

    key = _load_anthropic_key()

    server = AdvocateServer(key)
    server.start()
    port = server.port
    print(f"\n[SERVER] Broker on 127.0.0.1:{port}", flush=True)

    # Inject the broker URL via env so the SDK subprocess picks it up.
    # The dummy key is overridden by the broker server-side.
    os.environ["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port}"
    os.environ["ANTHROPIC_API_KEY"] = _DUMMY_KEY

    result_message = None
    error: Exception | None = None

    async def _run() -> None:
        nonlocal result_message, error
        try:
            options = ClaudeAgentOptions(
                model="claude-haiku-4-5-20251001",
                max_turns=1,
                permission_mode="dontAsk",
                allowed_tools=["Write"],
            )
            async for msg in query(prompt="say ok", options=options):
                msg_type = type(msg).__name__
                print(f"  [MSG] {msg_type}", flush=True)
                if msg_type == "ResultMessage":
                    result_message = msg
        except Exception as exc:
            error = exc

    try:
        asyncio.run(_run())
    finally:
        server.stop()
        # Restore env so other tests aren't affected
        os.environ.pop("ANTHROPIC_BASE_URL", None)
        os.environ.pop("ANTHROPIC_API_KEY", None)

    if error:
        pytest.fail(f"SDK query raised: {error}")

    assert result_message is not None, "No ResultMessage received"
    print(
        f"  [RESULT] is_error={getattr(result_message, 'is_error', '?')} subtype={getattr(result_message, 'subtype', '?')}",
        flush=True,
    )
    print(f"  [RESULT] result={getattr(result_message, 'result', '?')}", flush=True)

    assert not getattr(result_message, "is_error", True), (
        f"ResultMessage has is_error=True (subtype={getattr(result_message, 'subtype', '?')})"
    )
