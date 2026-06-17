"""Phase 0 Intent Contract — derive, sign, and verify.

The contract is derived **once**, before any untrusted data enters the agent,
from trusted config inputs only (system_prompt, task, declared tools, MCP
servers, GitHub toggles).  It is then **frozen** — never expanded at runtime.

The contract is HMAC-SHA256 signed with a per-invocation secret the Advocate
generates and holds (``os.urandom(32)``).  The secret is never exposed to the
agent.  ``verify()`` detects any post-derivation tampering (important for Phase
6 where a contract + transcript may cross an agent boundary).

Shape (spec §7.1)::

    {
        "scope": {
            "repos": ["org/repo-X"],           # from config.operates_on (when available)
            "writable_branches": ["agent/*"]    # default; narrowed by config if present
        },
        "capabilities": ["gh.read", ...],      # derived from config signals
        "destinations": ["api.github.com/...","anthropic(via-proxy)", ...],
        "irreversible_gate": ["gh.merge", "deploy", "delete"],
        "expiry": "this_invocation"
    }

Repo scope — ``operates_on`` (spec §10):
    ``Configuration`` exposes ``operates_on: "org/repo"`` and rejects
    ``github_enabled`` without it (UserException at parse time).  ``derive_contract``
    grants GitHub capabilities ONLY when ``operates_on`` is present and scopes the
    destination to ``api.github.com/repos/<operates_on>``; without it ALL GitHub
    capabilities are withheld (fail-closed).  The repo is never inferred from the
    task prompt (untrusted once Phase 2 begins).  The gate additionally enforces
    ``scope.repos`` and ``scope.writable_branches`` on GitHub writes (see
    ``gate.py``).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from configuration import McpRemoteServer, McpStdioServer

log = logging.getLogger(__name__)


@runtime_checkable
class _ConfigProto(Protocol):
    """Structural interface that ``derive_contract`` requires from a config object.

    ``Configuration`` satisfies this automatically (duck-typed); test helpers
    and future alternative config shapes also satisfy it without inheritance.
    """

    github_enabled: bool
    mcp_servers: list[McpStdioServer | McpRemoteServer]


# ---------------------------------------------------------------------------
# Canonical capability names
# ---------------------------------------------------------------------------

CAP_GH_READ = "gh.read"
CAP_GH_WRITE_BRANCH = "gh.write_branch"
CAP_GH_OPEN_PR = "gh.open_pr"
CAP_GH_COMMENT = "gh.comment"
CAP_GH_MERGE = "gh.merge"
CAP_GH_DELETE = "gh.delete"
# Repository-settings / branch-protection writes (e.g. PATCH /repos/{o}/{r},
# branch-protection endpoints). Elevated like gh.merge/gh.delete: NOT granted by
# the default contract, so a write_branch-capable agent cannot change default_branch
# or disable branch protection (which would undermine the writable-branch scope).
CAP_GH_ADMIN = "gh.admin"

# The Anthropic model channel.  Every contract grants this: the agent's ONLY
# model channel is the loopback proxy (it cannot reach the real Anthropic
# endpoint directly), so the proxy is always reachable.  Granting it explicitly
# means the Anthropic broker can be gated like every other endpoint (HIGH-4):
# a contract that does not carry this capability (tampered / mis-derived) makes
# the Anthropic path fail closed instead of silently ungated.
CAP_ANTHROPIC = "anthropic"

# MCP capability prefix: "mcp.<server_name>"
MCP_CAP_PREFIX = "mcp."

# The Anthropic proxy is always reachable (the agent cannot call the real
# Anthropic endpoint directly — the UDS proxy is the only channel).
DEST_ANTHROPIC = "anthropic(via-proxy)"

# The pinned GitHub API host (same constant used in github_broker).
GITHUB_API_HOST = "api.github.com"

# Default writable branch pattern (matches Phase 3 default; no user config yet).
_DEFAULT_WRITABLE_BRANCHES = ["agent/*"]

# Actions that are always in the irreversible gate regardless of capabilities.
_IRREVERSIBLE_GATE = ["gh.merge", "deploy", "delete"]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def derive_contract(
    cfg: _ConfigProto,
    *,
    operates_on: str | None = None,
) -> dict:
    """Build a frozen Intent Contract from trusted config inputs.

    Args:
        cfg: The validated ``Configuration`` for this invocation.  No untrusted
            data must have been processed before this is called (spec §6 step 3).
        operates_on: Optional ``org/repo`` string identifying the repository this
            agent operates on.  When not present (current POC default), the repo
            scope is empty — capability checking still applies but the destination
            allowlist cannot be narrowed to a specific repo path.  Phase 5 should
            wire this from a future ``cfg.operates_on`` field; do NOT infer it from
            the task prompt (that is untrusted once we start Phase 2).

    Returns:
        A plain dict representing the contract.  Sign it with :func:`sign_contract`
        before storing; pass the signed envelope to the gate.
    """
    # The Anthropic model channel is always granted (the agent has no other way
    # to reach a model) — see CAP_ANTHROPIC.
    capabilities: list[str] = [CAP_ANTHROPIC]
    destinations: list[str] = [DEST_ANTHROPIC]

    # GitHub capabilities — derived from config signals.
    #
    # HIGH-3 (fail-closed repo scope): GitHub capabilities are granted ONLY when
    # a concrete ``operates_on`` repo is known.  Without it we cannot bound the
    # PAT to a single repo, so the safe default is to grant NOTHING (a hijacked
    # agent then cannot drive the real token against arbitrary repos).  The
    # broad ``api.github.com`` destination is never emitted.  ``Configuration``
    # also rejects ``github_enabled`` without ``operates_on`` at parse time
    # (UserException), so in production this branch is belt-and-suspenders.
    if cfg.github_enabled and operates_on:
        capabilities.extend([CAP_GH_READ, CAP_GH_WRITE_BRANCH, CAP_GH_OPEN_PR, CAP_GH_COMMENT])
        destinations.append(f"{GITHUB_API_HOST}/repos/{operates_on}")
    elif cfg.github_enabled and not operates_on:
        log.warning(
            "derive_contract: github_enabled but no operates_on repo — withholding ALL "
            "GitHub capabilities (fail-closed). Set operates_on='org/repo' to grant scoped access."
        )

    # MCP capabilities — one "mcp.<name>" per declared server
    for server in cfg.mcp_servers:
        cap = f"{MCP_CAP_PREFIX}{server.name}"
        capabilities.append(cap)
        # For remote servers we know the URL; add it as a destination.
        # For stdio servers the destination is local (no network destination).
        from configuration import McpRemoteServer  # noqa: PLC0415

        if isinstance(server, McpRemoteServer):
            destinations.append(server.url)

    # Repo scope
    repos: list[str] = [operates_on] if operates_on else []

    # Writable-branch scope (HIGH-3): enforced by the gate on ref-targeting
    # writes.  Defaults to ``agent/*`` (the agent may push only to its own
    # branches, never to ``main``); a config may widen it via ``writable_branches``.
    writable_branches = getattr(cfg, "writable_branches", None) or _DEFAULT_WRITABLE_BRANCHES

    contract: dict = {
        "scope": {
            "repos": repos,
            "writable_branches": list(writable_branches),
        },
        "capabilities": sorted(capabilities),
        "destinations": sorted(destinations),
        "irreversible_gate": _IRREVERSIBLE_GATE,
        "expiry": "this_invocation",
    }
    return contract


def _canonical_json(contract: dict) -> bytes:
    """Return a stable, canonical JSON encoding of the contract dict.

    Uses ``sort_keys=True`` and no whitespace so the encoding is deterministic
    regardless of dict insertion order.
    """
    return json.dumps(contract, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def sign_contract(contract: dict, secret: bytes) -> dict:
    """Return a signed envelope ``{"contract": <contract>, "signature": <hex>}``.

    The signature is HMAC-SHA256(secret, canonical-JSON(contract)).  The secret
    must be ``os.urandom(32)`` (or similar) — never a user secret, never stored,
    never exposed to the agent.

    Args:
        contract: Plain contract dict from :func:`derive_contract`.
        secret: Per-invocation random bytes held by the Advocate only.

    Returns:
        A dict with ``"contract"`` and ``"signature"`` keys.
    """
    payload = _canonical_json(contract)
    sig = hmac.new(secret, payload, hashlib.sha256).hexdigest()
    return {"contract": contract, "signature": sig}


def verify_contract(envelope: dict, secret: bytes) -> bool:
    """Return ``True`` if the envelope's signature matches and the contract is untampered.

    Args:
        envelope: Dict as produced by :func:`sign_contract`.
        secret: The same per-invocation secret used when signing.

    Returns:
        ``True`` if valid, ``False`` if tampered or malformed.
    """
    try:
        contract = envelope["contract"]
        claimed_sig = envelope["signature"]
    except (KeyError, TypeError):  # fmt: skip
        log.warning("contract verification failed: missing contract or signature")
        return False

    if not isinstance(claimed_sig, str):
        log.warning("contract verification failed: signature is not a string")
        return False

    payload = _canonical_json(contract)
    expected_sig = hmac.new(secret, payload, hashlib.sha256).hexdigest()
    # Use hmac.compare_digest to prevent timing side-channels.
    return hmac.compare_digest(expected_sig, claimed_sig)


def new_invocation_secret() -> bytes:
    """Generate a fresh per-invocation signing secret (32 bytes of CSPRNG output).

    Called once by the Advocate at startup (Phase 5 will wire this into the boot
    sequence).  The secret is held only in the Advocate process memory and is
    never written to disk, logged, or sent over the UDS.
    """
    return os.urandom(32)
