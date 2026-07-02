#!/usr/bin/env python3
"""PreToolUse guardrail: block any access to secret VALUES in secrets.json.

Contract (Claude Code PreToolUse hook):
  - stdin: JSON with `tool_name` and `tool_input`.
  - To DENY a tool call, emit JSON on stdout with:
        {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                "permissionDecision": "deny",
                                "permissionDecisionReason": "<message>"}}
    and exit 0.
  - To ALLOW, emit nothing (or permissionDecision "allow") and exit 0.

Rules:
  1. Read/Edit/Write/NotebookEdit whose target path basename is `secrets.json`
     (relative or absolute) -> DENY.
  2. Bash whose command string contains the substring `secrets.json` -> DENY.

Rationale for the blanket Bash substring block: legitimate consumers
(`uv run pytest`, `python -m component`, `docker ...`) do NOT put secrets.json
on the command line -- the Keboola datadir / VCR framework locates it by
convention at runtime. So any literal `secrets.json` on a shell command line is
illegitimate, and blocking the substring outright is the simplest correct rule.
We deliberately do NOT allow-list "viewer" commands.
"""

import json
import os
import sys

SECRETS_BASENAME = "secrets.json"

FILE_TOOLS = {"Read", "Edit", "Write", "NotebookEdit"}

FILE_MSG = (
    "secrets.json holds secret VALUES — reading/editing it is forbidden. "
    "Reference key NAMES only; the component/tests load it at runtime."
)
BASH_MSG = (
    "Do not reference secrets.json on a shell command line. Run the "
    "component/tests (python/pytest/uv/docker) which load it internally; never "
    "cat/grep/print it."
)


def deny(reason: str) -> None:
    """Emit the PreToolUse deny decision and exit."""
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    sys.exit(0)


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        # Fail open on malformed input would be unsafe; but a broken payload is
        # not a tool call we can reason about. Allow and let other layers act.
        sys.exit(0)

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}

    if tool_name in FILE_TOOLS:
        # NotebookEdit uses notebook_path; the rest use file_path.
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if isinstance(path, str) and path:
            if os.path.basename(path.rstrip("/")) == SECRETS_BASENAME:
                deny(FILE_MSG)

    elif tool_name == "Bash":
        command = tool_input.get("command", "")
        if isinstance(command, str) and SECRETS_BASENAME in command:
            deny(BASH_MSG)

    # Default: allow (emit nothing).
    sys.exit(0)


if __name__ == "__main__":
    main()
