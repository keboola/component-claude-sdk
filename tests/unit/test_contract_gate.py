"""Phase 4 contract + gate tests.

Security load-bearing assertions (spec §7, must stay green):
1. Off-contract capability → hard deny.
2. Off-contract destination (incl. path-boundary attempt for sibling repo) → hard deny.
3. In-scope dev action (repo-scoped declared capability) → passes.
4. Contract tamper (modify a field, signature no longer verifies) → rejected.
5. Denial is a clean error, not a crash, and does not leak secrets/internal detail.
6. Derivation is deterministic (same inputs → same contract/signature).
7. Gate via TCP — contract-gated server rejects off-contract GitHub/MCP calls as 403.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx

from advocate.contract import (
    CAP_GH_COMMENT,
    CAP_GH_OPEN_PR,
    CAP_GH_READ,
    CAP_GH_WRITE_BRANCH,
    DEST_ANTHROPIC,
    GITHUB_API_HOST,
    _ConfigProto,
    derive_contract,
    new_invocation_secret,
    operates_on_to_repo_path,
    sign_contract,
    verify_contract,
)
from advocate.gate import (
    GateAllow,
    GateDenial,
    _dest_matches,
    check_action,
    is_irreversible,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Cfg:
    """Minimal duck-typed config object for tests.

    Carries class-level type annotations so ty recognises it as satisfying
    the ``_ConfigProto`` structural interface used by ``derive_contract``.
    We avoid a real ``Configuration`` instance here because it requires a
    live ``#anthropic_key`` that tests should not need.
    """

    github_enabled: bool
    mcp_servers: list

    def __init__(
        self,
        *,
        github_enabled: bool = False,
        mcp_servers: list | None = None,
    ) -> None:
        self.github_enabled = github_enabled
        self.mcp_servers = mcp_servers or []


def _make_cfg(
    *,
    github_enabled: bool = False,
    mcp_servers: list | None = None,
) -> _ConfigProto:
    """Return a minimal config duck-typed as ``_ConfigProto``."""
    return _Cfg(github_enabled=github_enabled, mcp_servers=mcp_servers)  # type: ignore[return-value]


def _make_remote_server(name: str, url: str) -> object:
    """A minimal McpRemoteServer-like object."""
    from configuration import McpRemoteServer

    return McpRemoteServer(name=name, type="http", url=url, headers={})


def _make_stdio_server(name: str) -> object:
    """A minimal McpStdioServer-like object."""
    from configuration import McpStdioServer

    return McpStdioServer(name=name, command="cat", args=[], env={})


def _tcp_client(port: int) -> httpx.Client:
    return httpx.Client(base_url=f"http://127.0.0.1:{port}")


# ---------------------------------------------------------------------------
# 1. derive_contract — determinism + shape
# ---------------------------------------------------------------------------


class TestOperatesOnToRepoPath:
    def test_exact_repo_passes_through(self) -> None:
        assert operates_on_to_repo_path("org/repo") == "org/repo"

    def test_org_wildcard_strips_suffix(self) -> None:
        assert operates_on_to_repo_path("org/*") == "org"


class TestDeriveContract:
    def test_same_inputs_yield_same_contract(self) -> None:
        """Derivation is deterministic: same inputs → identical contract dict."""
        cfg = _make_cfg(github_enabled=True)
        c1 = derive_contract(cfg, operates_on=["org/repo-X"])
        c2 = derive_contract(cfg, operates_on=["org/repo-X"])
        assert c1 == c2

    def test_github_enabled_adds_capabilities(self) -> None:
        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg, operates_on=["org/repo-X"])
        caps = c["capabilities"]
        assert CAP_GH_READ in caps
        assert CAP_GH_WRITE_BRANCH in caps
        assert CAP_GH_OPEN_PR in caps
        assert CAP_GH_COMMENT in caps

    def test_github_disabled_no_gh_capabilities(self) -> None:
        cfg = _make_cfg(github_enabled=False)
        c = derive_contract(cfg)
        caps = c["capabilities"]
        assert not any(cap.startswith("gh.") for cap in caps)

    def test_anthropic_proxy_always_in_destinations(self) -> None:
        cfg = _make_cfg()
        c = derive_contract(cfg)
        assert DEST_ANTHROPIC in c["destinations"]

    def test_operates_on_scopes_github_destination(self) -> None:
        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg, operates_on=["org/repo-X"])
        dests = c["destinations"]
        # The scoped destination must be present.
        assert f"{GITHUB_API_HOST}/repos/org/repo-X" in dests
        # The broad host-only destination must NOT be present when scoped.
        assert GITHUB_API_HOST not in dests

    def test_no_operates_on_withholds_github_entirely(self) -> None:
        """HIGH-3 fail-closed: github_enabled WITHOUT operates_on grants no GitHub
        capability and emits no GitHub destination (not even the broad host)."""
        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg)  # no operates_on
        assert not any(cap.startswith("gh.") for cap in c["capabilities"])
        assert not any(GITHUB_API_HOST in d for d in c["destinations"])

    def test_repos_scope_set_when_operates_on_given(self) -> None:
        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg, operates_on=["org/repo-X"])
        assert c["scope"]["repos"] == ["org/repo-X"]

    def test_repos_scope_empty_without_operates_on(self) -> None:
        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg)
        assert c["scope"]["repos"] == []

    def test_multiple_repos_all_scoped_as_destinations(self) -> None:
        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg, operates_on=["org/repo-X", "org/repo-Y"])
        dests = c["destinations"]
        assert f"{GITHUB_API_HOST}/repos/org/repo-X" in dests
        assert f"{GITHUB_API_HOST}/repos/org/repo-Y" in dests

    def test_multiple_repos_all_in_scope_list(self) -> None:
        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg, operates_on=["org/repo-X", "org/repo-Y"])
        assert c["scope"]["repos"] == ["org/repo-X", "org/repo-Y"]

    def test_org_wildcard_scopes_destination_to_org_only(self) -> None:
        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg, operates_on=["org/*"])
        dests = c["destinations"]
        assert f"{GITHUB_API_HOST}/repos/org" in dests
        # No repo-specific or double-org destination leaked in.
        assert f"{GITHUB_API_HOST}/repos/org/*" not in dests

    def test_org_wildcard_kept_as_literal_pattern_in_scope(self) -> None:
        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg, operates_on=["org/*"])
        assert c["scope"]["repos"] == ["org/*"]

    def test_mcp_remote_server_adds_capability_and_destination(self) -> None:
        server = _make_remote_server("keboola-mcp", "https://mcp.keboola.com/rpc")
        cfg = _make_cfg(mcp_servers=[server])
        c = derive_contract(cfg)
        assert "mcp.keboola-mcp" in c["capabilities"]
        assert "https://mcp.keboola.com/rpc" in c["destinations"]

    def test_mcp_stdio_server_adds_capability_no_network_destination(self) -> None:
        server = _make_stdio_server("local-mcp")
        cfg = _make_cfg(mcp_servers=[server])
        c = derive_contract(cfg)
        assert "mcp.local-mcp" in c["capabilities"]
        # No network URL in destinations — stdio is local.
        assert not any(d.startswith("http") for d in c["destinations"] if d != DEST_ANTHROPIC)

    def test_expiry_is_this_invocation(self) -> None:
        cfg = _make_cfg()
        c = derive_contract(cfg)
        assert c["expiry"] == "this_invocation"

    def test_irreversible_gate_present(self) -> None:
        cfg = _make_cfg()
        c = derive_contract(cfg)
        assert "gh.merge" in c["irreversible_gate"]
        assert "delete" in c["irreversible_gate"]


# ---------------------------------------------------------------------------
# 2. sign_contract + verify_contract
# ---------------------------------------------------------------------------


class TestSignVerify:
    def test_round_trip_verifies(self) -> None:
        """sign then verify with the same secret → True."""
        secret = new_invocation_secret()
        cfg = _make_cfg(github_enabled=True)
        contract = derive_contract(cfg, operates_on=["org/repo-X"])
        envelope = sign_contract(contract, secret)
        assert verify_contract(envelope, secret) is True

    def test_tamper_capabilities_fails_verification(self) -> None:
        """Modifying capabilities after signing → verify returns False."""
        secret = new_invocation_secret()
        cfg = _make_cfg(github_enabled=True)
        contract = derive_contract(cfg, operates_on=["org/repo-X"])
        envelope = sign_contract(contract, secret)

        # Tamper: widen capabilities post-signing.
        envelope["contract"]["capabilities"].append("gh.merge")

        assert verify_contract(envelope, secret) is False

    def test_tamper_destination_fails_verification(self) -> None:
        """Adding a destination after signing → verify returns False."""
        secret = new_invocation_secret()
        cfg = _make_cfg(github_enabled=True)
        contract = derive_contract(cfg, operates_on=["org/repo-X"])
        envelope = sign_contract(contract, secret)

        envelope["contract"]["destinations"].append("https://evil.com/exfil")

        assert verify_contract(envelope, secret) is False

    def test_wrong_secret_fails_verification(self) -> None:
        """Verifying with a different secret → False."""
        secret1 = new_invocation_secret()
        secret2 = new_invocation_secret()
        contract = derive_contract(_make_cfg())
        envelope = sign_contract(contract, secret1)
        assert verify_contract(envelope, secret2) is False

    def test_tamper_signature_field_directly(self) -> None:
        """Replacing the signature string → verify returns False."""
        secret = new_invocation_secret()
        contract = derive_contract(_make_cfg())
        envelope = sign_contract(contract, secret)
        envelope["signature"] = "deadbeef" * 8
        assert verify_contract(envelope, secret) is False

    def test_missing_signature_field(self) -> None:
        """Envelope without 'signature' key → verify returns False (not a crash)."""
        secret = new_invocation_secret()
        contract = derive_contract(_make_cfg())
        envelope = sign_contract(contract, secret)
        del envelope["signature"]
        assert verify_contract(envelope, secret) is False

    def test_missing_contract_field(self) -> None:
        """Envelope without 'contract' key → verify returns False (not a crash)."""
        secret = new_invocation_secret()
        envelope = {"signature": "abc123"}
        assert verify_contract(envelope, secret) is False

    def test_deterministic_signature(self) -> None:
        """Same contract + same secret → same signature every time."""
        secret = new_invocation_secret()
        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg, operates_on=["org/repo-X"])
        e1 = sign_contract(c, secret)
        e2 = sign_contract(c, secret)
        assert e1["signature"] == e2["signature"]

    def test_new_invocation_secret_is_random(self) -> None:
        """Each call to new_invocation_secret returns different bytes."""
        s1 = new_invocation_secret()
        s2 = new_invocation_secret()
        assert s1 != s2
        assert len(s1) == 32


# ---------------------------------------------------------------------------
# 3. Gate — destination matching helpers
# ---------------------------------------------------------------------------


class TestDestMatching:
    def test_exact_match_allowed(self) -> None:
        assert _dest_matches("api.github.com/repos/org/repo", "api.github.com/repos/org/repo") is True

    def test_child_path_allowed(self) -> None:
        assert _dest_matches("api.github.com/repos/org/repo/contents", "api.github.com/repos/org/repo") is True

    def test_sibling_repo_denied(self) -> None:
        """``repo-evil`` must NOT match ``repo`` — prevents the naive startswith leak."""
        assert _dest_matches("api.github.com/repos/org/repo-evil", "api.github.com/repos/org/repo") is False

    def test_prefix_without_slash_denied(self) -> None:
        assert _dest_matches("api.github.com/repos/org/repository", "api.github.com/repos/org/repo") is False

    def test_opaque_token_exact_only(self) -> None:
        """Opaque destinations like 'anthropic(via-proxy)' match only exactly."""
        assert _dest_matches("anthropic(via-proxy)", "anthropic(via-proxy)") is True
        assert _dest_matches("anthropic(via-proxy)/extra", "anthropic(via-proxy)") is False

    def test_trailing_slash_normalized(self) -> None:
        """Trailing slashes on either side should not affect matching."""
        assert _dest_matches("api.github.com/repos/org/repo/", "api.github.com/repos/org/repo") is True
        assert _dest_matches("api.github.com/repos/org/repo", "api.github.com/repos/org/repo/") is True


# ---------------------------------------------------------------------------
# 4. check_action — deterministic gate rule
# ---------------------------------------------------------------------------


class TestCheckAction:
    def _gh_contract(self, *, operates_on: list[str] | None = None) -> dict:
        cfg = _make_cfg(github_enabled=True)
        return derive_contract(cfg, operates_on=operates_on if operates_on is not None else ["org/repo-X"])

    # --- ALLOW cases ---

    def test_in_scope_gh_read_passes(self) -> None:
        """A declared gh.read on the scoped repo must pass."""
        c = self._gh_contract()
        result = check_action(
            c,
            capability=CAP_GH_READ,
            destination=f"{GITHUB_API_HOST}/repos/org/repo-X/contents/README.md",
        )
        assert isinstance(result, GateAllow)

    def test_in_scope_gh_write_branch_passes(self) -> None:
        c = self._gh_contract()
        result = check_action(
            c,
            capability=CAP_GH_WRITE_BRANCH,
            destination=f"{GITHUB_API_HOST}/repos/org/repo-X/git/refs",
        )
        assert isinstance(result, GateAllow)

    def test_anthropic_proxy_destination_always_passes(self) -> None:
        """The Anthropic proxy destination is always in contract → should pass
        if we add a dummy capability for it (the proxy does not need a specific cap)."""
        c = self._gh_contract()
        # Manually add a dummy capability entry for this test to isolate destination.
        c["capabilities"].append("anthropic.call")
        result = check_action(c, capability="anthropic.call", destination=DEST_ANTHROPIC)
        assert isinstance(result, GateAllow)

    # --- DENY cases: off-contract capability ---

    def test_off_contract_capability_denied(self) -> None:
        """A capability not in the contract → hard deny."""
        c = self._gh_contract()
        result = check_action(
            c,
            capability="gh.deploy_production",
            destination=f"{GITHUB_API_HOST}/repos/org/repo-X",
        )
        assert isinstance(result, GateDenial)
        assert result.failed_check == "capability"

    def test_unknown_mcp_capability_denied(self) -> None:
        c = self._gh_contract()
        result = check_action(
            c,
            capability="mcp.attacker-server",
            destination="https://evil.com/mcp",
        )
        assert isinstance(result, GateDenial)
        assert result.failed_check == "capability"

    # --- DENY cases: off-contract destination ---

    def test_sibling_repo_destination_denied(self) -> None:
        """``org/repo-X-evil`` must not be accessible when contract scopes ``org/repo-X``."""
        c = self._gh_contract()
        result = check_action(
            c,
            capability=CAP_GH_READ,
            destination=f"{GITHUB_API_HOST}/repos/org/repo-X-evil/contents",
        )
        assert isinstance(result, GateDenial)
        assert result.failed_check == "destination"

    def test_arbitrary_destination_denied(self) -> None:
        c = self._gh_contract()
        result = check_action(
            c,
            capability=CAP_GH_READ,
            destination="https://evil.com/exfil",
        )
        assert isinstance(result, GateDenial)
        assert result.failed_check == "destination"

    def test_path_boundary_sibling_denied(self) -> None:
        """``/repos/org/repo-X`` must NOT grant ``/repos/org/repo-Xtra``."""
        c = self._gh_contract()
        result = check_action(
            c,
            capability=CAP_GH_READ,
            destination=f"{GITHUB_API_HOST}/repos/org/repo-Xtra",
        )
        assert isinstance(result, GateDenial)
        assert result.failed_check == "destination"

    # --- DENY cases: scope ---

    def test_off_scope_repo_denied(self) -> None:
        """When scope.repos is non-empty, a different repo is denied."""
        c = self._gh_contract(operates_on=["org/repo-X"])
        result = check_action(
            c,
            capability=CAP_GH_READ,
            destination=f"{GITHUB_API_HOST}/repos/org/repo-X",
            scope_repo="org/repo-Y",  # wrong repo
        )
        assert isinstance(result, GateDenial)
        assert result.failed_check == "scope"

    def test_scope_check_skipped_when_repos_empty(self) -> None:
        """When scope.repos is empty, the scope check is a no-op.

        Derive_contract no longer produces a GitHub-capable contract with empty
        repos (HIGH-3 fail-closed), so we hand-build a contract that DOES carry
        the capability + destination but an empty repos list, to isolate the
        gate's scope-skip branch.
        """
        c = {
            "scope": {"repos": [], "writable_branches": ["agent/*"]},
            "capabilities": [CAP_GH_READ],
            "destinations": [GITHUB_API_HOST],
        }
        # Even if we pass a scope_repo, the gate should not deny on scope.
        result = check_action(
            c,
            capability=CAP_GH_READ,
            destination=GITHUB_API_HOST,
            scope_repo="org/any-repo",
        )
        # capability and destination match → should pass despite scope_repo given
        assert isinstance(result, GateAllow)

    def test_org_wildcard_scope_allows_any_repo_under_org(self) -> None:
        """A contract scoped to 'org/*' must allow gh.read on ANY repo under org."""
        c = self._gh_contract(operates_on=["org/*"])
        result = check_action(
            c,
            capability=CAP_GH_READ,
            destination=f"{GITHUB_API_HOST}/repos/org",
            scope_repo="org/some-other-repo",
        )
        assert isinstance(result, GateAllow)

    def test_org_wildcard_scope_denies_different_org(self) -> None:
        """A contract scoped to 'org/*' must NOT allow a repo under a different org."""
        c = self._gh_contract(operates_on=["org/*"])
        result = check_action(
            c,
            capability=CAP_GH_READ,
            destination=f"{GITHUB_API_HOST}/repos/org",
            scope_repo="other-org/some-repo",
        )
        assert isinstance(result, GateDenial)
        assert result.failed_check == "scope"

    def test_multi_repo_scope_allows_either_listed_repo(self) -> None:
        c = self._gh_contract(operates_on=["org/repo-X", "org/repo-Y"])
        result = check_action(
            c,
            capability=CAP_GH_READ,
            destination=f"{GITHUB_API_HOST}/repos/org/repo-Y",
            scope_repo="org/repo-Y",
        )
        assert isinstance(result, GateAllow)

    def test_multi_repo_scope_denies_unlisted_repo(self) -> None:
        c = self._gh_contract(operates_on=["org/repo-X", "org/repo-Y"])
        result = check_action(
            c,
            capability=CAP_GH_READ,
            destination=f"{GITHUB_API_HOST}/repos/org/repo-X",
            scope_repo="org/repo-Z",
        )
        assert isinstance(result, GateDenial)
        assert result.failed_check == "scope"

    def test_exact_repo_scope_still_rejects_prefix_leak(self) -> None:
        """A literal 'org/repo' pattern (no wildcard chars) must not glob-match 'org/repo-evil'."""
        c = self._gh_contract(operates_on=["org/repo"])
        result = check_action(
            c,
            capability=CAP_GH_READ,
            destination=f"{GITHUB_API_HOST}/repos/org/repo",
            scope_repo="org/repo-evil",
        )
        assert isinstance(result, GateDenial)
        assert result.failed_check == "scope"

    # --- Denial is clean, not a crash ---

    def test_denial_has_reason_not_secret_detail(self) -> None:
        """GateDenial.reason must not contain signing secrets or internal contract dump."""
        secret = new_invocation_secret()
        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg, operates_on=["org/repo-X"])
        result = check_action(c, capability="evil.cap", destination="https://evil.com")
        assert isinstance(result, GateDenial)
        # The reason should be a short, sanitized string — not a full contract dump.
        assert isinstance(result.reason, str)
        assert len(result.reason) < 300
        # The HMAC secret (random bytes) should never appear in the reason.
        assert secret.hex() not in result.reason


# ---------------------------------------------------------------------------
# 5. is_irreversible helper
# ---------------------------------------------------------------------------


class TestIsIrreversible:
    def test_merge_is_irreversible(self) -> None:
        c = derive_contract(_make_cfg())
        assert is_irreversible(c, "gh.merge") is True

    def test_delete_is_irreversible(self) -> None:
        c = derive_contract(_make_cfg())
        assert is_irreversible(c, "delete") is True

    def test_gh_read_is_not_irreversible(self) -> None:
        c = derive_contract(_make_cfg())
        assert is_irreversible(c, "gh.read") is False


# ---------------------------------------------------------------------------
# 6. Gate wired into AdvocateServer via TCP (end-to-end)
# ---------------------------------------------------------------------------


class TestGateViaUds:
    """The AdvocateServer must gate GitHub and MCP calls against the contract
    when a signed contract envelope is configured."""

    def _make_server_with_contract(self) -> tuple:
        """Return (server, secret, envelope) for a server gated to ``org/repo-X``."""
        from advocate.server import AdvocateServer

        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg, operates_on=["org/repo-X"])
        secret = new_invocation_secret()
        envelope = sign_contract(c, secret)

        server = AdvocateServer(
            anthropic_key="dummy",
            github_token="dummy-gh-token",
            contract_envelope=envelope,
            contract_signing_secret=secret,
        )
        server.start()
        return server, secret, envelope

    def test_in_scope_github_get_passes_gate(self) -> None:
        """A GET to the scoped repo passes the contract gate and reaches the broker."""
        from advocate import idempotency
        from advocate.brokers import github_broker

        idempotency.clear()
        server, _secret, _env = self._make_server_with_contract()

        mock_result = (200, {"id": 1, "full_name": "org/repo-X"})
        try:
            with patch.object(github_broker, "_call_github", return_value=mock_result):
                payload = {
                    "action_id": "gate-allow-1",
                    "method": "GET",
                    "path": "/repos/org/repo-X",
                }
                with _tcp_client(server.port) as client:
                    resp = client.post(
                        "/v1/github",
                        json=payload,
                        headers={"content-type": "application/json"},
                    )
            assert resp.status_code == 200
        finally:
            server.stop()

    def test_off_contract_capability_github_denied(self) -> None:
        """A POST (gh.write_branch) when the contract only allows gh.read → 403.

        We build a read-only contract (github_enabled=False → no gh.* caps at all)
        and confirm a GitHub write is denied.
        """
        from advocate.server import AdvocateServer

        cfg = _make_cfg(github_enabled=False)  # no GH caps
        c = derive_contract(cfg)
        secret = new_invocation_secret()
        envelope = sign_contract(c, secret)

        server = AdvocateServer(
            anthropic_key="dummy",
            github_token="dummy",
            contract_envelope=envelope,
            contract_signing_secret=secret,
        )
        server.start()
        try:
            payload = {
                "action_id": "gate-deny-cap-1",
                "method": "POST",
                "path": "/repos/org/repo-X/git/refs",
                "body": {"ref": "refs/heads/agent/test", "sha": "abc123"},
            }
            with _tcp_client(server.port) as client:
                resp = client.post(
                    "/v1/github",
                    json=payload,
                    headers={"content-type": "application/json"},
                )
            assert resp.status_code == 403
            body = resp.json()
            assert "error" in body
            # Denial must not expose internal contract detail or secrets.
            assert "secret" not in body["error"].lower()
        finally:
            server.stop()

    def test_off_contract_destination_github_denied(self) -> None:
        """A path to a sibling repo (repo-X-evil) must be denied by the gate."""
        from advocate import idempotency

        idempotency.clear()
        server, _s, _e = self._make_server_with_contract()
        try:
            payload = {
                "action_id": "gate-deny-dest-1",
                "method": "GET",
                "path": "/repos/org/repo-X-evil/contents",
            }
            with _tcp_client(server.port) as client:
                resp = client.post(
                    "/v1/github",
                    json=payload,
                    headers={"content-type": "application/json"},
                )
            assert resp.status_code == 403
            body = resp.json()
            assert "error" in body
        finally:
            server.stop()

    def test_tampered_contract_denied(self) -> None:
        """A server configured with a tampered contract envelope must deny all calls."""
        from advocate.server import AdvocateServer

        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg, operates_on=["org/repo-X"])
        secret = new_invocation_secret()
        envelope = sign_contract(c, secret)

        # Tamper: widen scope after signing.
        envelope["contract"]["capabilities"].append("gh.merge")

        server = AdvocateServer(
            anthropic_key="dummy",
            github_token="dummy",
            contract_envelope=envelope,
            contract_signing_secret=secret,
        )
        server.start()
        try:
            payload = {
                "action_id": "gate-tamper-1",
                "method": "GET",
                "path": "/repos/org/repo-X",
            }
            with _tcp_client(server.port) as client:
                resp = client.post(
                    "/v1/github",
                    json=payload,
                    headers={"content-type": "application/json"},
                )
            assert resp.status_code == 403
            body = resp.json()
            assert "error" in body
        finally:
            server.stop()

    def test_no_contract_is_backward_compatible(self) -> None:
        """Without a contract (Phase 3 default), the server still works normally."""
        from advocate import idempotency
        from advocate.brokers import github_broker
        from advocate.server import AdvocateServer

        idempotency.clear()
        # No contract_envelope / secret → gate is a no-op.
        server = AdvocateServer(anthropic_key="dummy", github_token="dummy")
        server.start()

        mock_result = (200, {"id": 99})
        try:
            with patch.object(github_broker, "_call_github", return_value=mock_result):
                payload = {
                    "action_id": "compat-1",
                    "method": "GET",
                    "path": "/repos/any/repo",
                }
                with _tcp_client(server.port) as client:
                    resp = client.post(
                        "/v1/github",
                        json=payload,
                        headers={"content-type": "application/json"},
                    )
            assert resp.status_code == 200
        finally:
            server.stop()


# ---------------------------------------------------------------------------
# 7. Fail-closed: envelope/secret mismatch must deny, not allow
# ---------------------------------------------------------------------------


class TestFailClosed:
    """The gate must fail CLOSED on any configuration slip, never fail-open."""

    def test_envelope_without_secret_raises_at_construction(self) -> None:
        """Providing an envelope but no secret → ValueError at AdvocateServer construction."""
        import pytest

        from advocate.server import AdvocateServer

        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg, operates_on=["org/repo-X"])
        secret = new_invocation_secret()
        envelope = sign_contract(c, secret)

        with pytest.raises(ValueError, match="both be provided together or both omitted"):
            AdvocateServer(
                anthropic_key="dummy",
                github_token="dummy",
                contract_envelope=envelope,
                contract_signing_secret=None,  # mismatch → must raise
            )

    def test_secret_without_envelope_raises_at_construction(self) -> None:
        """Providing a secret but no envelope → ValueError at AdvocateServer construction."""
        import pytest

        from advocate.server import AdvocateServer

        with pytest.raises(ValueError, match="both be provided together or both omitted"):
            AdvocateServer(
                anthropic_key="dummy",
                github_token="dummy",
                contract_envelope=None,  # mismatch → must raise
                contract_signing_secret=new_invocation_secret(),
            )

    def test_envelope_present_secret_none_check_gate_denies(self) -> None:
        """If somehow _check_gate is reached with envelope set but secret=None, it must DENY.

        This tests the in-handler defence-in-depth; the constructor guard above is
        the primary protection, but _check_gate must not allow-all as a fallback.
        """
        from advocate.server import _Handler, _TcpServer

        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg, operates_on=["org/repo-X"])
        secret = new_invocation_secret()
        envelope = sign_contract(c, secret)

        # Bypass the AdvocateServer constructor check by directly constructing
        # _TcpServer (which does NOT validate the mismatch — only AdvocateServer
        # does).  This simulates a future code path that might bypass the outer guard.
        tcp_server = _TcpServer(
            ("127.0.0.1", 0),
            _Handler,
            "dummy-key",
            github_token="dummy",
            contract_envelope=envelope,
            contract_signing_secret=None,  # mismatch — _check_gate should deny
        )

        # Simulate calling _check_gate directly by creating a minimal _Handler instance.
        # We reach in via a unit-test shim rather than a live TCP round-trip.
        handler = _Handler.__new__(_Handler)
        handler.server = tcp_server  # type: ignore[attr-defined]

        denied_responses: list[tuple[int, dict]] = []

        def _fake_respond(status: int, body: dict) -> None:
            denied_responses.append((status, body))

        handler._respond = _fake_respond  # type: ignore[method-assign]

        result = handler._check_gate("gh.read", "api.github.com/repos/org/repo-X")

        assert result is False, "_check_gate must fail-closed when secret is None but envelope is present"
        assert len(denied_responses) == 1
        status, body = denied_responses[0]
        assert status == 403
        assert "error" in body
        tcp_server.socket.close()


# ---------------------------------------------------------------------------
# 8. Capability granularity: DELETE and merge ops require elevated capabilities
# ---------------------------------------------------------------------------


class TestGithubCapabilityMapping:
    """The _github_capability helper must map HTTP methods to the correct capability.

    gh.delete and gh.merge are NOT in the default contract, so they must
    hard-deny when a GitHub-enabled contract is present.
    """

    def test_get_maps_to_gh_read(self) -> None:
        from advocate.server import _github_capability

        assert _github_capability("GET", "/repos/org/repo") == "gh.read"

    def test_delete_maps_to_gh_delete(self) -> None:
        from advocate.server import _github_capability

        assert _github_capability("DELETE", "/repos/org/repo") == "gh.delete"

    def test_put_to_merge_path_maps_to_gh_merge(self) -> None:
        from advocate.server import _github_capability

        assert _github_capability("PUT", "/repos/org/repo/pulls/1/merge") == "gh.merge"

    def test_post_to_merges_path_maps_to_gh_merge(self) -> None:
        from advocate.server import _github_capability

        assert _github_capability("POST", "/repos/org/repo/merges") == "gh.merge"

    def test_post_to_non_merge_path_maps_to_gh_write_branch(self) -> None:
        from advocate.server import _github_capability

        assert _github_capability("POST", "/repos/org/repo/issues") == "gh.write_branch"

    def test_post_to_pulls_maps_to_gh_open_pr(self) -> None:
        from advocate.server import _github_capability

        assert _github_capability("POST", "/repos/org/repo/pulls") == "gh.open_pr"

    def test_get_to_pulls_is_still_read(self) -> None:
        """Listing PRs (GET …/pulls) is a read, not open_pr — GET precedence holds."""
        from advocate.server import _github_capability

        assert _github_capability("GET", "/repos/org/repo/pulls") == "gh.read"

    def test_post_to_issue_comments_maps_to_gh_comment(self) -> None:
        from advocate.server import _github_capability

        assert _github_capability("POST", "/repos/org/repo/issues/1/comments") == "gh.comment"

    def test_post_to_pr_review_comments_maps_to_gh_comment(self) -> None:
        from advocate.server import _github_capability

        assert _github_capability("POST", "/repos/org/repo/pulls/1/comments") == "gh.comment"

    def test_open_pr_allowed_by_default_contract(self) -> None:
        """The default derived contract grants gh.open_pr → a PR-open passes the gate."""
        from advocate import idempotency
        from advocate.brokers import github_broker
        from advocate.server import AdvocateServer

        idempotency.clear()
        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg, operates_on=["org/repo-X"])
        secret = new_invocation_secret()
        envelope = sign_contract(c, secret)

        server = AdvocateServer(
            anthropic_key="dummy",
            github_token="dummy",
            contract_envelope=envelope,
            contract_signing_secret=secret,
        )
        server.start()
        mock_result = (201, {"number": 1, "html_url": "https://github.com/org/repo-X/pull/1"})
        try:
            with patch.object(github_broker, "_call_github", return_value=mock_result):
                payload = {
                    "action_id": "open-pr-1",
                    "method": "POST",
                    "path": "/repos/org/repo-X/pulls",
                    "body": {"title": "x", "head": "agent/fix", "base": "main"},
                }
                with _tcp_client(server.port) as client:
                    resp = client.post("/v1/github", json=payload, headers={"content-type": "application/json"})
            assert resp.status_code == 201
        finally:
            server.stop()

    def test_open_pr_denied_when_capability_withheld(self) -> None:
        """Granularity is now REAL: a contract granting gh.write_branch but NOT
        gh.open_pr must block PR creation (the reviewer's #4 point — removing
        gh.open_pr actually blocks the op now)."""
        from advocate import idempotency
        from advocate.brokers import github_broker
        from advocate.server import AdvocateServer

        idempotency.clear()
        # Hand-built contract: read + write_branch, but explicitly WITHOUT open_pr.
        contract = {
            "scope": {"repos": ["org/repo-X"], "writable_branches": ["agent/*"]},
            "capabilities": [CAP_GH_READ, CAP_GH_WRITE_BRANCH],
            "destinations": [f"{GITHUB_API_HOST}/repos/org/repo-X"],
            "irreversible_gate": ["gh.merge", "gh.delete"],
            "expiry": "this_invocation",
        }
        secret = new_invocation_secret()
        envelope = sign_contract(contract, secret)

        server = AdvocateServer(
            anthropic_key="dummy",
            github_token="dummy",
            contract_envelope=envelope,
            contract_signing_secret=secret,
        )
        server.start()
        try:
            with patch.object(github_broker, "_call_github", return_value=(201, {"number": 1})) as called:
                payload = {
                    "action_id": "open-pr-deny-1",
                    "method": "POST",
                    "path": "/repos/org/repo-X/pulls",
                    "body": {"title": "x", "head": "agent/fix", "base": "main"},
                }
                with _tcp_client(server.port) as client:
                    resp = client.post("/v1/github", json=payload, headers={"content-type": "application/json"})
            assert resp.status_code == 403
            assert called.call_count == 0, "a capability-denied PR-open must never reach upstream"
        finally:
            server.stop()

    def test_patch_maps_to_gh_write_branch(self) -> None:
        from advocate.server import _github_capability

        assert _github_capability("PATCH", "/repos/org/repo/issues/1") == "gh.write_branch"

    def test_bare_repo_patch_maps_to_gh_admin(self) -> None:
        """PATCH /repos/{o}/{r} (repo settings, incl. default_branch) → gh.admin."""
        from advocate.server import _github_capability

        assert _github_capability("PATCH", "/repos/org/repo") == "gh.admin"

    def test_branch_protection_maps_to_gh_admin(self) -> None:
        from advocate.server import _github_capability

        assert _github_capability("PUT", "/repos/org/repo/branches/main/protection") == "gh.admin"

    def test_repo_admin_write_denied_by_default_contract(self) -> None:
        """A bare-repo PATCH (default_branch change) is denied under the default contract."""
        from advocate import idempotency
        from advocate.server import AdvocateServer

        idempotency.clear()
        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg, operates_on=["org/repo-X"])
        secret = new_invocation_secret()
        envelope = sign_contract(c, secret)

        server = AdvocateServer(
            anthropic_key="dummy",
            github_token="dummy",
            contract_envelope=envelope,
            contract_signing_secret=secret,
        )
        server.start()
        try:
            payload = {
                "action_id": "admin-1",
                "method": "PATCH",
                "path": "/repos/org/repo-X",
                "body": {"default_branch": "attacker-controlled"},
            }
            with _tcp_client(server.port) as client:
                resp = client.post("/v1/github", json=payload, headers={"content-type": "application/json"})
            assert resp.status_code == 403
        finally:
            server.stop()

    def test_delete_via_uds_denied_by_default_contract(self) -> None:
        """DELETE under the default github contract → 403 (gh.delete not granted)."""
        from advocate import idempotency
        from advocate.server import AdvocateServer

        idempotency.clear()
        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg, operates_on=["org/repo-X"])
        secret = new_invocation_secret()
        envelope = sign_contract(c, secret)

        server = AdvocateServer(
            anthropic_key="dummy",
            github_token="dummy",
            contract_envelope=envelope,
            contract_signing_secret=secret,
        )
        server.start()
        try:
            payload = {
                "action_id": "cap-delete-1",
                "method": "DELETE",
                "path": "/repos/org/repo-X",
            }
            with _tcp_client(server.port) as client:
                resp = client.post(
                    "/v1/github",
                    json=payload,
                    headers={"content-type": "application/json"},
                )
            assert resp.status_code == 403
            body = resp.json()
            assert "error" in body
            # Denial must not mention internal contract fields or secrets.
            assert "secret" not in body["error"].lower()
        finally:
            server.stop()

    def test_merge_via_uds_denied_by_default_contract(self) -> None:
        """PUT to a /merge path under the default github contract → 403 (gh.merge not granted)."""
        from advocate import idempotency
        from advocate.server import AdvocateServer

        idempotency.clear()
        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg, operates_on=["org/repo-X"])
        secret = new_invocation_secret()
        envelope = sign_contract(c, secret)

        server = AdvocateServer(
            anthropic_key="dummy",
            github_token="dummy",
            contract_envelope=envelope,
            contract_signing_secret=secret,
        )
        server.start()
        try:
            payload = {
                "action_id": "cap-merge-1",
                "method": "PUT",
                "path": "/repos/org/repo-X/pulls/42/merge",
            }
            with _tcp_client(server.port) as client:
                resp = client.post(
                    "/v1/github",
                    json=payload,
                    headers={"content-type": "application/json"},
                )
            assert resp.status_code == 403
            body = resp.json()
            assert "error" in body
        finally:
            server.stop()

    def test_normal_branch_write_allowed_by_default_contract(self) -> None:
        """A normal POST (branch write) under the default github contract → passes gate."""
        from advocate import idempotency
        from advocate.brokers import github_broker
        from advocate.server import AdvocateServer

        idempotency.clear()
        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg, operates_on=["org/repo-X"])
        secret = new_invocation_secret()
        envelope = sign_contract(c, secret)

        server = AdvocateServer(
            anthropic_key="dummy",
            github_token="dummy",
            contract_envelope=envelope,
            contract_signing_secret=secret,
        )
        server.start()
        mock_result = (201, {"ref": "refs/heads/agent/test"})
        try:
            with patch.object(github_broker, "_call_github", return_value=mock_result):
                payload = {
                    "action_id": "cap-write-1",
                    "method": "POST",
                    "path": "/repos/org/repo-X/git/refs",
                    "body": {"ref": "refs/heads/agent/test", "sha": "abc123"},
                }
                with _tcp_client(server.port) as client:
                    resp = client.post(
                        "/v1/github",
                        json=payload,
                        headers={"content-type": "application/json"},
                    )
            assert resp.status_code == 201
        finally:
            server.stop()


# ---------------------------------------------------------------------------
# 8b. Percent-encoding capability bypass — classifier must see the routed path
# ---------------------------------------------------------------------------


class TestPercentEncodingCapabilityBypass:
    """GitHub percent-decodes path segments once before routing.

    The capability classifier, repo-scope, writable-branch and destination
    checks therefore MUST run on the once-decoded path; otherwise an agent can
    hide a privileged operation behind a percent-encoded letter and have the
    benign classification ride past the gate while GitHub executes the real,
    privileged endpoint with the injected PAT.
    """

    @staticmethod
    def _gated_server() -> object:
        from advocate import idempotency
        from advocate.server import AdvocateServer

        idempotency.clear()
        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg, operates_on=["org/repo-X"])
        secret = new_invocation_secret()
        envelope = sign_contract(c, secret)
        server = AdvocateServer(
            anthropic_key="dummy",
            github_token="dummy",
            contract_envelope=envelope,
            contract_signing_secret=secret,
        )
        server.start()
        return server

    def _assert_denied(self, payload: dict) -> None:
        from advocate.brokers import github_broker

        server = self._gated_server()
        try:
            # _call_github is mocked so a (hypothetical) bypass would NOT actually
            # reach GitHub — but if the gate let it through we would observe the
            # mocked 200 instead of a 403, which is exactly what we assert against.
            with patch.object(github_broker, "_call_github", return_value=(200, {"ok": True})) as called:
                with _tcp_client(server.port) as client:
                    resp = client.post(
                        "/v1/github",
                        json=payload,
                        headers={"content-type": "application/json"},
                    )
            assert resp.status_code == 403, f"expected gate denial, got {resp.status_code}: {resp.text}"
            assert called.call_count == 0, "denied request must never reach the upstream call"
        finally:
            server.stop()  # type: ignore[attr-defined]

    def test_encoded_merge_denied(self) -> None:
        """PUT /pulls/42/%6Derge (%6D='m') must be denied — it routes to /merge."""
        self._assert_denied({"action_id": "enc-merge", "method": "PUT", "path": "/repos/org/repo-X/pulls/42/%6Derge"})

    def test_encoded_protection_denied(self) -> None:
        """PUT /branches/main/%70rotection (%70='p') must be denied — gh.admin endpoint."""
        self._assert_denied(
            {
                "action_id": "enc-prot",
                "method": "PUT",
                "path": "/repos/org/repo-X/branches/main/%70rotection",
            }
        )

    def test_encoded_ref_to_main_denied(self) -> None:
        """PATCH /git/refs/%68eads/main (%68='h') must be denied — writes protected main."""
        self._assert_denied(
            {
                "action_id": "enc-ref",
                "method": "PATCH",
                "path": "/repos/org/repo-X/git/refs/%68eads/main",
                "body": {"sha": "deadbeef", "force": True},
            }
        )

    def test_double_encoded_path_rejected_at_validate(self) -> None:
        """A multiply percent-encoded path is rejected at validation (fail-closed)."""
        from advocate.brokers.github_broker import validate

        validated, err = validate({"action_id": "dbl", "method": "PUT", "path": "/repos/org/repo-X/pulls/42/%256Derge"})
        assert validated is None
        assert err is not None

    def test_validate_canonicalizes_path(self) -> None:
        """validate exposes the once-decoded path used for gating decisions."""
        from advocate.brokers.github_broker import validate

        validated, err = validate({"action_id": "canon", "method": "PUT", "path": "/repos/org/repo-X/pulls/42/%6Derge"})
        assert err is None
        assert validated is not None
        assert validated["path_canonical"] == "/repos/org/repo-X/pulls/42/merge"
        # The raw path is preserved verbatim for the outbound wire request.
        assert validated["path"] == "/repos/org/repo-X/pulls/42/%6Derge"


# ---------------------------------------------------------------------------
# 9. Writable-branch enforcement (HIGH-3) — push to main denied, agent/* allowed
# ---------------------------------------------------------------------------


class TestWritableBranchGate:
    """The gate must deny ref-targeting writes to branches outside writable_branches."""

    def test_branch_allowed_glob(self) -> None:
        from advocate.gate import _branch_allowed

        assert _branch_allowed("agent/fix-1", ["agent/*"]) is True
        assert _branch_allowed("main", ["agent/*"]) is False
        assert _branch_allowed("agent", ["agent/*"]) is False  # no slash → not under agent/*
        assert _branch_allowed("anything", []) is False  # empty list denies

    def test_write_branch_extraction(self) -> None:
        from advocate.server import _github_write_branch

        # PATCH/DELETE on a ref path
        assert _github_write_branch("PATCH", "/repos/o/r/git/refs/heads/main", None) == "main"
        assert _github_write_branch("DELETE", "/repos/o/r/git/refs/heads/agent/x", None) == "agent/x"
        # POST create-ref with body.ref
        assert _github_write_branch("POST", "/repos/o/r/git/refs", {"ref": "refs/heads/main"}) == "main"
        # PUT contents with body.branch
        assert _github_write_branch("PUT", "/repos/o/r/contents/f.txt", {"branch": "main"}) == "main"
        # Reads and non-branch writes return None
        assert _github_write_branch("GET", "/repos/o/r", None) is None
        assert _github_write_branch("POST", "/repos/o/r/issues", {"title": "x"}) is None

    def test_check_action_denies_main_branch(self) -> None:
        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg, operates_on=["org/repo-X"])  # writable_branches=["agent/*"]
        result = check_action(
            c,
            capability=CAP_GH_WRITE_BRANCH,
            destination=f"{GITHUB_API_HOST}/repos/org/repo-X/git/refs",
            scope_repo="org/repo-X",
            write_branch="main",
        )
        assert isinstance(result, GateDenial)
        assert result.failed_check == "branch"

    def test_check_action_allows_agent_branch(self) -> None:
        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg, operates_on=["org/repo-X"])
        result = check_action(
            c,
            capability=CAP_GH_WRITE_BRANCH,
            destination=f"{GITHUB_API_HOST}/repos/org/repo-X/git/refs",
            scope_repo="org/repo-X",
            write_branch="agent/fix-1",
        )
        assert isinstance(result, GateAllow)

    def test_path_traversal_repo_escape_denied_via_uds(self) -> None:
        """`/repos/org/repo-X/../other` must NOT reach another repo (httpx collapses `..`)."""
        from advocate.brokers import github_broker
        from advocate.server import AdvocateServer

        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg, operates_on=["org/repo-X"])
        secret = new_invocation_secret()
        envelope = sign_contract(c, secret)

        server = AdvocateServer(
            anthropic_key="dummy",
            github_token="dummy",
            github_allowed_destinations=["/repos/org/repo-X"],
            contract_envelope=envelope,
            contract_signing_secret=secret,
        )
        server.start()
        try:
            # If the broker ever called upstream, this would explode the test.
            with patch.object(github_broker, "_call_github", side_effect=AssertionError("must not reach upstream")):
                for path in ("/repos/org/repo-X/../other-repo", "/repos/org/repo-X/../../org/secret"):
                    payload = {"action_id": "trav-1", "method": "GET", "path": path}
                    with _tcp_client(server.port) as client:
                        resp = client.post("/v1/github", json=payload, headers={"content-type": "application/json"})
                    assert resp.status_code in (400, 403), f"{path!r} expected deny, got {resp.status_code}"
        finally:
            server.stop()

    def test_contents_write_without_branch_denied_via_uds(self) -> None:
        """A Contents-API PUT with no explicit branch defaults to main → denied."""
        from advocate.server import AdvocateServer

        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg, operates_on=["org/repo-X"])
        secret = new_invocation_secret()
        envelope = sign_contract(c, secret)

        server = AdvocateServer(
            anthropic_key="dummy",
            github_token="dummy",
            contract_envelope=envelope,
            contract_signing_secret=secret,
        )
        server.start()
        try:
            payload = {
                "action_id": "contents-1",
                "method": "PUT",
                "path": "/repos/org/repo-X/contents/app.py",
                "body": {"message": "x", "content": "eA=="},  # no "branch" → default branch
            }
            with _tcp_client(server.port) as client:
                resp = client.post("/v1/github", json=payload, headers={"content-type": "application/json"})
            assert resp.status_code == 403
            assert "branch" in resp.json()["error"]
        finally:
            server.stop()

    def test_push_to_main_denied_via_uds(self) -> None:
        """End-to-end: a PATCH that force-moves refs/heads/main → 403 at the gate."""
        from advocate.server import AdvocateServer

        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg, operates_on=["org/repo-X"])
        secret = new_invocation_secret()
        envelope = sign_contract(c, secret)

        server = AdvocateServer(
            anthropic_key="dummy",
            github_token="dummy",
            contract_envelope=envelope,
            contract_signing_secret=secret,
        )
        server.start()
        try:
            payload = {
                "action_id": "branch-deny-1",
                "method": "PATCH",
                "path": "/repos/org/repo-X/git/refs/heads/main",
                "body": {"sha": "deadbeef", "force": True},
            }
            with _tcp_client(server.port) as client:
                resp = client.post("/v1/github", json=payload, headers={"content-type": "application/json"})
            assert resp.status_code == 403
            assert "branch" in resp.json()["error"]
        finally:
            server.stop()


# ---------------------------------------------------------------------------
# 10. Anthropic endpoint is gated (HIGH-4)
# ---------------------------------------------------------------------------


class TestAnthropicGate:
    """The Anthropic path must be denied when the contract lacks the capability."""

    def test_anthropic_denied_when_capability_absent(self) -> None:
        """A signed contract WITHOUT the 'anthropic' capability → /v1/messages 403.

        derive_contract always grants it, so we hand-build + sign a contract that
        omits it to prove the gate is actually wired on the Anthropic path.
        """
        from advocate.server import AdvocateServer

        contract = {
            "scope": {"repos": [], "writable_branches": ["agent/*"]},
            "capabilities": [],  # no 'anthropic'
            "destinations": [DEST_ANTHROPIC],
            "irreversible_gate": [],
            "expiry": "this_invocation",
        }
        secret = new_invocation_secret()
        envelope = sign_contract(contract, secret)

        server = AdvocateServer(
            anthropic_key="dummy",
            contract_envelope=envelope,
            contract_signing_secret=secret,
        )
        server.start()
        try:
            payload = {"model": "claude-opus-4-8", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]}
            with _tcp_client(server.port) as client:
                resp = client.post("/v1/messages", json=payload, headers={"content-type": "application/json"})
            assert resp.status_code == 403
        finally:
            server.stop()

    def test_anthropic_allowed_with_normal_contract(self) -> None:
        """A normal derived contract carries 'anthropic' → the gate lets it through.

        We stub the upstream call so no network happens; only the gate is exercised.
        """
        from advocate import anthropic_proxy
        from advocate.server import AdvocateServer

        cfg = _make_cfg()
        contract = derive_contract(cfg)
        secret = new_invocation_secret()
        envelope = sign_contract(contract, secret)

        server = AdvocateServer(
            anthropic_key="dummy",
            contract_envelope=envelope,
            contract_signing_secret=secret,
        )
        server.start()
        try:
            with patch.object(anthropic_proxy, "handle_request_passthrough", return_value=(200, {"ok": True})):
                payload = {
                    "model": "claude-opus-4-8",
                    "max_tokens": 8,
                    "messages": [{"role": "user", "content": "hi"}],
                }
                with _tcp_client(server.port) as client:
                    resp = client.post("/v1/messages", json=payload, headers={"content-type": "application/json"})
            assert resp.status_code == 200
        finally:
            server.stop()
