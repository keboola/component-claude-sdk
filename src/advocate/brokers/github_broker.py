"""GitHub/HTTP broker — scoped token injected server-side; SSRF-hard-denied.

Security invariants:
1. The GitHub token is injected by the Advocate from config — the agent never
   holds it.
2. The destination is validated against a config-driven allowlist **before**
   any outbound call is made.  An off-allowlist destination is hard-denied and
   returned as a clean tool error; the token is never attached to a denied
   request.
3. ``api.github.com`` is the ONLY host the GitHub broker will ever contact.
   The allowed-destination check is enforced here; even if Phase 4's contract
   gate is not yet wired in, an agent-supplied destination that does not match
   is rejected deterministically.

Design notes (Phase 5):
- The claude CLI's GitHub tooling currently drives ``gh`` / ``git`` CLI tools
  directly.  Pointing them at this broker requires either (a) a thin shim
  binary that translates ``gh api …`` to a UDS RPC, or (b) configuring the CLI
  to use this endpoint via an environment variable.  Phase 5 will investigate;
  see the Phase 5 section of the plan doc.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import urllib.parse
from typing import Any

import httpx

from advocate.idempotency import get as _idem_get
from advocate.idempotency import store as _idem_store

log = logging.getLogger(__name__)

# ---- Pinned upstream (HARD-CODED — never taken from agent input) ----
GITHUB_API_HOST = "api.github.com"
GITHUB_API_BASE = f"https://{GITHUB_API_HOST}"

# Allowed HTTP methods for GitHub REST calls.
_ALLOWED_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})

_ACTION_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_PATH_RE = re.compile(r"^/[a-zA-Z0-9_.~%!$&'()*+,;=:@/-]*$")

_ALLOWED_FIELDS = frozenset({"action_id", "method", "path", "body", "headers"})

_UPSTREAM_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)


def _is_safe_path(path: str) -> bool:
    """Reject directory-traversal and empty path segments — the repo-scope escape.

    The contract gate and the broker allowlist both match on the *literal* path
    string, but ``httpx`` (and the GitHub server) collapse RFC 3986 dot-segments
    *before the request is sent*. So a literal ``/repos/org/repo/../other-repo``
    matches the allowed ``/repos/org/repo/`` prefix at gate/allowlist time, yet
    the real token is injected against ``/repos/org/other-repo``. We close that by
    hard-denying any path that contains a ``.``/``..`` segment or a ``//`` — in
    both the raw and percent-decoded forms (so ``..%2f`` is caught too). GitHub
    REST paths never legitimately need these; a real filename like ``file.txt`` is
    a single segment, not a ``.`` segment, so it is unaffected.
    """
    for candidate in {path, urllib.parse.unquote(path)}:
        if "//" in candidate:
            return False
        if any(seg in (".", "..") for seg in candidate.strip("/").split("/")):
            return False
    return True


def _server_idem_key(method: str, path: str, body: dict | None) -> str:
    """Derive the idempotency cache key from the gated request content.

    MED-3: the cache key is computed SERVER-SIDE from (method, path, body), NOT
    from the agent-supplied ``action_id``.  An agent therefore cannot pre-seed an
    ``action_id`` with a benign 2xx and have a *different* legitimate side-effecting
    call collide with it (cached-reply suppression).  Identical requests still
    dedupe (true idempotency); different requests always get distinct keys.
    """
    canonical = json.dumps(
        {"method": method, "path": path, "body": body},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate(raw: dict) -> tuple[dict | None, str | None]:
    """Validate an untrusted GitHub REST RPC request from the agent.

    Returns ``(validated, None)`` on success or ``(None, error_message)``.
    """
    extra = set(raw.keys()) - _ALLOWED_FIELDS
    if extra:
        return None, f"unexpected field(s): {sorted(extra)}"

    action_id = raw.get("action_id")
    if not isinstance(action_id, str) or not action_id or len(action_id) > 128:
        return None, "action_id: required non-empty string, max 128 chars"
    if not _ACTION_ID_RE.match(action_id):
        return None, "action_id: only [a-zA-Z0-9_-] allowed"

    method = raw.get("method")
    if not isinstance(method, str) or method not in _ALLOWED_HTTP_METHODS:
        return None, f"method: must be one of {sorted(_ALLOWED_HTTP_METHODS)}"

    path = raw.get("path")
    if not isinstance(path, str) or not path or len(path) > 2048:
        return None, "path: required non-empty string, max 2048 chars"
    if not path.startswith("/"):
        return None, "path: must start with '/'"
    if not _PATH_RE.match(path):
        return None, "path: contains disallowed characters"
    if not _is_safe_path(path):
        return None, "path: directory traversal or empty segments are not allowed"

    # Canonicalize to the path GitHub will actually ROUTE on. GitHub (and the
    # upstream proxy) percent-decode path segments exactly once before routing,
    # so the capability classifier, the contract gate, the destination allowlist
    # and the writable-branch / repo-scope checks MUST all operate on the decoded
    # path — otherwise an agent hides a privileged operation behind a percent-
    # encoded letter (e.g. ``/pulls/42/%6Derge`` classifies as ``write_branch``
    # but GitHub routes it to ``/pulls/42/merge``).  We expose the once-decoded
    # form as ``path_canonical`` for those decisions while ``path`` is forwarded
    # verbatim on the wire (it carries no literal query/fragment delimiters —
    # ``_PATH_RE`` forbids raw ``?``/``#`` — so GitHub's single decode of the raw
    # path equals ``path_canonical``).  We reject MULTIPLY percent-encoded paths:
    # no legitimate GitHub REST path needs them, and they would push the gate's
    # single decode out of step with GitHub's, re-opening the bypass one level
    # deeper.  Fail-closed.
    decoded = urllib.parse.unquote(path)
    if urllib.parse.unquote(decoded) != decoded:
        return None, "path: multiply percent-encoded segments are not allowed"
    if not _is_safe_path(decoded):
        return None, "path: directory traversal or empty segments are not allowed"

    body = raw.get("body")
    if body is not None and not isinstance(body, dict):
        return None, "body: must be a JSON object when present"

    # Agent-supplied extra headers are ignored — the broker sets Authorization.
    # We accept the field but strip it silently to avoid a 400 on the caller side
    # while still never honouring agent-supplied auth headers.
    headers = raw.get("headers")
    if headers is not None and not isinstance(headers, dict):
        return None, "headers: must be a JSON object when present"

    validated = dict(raw)
    validated["path_canonical"] = decoded
    return validated, None


def handle_request(
    payload: dict,
    github_token: str,
    *,
    allowed_destinations: list[str] | None = None,
) -> tuple[int, dict]:
    """Execute a validated GitHub REST call with the scoped token injected.

    Args:
        payload: Validated dict from :func:`validate`.
        github_token: Real GitHub PAT (never sourced from agent input).
        allowed_destinations: Optional list of allowed path prefixes (e.g.
            ``["/repos/org/repo-X"]``).  ``None`` means all GitHub API paths
            are allowed (Phase 3 default; Phase 4 will narrow this via the
            contract gate).  An empty list allows nothing.

    Returns:
        ``(status_code, response_body_dict)`` tuple.
    """
    path: str = payload["path"]
    # The allowlist must match the path GitHub will route on (decoded once), not
    # the raw encoded form — otherwise an encoded segment could slip a request
    # past the allowlist while GitHub routes it elsewhere. ``validate`` provides
    # the canonical form; fall back to the raw path for any caller that bypassed
    # validation (then they get the stricter raw comparison).
    canonical_path: str = payload.get("path_canonical", path)
    # MED-3: cache key is server-derived from the request content, not the
    # agent-supplied action_id (which is kept only as a protocol/validation field).
    idem_key = _server_idem_key(payload["method"], canonical_path, payload.get("body"))
    cached = _idem_get("github", idem_key)
    if cached is not None:
        return cached

    # ---- SSRF / destination check (Phase 3 focused allowlist) ----
    # The upstream host is ALWAYS api.github.com (pinned above).
    # If an explicit path allowlist is configured, enforce it here.
    if allowed_destinations is not None and not _path_allowed(canonical_path, allowed_destinations):
        log.warning("github broker: off-allowlist destination denied for path=%s", path)
        return 403, {"error": "destination not in allowlist"}

    try:
        result = _call_github(payload, github_token)
    except Exception:  # noqa: BLE001
        log.warning("github broker error for path=%s", path, exc_info=True)
        return 502, {"error": "GitHub request failed"}

    status, body = result
    _idem_store("github", idem_key, status, body)
    return result


def _normalize_path(p: str) -> str:
    """Return ``p`` with exactly one leading slash and no trailing slash.

    Consistent normalization prevents bypass via double slashes or missing
    leading slash on either the path or a configured destination.
    """
    p = "/" + p.lstrip("/")
    return p.rstrip("/") or "/"


def _path_allowed(path: str, allowed_destinations: list[str]) -> bool:
    """Return True if ``path`` is at or under at least one entry in ``allowed_destinations``.

    Matching requires an exact match or a path-segment boundary so that
    ``/repos/org/repo`` does NOT grant access to ``/repos/org/repo-evil`` or
    ``/repos/org/repository-private``.

    Both ``path`` and each destination are normalized (single leading slash,
    no trailing slash) before comparison, so the check is consistent regardless
    of whether the caller omits or includes a leading slash.

    An empty list denies everything.

    Examples::

        _path_allowed("/repos/org/repo", ["/repos/org/repo"])          # True  (exact)
        _path_allowed("/repos/org/repo/contents", ["/repos/org/repo"]) # True  (child)
        _path_allowed("/repos/org/repo-evil", ["/repos/org/repo"])     # False (prefix leak)
        _path_allowed("/repos/org/repository", ["/repos/org/repo"])    # False (prefix leak)
    """
    norm_path = _normalize_path(path)
    for dest in allowed_destinations:
        norm_dest = _normalize_path(dest)
        if norm_path == norm_dest or norm_path.startswith(norm_dest + "/"):
            return True
    return False


def _call_github(payload: dict, github_token: str) -> tuple[int, dict[str, Any]]:
    """Make the actual GitHub API call.

    The ``Authorization`` header is injected here; the URL host is the
    module-level constant ``GITHUB_API_BASE`` — NEVER taken from payload.

    Agent-supplied ``headers`` are silently discarded to prevent header
    injection (e.g. the agent cannot override ``Authorization``).
    """
    path: str = payload["path"]
    method: str = payload["method"]
    body: dict | None = payload.get("body")

    # Inject the token; discard any agent-supplied headers entirely.
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"

    url = f"{GITHUB_API_BASE}{path}"

    try:
        with httpx.Client() as client:
            resp = client.request(
                method,
                url,
                headers=headers,
                json=body,
                timeout=_UPSTREAM_TIMEOUT,
            )
        try:
            resp_body = resp.json()
        except Exception:  # noqa: BLE001 — non-JSON body (204 No Content, etc.)
            resp_body = {"status": resp.status_code}
        return resp.status_code, resp_body
    except httpx.HTTPError:
        log.warning("github broker HTTP error path=%s method=%s", path, method, exc_info=True)
        return 502, {"error": "GitHub request failed"}
    except Exception:  # noqa: BLE001
        log.warning("github broker unexpected error path=%s", path, exc_info=True)
        return 502, {"error": "GitHub request failed"}
