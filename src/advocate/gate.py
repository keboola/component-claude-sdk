"""Phase 1 deterministic Contract gate (spec §7.2).

The gate is called by the Advocate **before** every agent RPC.  It is purely
deterministic — no LLM, no network, no fuzzy logic.  A request is allowed only
if **all** of:

    capability ∈ contract["capabilities"]
    destination passes segment-boundary matching against contract["destinations"]
    scope is respected (when a repos list is non-empty)

Anything not explicitly allowed → hard deny.

A denial is returned as a clean ``GateDenial`` result carrying a sanitized
message suitable for surfacing to the agent as a tool error.  No internal
contract detail, no secrets, and no exception traceback are included.

Destination matching (spec §7.2 — reuses github_broker boundary logic):
    Uses the same path-segment boundary algorithm as ``github_broker._path_allowed``:
    a destination is allowed if the requested destination is an exact match or
    is a child path (prefix + ``/``).  This prevents the classic naive-startswith
    prefix-leak (``api.github.com/repos/org/repo-evil`` ≠ child of
    ``api.github.com/repos/org/repo``).

    For non-path destinations like ``"anthropic(via-proxy)"`` or MCP URLs, exact
    string equality is used (there is no path hierarchy to traverse).
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateAllow:
    """The action is allowed by the contract."""

    capability: str
    destination: str


@dataclass(frozen=True)
class GateDenial:
    """The action is denied by the contract.

    ``reason`` is a sanitized message safe to surface to the agent as a tool
    error.  It does NOT include the full contract, signing secret, or any
    internal detail beyond what the agent needs to understand the denial.
    """

    reason: str
    # Which check failed — for the Advocate's own structured log (never sent
    # to the agent verbatim; only ``reason`` is).
    failed_check: str = field(default="", compare=False)


# ---------------------------------------------------------------------------
# Destination helpers (mirrored from github_broker to avoid import coupling)
# ---------------------------------------------------------------------------


def _normalize_dest(dest: str) -> str:
    """Normalize a destination string to a canonical form.

    For URL/path destinations: strips trailing slashes, ensures single leading
    slash when the destination looks like a path (starts with ``/``).

    For opaque tokens like ``"anthropic(via-proxy)"`` or full URLs like
    ``"https://…"``: returned unchanged (no slash manipulation).
    """
    if dest.startswith("/"):
        dest = "/" + dest.lstrip("/")
        return dest.rstrip("/") or "/"
    # Strip trailing slash only for path-like strings that don't have a scheme.
    if "://" not in dest:
        return dest.rstrip("/") or dest
    # Full URL — no normalization beyond stripping trailing slash.
    return dest.rstrip("/") or dest


def _is_path_like(dest: str) -> bool:
    """Return True when ``dest`` is a host+path token (no scheme, no special chars).

    Path-like destinations (e.g. ``"api.github.com/repos/org/repo"``) support
    child-path matching.  Opaque tokens (``"anthropic(via-proxy)"``) and full
    URLs (``"https://…"``) require exact match only — we do not want
    ``"anthropic(via-proxy)/extra"`` to silently pass through.
    """
    return "://" not in dest and "(" not in dest


def _dest_matches(requested: str, allowed: str) -> bool:
    """Return True if ``requested`` is at or under ``allowed``.

    Uses path-segment boundary matching to avoid the naive startswith prefix
    leak: ``allowed = "api.github.com/repos/org/repo"`` must NOT match
    ``requested = "api.github.com/repos/org/repo-evil"``.

    Matching rules (applied to normalized forms):
    - Exact equality → always allow.
    - If ``allowed`` is path-like (host+path, no scheme, no special chars):
      ``requested`` starts with ``allowed + "/"`` → allow (child path).
    - Opaque tokens and full URLs: exact match only.
    """
    n_req = _normalize_dest(requested)
    n_allow = _normalize_dest(allowed)
    if n_req == n_allow:
        return True
    # Only apply child-path matching for host+path tokens, not opaque or scheme-URLs.
    if _is_path_like(n_allow) and n_req.startswith(n_allow + "/"):
        return True
    return False


def _destination_allowed(requested: str, allowed_destinations: list[str]) -> bool:
    """Return True if ``requested`` matches at least one entry in ``allowed_destinations``."""
    return any(_dest_matches(requested, a) for a in allowed_destinations)


def _branch_allowed(branch: str, allowed_patterns: list[str]) -> bool:
    """Return True if ``branch`` matches at least one glob pattern in ``allowed_patterns``.

    Patterns use shell-glob semantics (``fnmatch``): ``agent/*`` matches
    ``agent/fix-123`` but not ``main`` or ``agent`` (no slash). An empty pattern
    list denies every branch (fail-closed).
    """
    return any(fnmatch.fnmatch(branch, pat) for pat in allowed_patterns)


def _repo_allowed(repo: str, allowed_patterns: list[str]) -> bool:
    """Return True if ``repo`` matches at least one glob pattern in ``allowed_patterns``.

    Mirrors :func:`_branch_allowed`. A literal pattern like ``"org/repo"`` (no
    wildcard characters) matches only itself via ``fnmatch`` — it does NOT
    prefix-match ``"org/repo-evil"``. A pattern like ``"org/*"`` matches any
    repo under that org. An empty pattern list denies every repo (fail-closed).
    """
    return any(fnmatch.fnmatch(repo, pat) for pat in allowed_patterns)


# ---------------------------------------------------------------------------
# Public gate
# ---------------------------------------------------------------------------


def check_action(
    contract: dict,
    *,
    capability: str,
    destination: str,
    scope_repo: str | None = None,
    write_branch: str | None = None,
) -> GateAllow | GateDenial:
    """Gate a single agent RPC against the frozen contract.

    Args:
        contract: The plain contract dict extracted from a verified envelope
            (i.e., after :func:`~advocate.contract.verify_contract` has confirmed
            the signature).  The gate trusts this dict — callers MUST verify the
            signature before passing it here.
        capability: The logical capability being requested (e.g. ``"gh.read"``,
            ``"mcp.keboola-mcp"``).
        destination: The concrete destination for this call (e.g.
            ``"api.github.com/repos/org/repo-X"``,
            ``"anthropic(via-proxy)"``).
        scope_repo: Optional repository (``"org/repo-X"``) the action targets.
            When provided and ``contract["scope"]["repos"]`` is non-empty, the
            action is denied if the repo is not in the allowed list.  When
            ``scope.repos`` is empty (no ``operates_on``) the scope check is
            skipped — but note GitHub capabilities are then never granted, so a
            GitHub call is already denied at the capability step.
        write_branch: Optional target branch (``"agent/fix-1"``, ``"main"``) for
            a ref-targeting GitHub write.  When provided, the action is denied
            unless the branch matches ``contract["scope"]["writable_branches"]``
            (glob).  Pass ``None`` for reads and non-ref-targeting calls.

    Returns:
        :class:`GateAllow` if all checks pass, :class:`GateDenial` otherwise.
    """
    allowed_caps: list[str] = contract.get("capabilities", [])
    allowed_dests: list[str] = contract.get("destinations", [])
    scope: dict = contract.get("scope", {})
    allowed_repos: list[str] = scope.get("repos", [])
    allowed_branches: list[str] = scope.get("writable_branches", [])

    # NOT WIRED: is_irreversible()/contract["irreversible_gate"] are intentionally
    # not consulted here. No capability that Configuration/derive_contract can
    # currently grant (gh.read, gh.write, mcp.*, anthropic) is ever a member of
    # irreversible_gate (gh.merge, deploy, delete) — those capabilities are not
    # yet issuable, so an irreversible-gate check here would be dead weight with
    # no observable effect. Wire an is_irreversible() call + an out-of-band
    # approval step into this function once gh.merge / gh.delete (or another
    # irreversible capability) become grantable (Phase 5+, per is_irreversible's
    # own docstring) — do not silently allow them through capability-check alone.

    # 1. Capability check
    if capability not in allowed_caps:
        log.warning(
            "gate: capability denied — cap=%r not in contract",
            capability,
        )
        return GateDenial(
            reason=f"capability '{capability}' is not in the contract",
            failed_check="capability",
        )

    # 2. Destination check (segment-boundary matching)
    if not _destination_allowed(destination, allowed_dests):
        log.warning(
            "gate: destination denied — dest=%r not in contract",
            destination,
        )
        return GateDenial(
            reason=f"destination '{destination}' is not in the contract",
            failed_check="destination",
        )

    # 3. Scope check (only when the contract has a non-empty repos list). Uses
    # fnmatch so an "org/*" pattern authorizes any repo under that org, while a
    # literal "org/repo" pattern (no wildcard characters) only matches itself —
    # fnmatch treats a pattern with no special characters as an exact match.
    if scope_repo is not None and allowed_repos:
        if not _repo_allowed(scope_repo, allowed_repos):
            log.warning(
                "gate: scope denied — repo=%r not in contract scope",
                scope_repo,
            )
            return GateDenial(
                reason=f"repository '{scope_repo}' is not in the contract scope",
                failed_check="scope",
            )

    # 4. Writable-branch check (only for ref-targeting writes that name a branch)
    if write_branch is not None and not _branch_allowed(write_branch, allowed_branches):
        log.warning(
            "gate: branch denied — branch=%r not in contract writable_branches",
            write_branch,
        )
        return GateDenial(
            reason=f"branch '{write_branch}' is not writable under the contract",
            failed_check="branch",
        )

    return GateAllow(capability=capability, destination=destination)


def is_irreversible(contract: dict, capability: str) -> bool:
    """Return True if ``capability`` is in the contract's irreversible gate.

    Irreversible actions (``gh.merge``, ``deploy``, ``delete``) require an
    out-of-band approval signal (Phase 5+).  This helper is provided for
    callers to check before dispatching — the gate itself allows them if they
    are in ``capabilities``, but the Advocate should layer an additional
    approval step on top for anything that returns ``True`` here.
    """
    return capability in contract.get("irreversible_gate", [])
