"""Phase 4 contract + gate tests.

Security load-bearing assertions (spec §7, must stay green):
1. Off-contract capability → hard deny.
2. Off-contract destination (incl. path-boundary attempt for sibling repo) → hard deny.
3. In-scope dev action (repo-scoped declared capability) → passes.
4. Contract tamper (modify a field, signature no longer verifies) → rejected.
5. Denial is a clean error, not a crash, and does not leak secrets/internal detail.
6. Derivation is deterministic (same inputs → same contract/signature).
7. Gate via UDS — contract-gated server rejects off-contract GitHub/MCP calls as 403.
"""

from __future__ import annotations

import os
import tempfile
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


def _short_sock_path(name: str = "gate.sock") -> str:
    d = tempfile.mkdtemp(prefix="gatetest_", dir="/tmp")  # noqa: S108
    return os.path.join(d, name)


def _uds_client(sock_path: str) -> httpx.Client:
    transport = httpx.HTTPTransport(uds=sock_path)
    return httpx.Client(transport=transport)


# ---------------------------------------------------------------------------
# 1. derive_contract — determinism + shape
# ---------------------------------------------------------------------------


class TestDeriveContract:
    def test_same_inputs_yield_same_contract(self) -> None:
        """Derivation is deterministic: same inputs → identical contract dict."""
        cfg = _make_cfg(github_enabled=True)
        c1 = derive_contract(cfg, operates_on="org/repo-X")
        c2 = derive_contract(cfg, operates_on="org/repo-X")
        assert c1 == c2

    def test_github_enabled_adds_capabilities(self) -> None:
        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg, operates_on="org/repo-X")
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
        c = derive_contract(cfg, operates_on="org/repo-X")
        dests = c["destinations"]
        # The scoped destination must be present.
        assert f"{GITHUB_API_HOST}/repos/org/repo-X" in dests
        # The broad host-only destination must NOT be present when scoped.
        assert GITHUB_API_HOST not in dests

    def test_no_operates_on_uses_broad_github_destination(self) -> None:
        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg)
        assert GITHUB_API_HOST in c["destinations"]

    def test_repos_scope_set_when_operates_on_given(self) -> None:
        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg, operates_on="org/repo-X")
        assert c["scope"]["repos"] == ["org/repo-X"]

    def test_repos_scope_empty_without_operates_on(self) -> None:
        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg)
        assert c["scope"]["repos"] == []

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
        contract = derive_contract(cfg, operates_on="org/repo-X")
        envelope = sign_contract(contract, secret)
        assert verify_contract(envelope, secret) is True

    def test_tamper_capabilities_fails_verification(self) -> None:
        """Modifying capabilities after signing → verify returns False."""
        secret = new_invocation_secret()
        cfg = _make_cfg(github_enabled=True)
        contract = derive_contract(cfg, operates_on="org/repo-X")
        envelope = sign_contract(contract, secret)

        # Tamper: widen capabilities post-signing.
        envelope["contract"]["capabilities"].append("gh.merge")

        assert verify_contract(envelope, secret) is False

    def test_tamper_destination_fails_verification(self) -> None:
        """Adding a destination after signing → verify returns False."""
        secret = new_invocation_secret()
        cfg = _make_cfg(github_enabled=True)
        contract = derive_contract(cfg, operates_on="org/repo-X")
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
        c = derive_contract(cfg, operates_on="org/repo-X")
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
    def _gh_contract(self, *, operates_on: str = "org/repo-X") -> dict:
        cfg = _make_cfg(github_enabled=True)
        return derive_contract(cfg, operates_on=operates_on)

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
        c = self._gh_contract(operates_on="org/repo-X")
        result = check_action(
            c,
            capability=CAP_GH_READ,
            destination=f"{GITHUB_API_HOST}/repos/org/repo-X",
            scope_repo="org/repo-Y",  # wrong repo
        )
        assert isinstance(result, GateDenial)
        assert result.failed_check == "scope"

    def test_scope_check_skipped_when_repos_empty(self) -> None:
        """When scope.repos is empty (no operates_on), the scope check is a no-op."""
        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg)  # no operates_on → repos = []
        # Even if we pass a scope_repo, the gate should not deny on scope.
        result = check_action(
            c,
            capability=CAP_GH_READ,
            destination=GITHUB_API_HOST,
            scope_repo="org/any-repo",
        )
        # capability and destination match → should pass despite scope_repo given
        assert isinstance(result, GateAllow)

    # --- Denial is clean, not a crash ---

    def test_denial_has_reason_not_secret_detail(self) -> None:
        """GateDenial.reason must not contain signing secrets or internal contract dump."""
        secret = new_invocation_secret()
        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg, operates_on="org/repo-X")
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
# 6. Gate wired into AdvocateServer via UDS (end-to-end)
# ---------------------------------------------------------------------------


class TestGateViaUds:
    """The AdvocateServer must gate GitHub and MCP calls against the contract
    when a signed contract envelope is configured."""

    def _make_server_with_contract(self, sock_path: str) -> tuple:
        """Return (server, secret, envelope) for a server gated to ``org/repo-X``."""
        from advocate.server import AdvocateServer

        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg, operates_on="org/repo-X")
        secret = new_invocation_secret()
        envelope = sign_contract(c, secret)

        server = AdvocateServer(
            sock_path,
            anthropic_key="dummy",
            github_token="dummy-gh-token",
            contract_envelope=envelope,
            contract_signing_secret=secret,
        )
        return server, secret, envelope

    def test_in_scope_github_get_passes_gate(self) -> None:
        """A GET to the scoped repo passes the contract gate and reaches the broker."""
        from advocate import idempotency
        from advocate.brokers import github_broker

        idempotency.clear()
        sock_path = _short_sock_path("gate_allow.sock")
        server, _secret, _env = self._make_server_with_contract(sock_path)
        server.start()

        mock_result = (200, {"id": 1, "full_name": "org/repo-X"})
        try:
            with patch.object(github_broker, "_call_github", return_value=mock_result):
                payload = {
                    "action_id": "gate-allow-1",
                    "method": "GET",
                    "path": "/repos/org/repo-X",
                }
                with _uds_client(sock_path) as client:
                    resp = client.post(
                        "http://localhost/v1/github",
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

        sock_path = _short_sock_path("gate_deny_cap.sock")
        cfg = _make_cfg(github_enabled=False)  # no GH caps
        c = derive_contract(cfg)
        secret = new_invocation_secret()
        envelope = sign_contract(c, secret)

        server = AdvocateServer(
            sock_path,
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
            with _uds_client(sock_path) as client:
                resp = client.post(
                    "http://localhost/v1/github",
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
        sock_path = _short_sock_path("gate_deny_dest.sock")
        server, _s, _e = self._make_server_with_contract(sock_path)
        server.start()
        try:
            payload = {
                "action_id": "gate-deny-dest-1",
                "method": "GET",
                "path": "/repos/org/repo-X-evil/contents",
            }
            with _uds_client(sock_path) as client:
                resp = client.post(
                    "http://localhost/v1/github",
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

        sock_path = _short_sock_path("gate_tamper.sock")
        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg, operates_on="org/repo-X")
        secret = new_invocation_secret()
        envelope = sign_contract(c, secret)

        # Tamper: widen scope after signing.
        envelope["contract"]["capabilities"].append("gh.merge")

        server = AdvocateServer(
            sock_path,
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
            with _uds_client(sock_path) as client:
                resp = client.post(
                    "http://localhost/v1/github",
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
        sock_path = _short_sock_path("gate_compat.sock")
        # No contract_envelope / secret → gate is a no-op.
        server = AdvocateServer(sock_path, anthropic_key="dummy", github_token="dummy")
        server.start()

        mock_result = (200, {"id": 99})
        try:
            with patch.object(github_broker, "_call_github", return_value=mock_result):
                payload = {
                    "action_id": "compat-1",
                    "method": "GET",
                    "path": "/repos/any/repo",
                }
                with _uds_client(sock_path) as client:
                    resp = client.post(
                        "http://localhost/v1/github",
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
        c = derive_contract(cfg, operates_on="org/repo-X")
        secret = new_invocation_secret()
        envelope = sign_contract(c, secret)

        with pytest.raises(ValueError, match="both be provided together or both omitted"):
            AdvocateServer(
                "/tmp/unused.sock",  # noqa: S108
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
                "/tmp/unused.sock",  # noqa: S108
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
        from advocate.server import _Handler, _UnixServer

        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg, operates_on="org/repo-X")
        secret = new_invocation_secret()
        envelope = sign_contract(c, secret)

        # Bypass the AdvocateServer constructor check by directly constructing
        # _UnixServer (which does NOT validate the mismatch — only AdvocateServer
        # does).  This simulates a future code path that might bypass the outer guard.
        sock_path = _short_sock_path("gate_failclosed.sock")
        # _UnixServer does not have the same mismatch guard, so we can construct it
        # with a mismatched pair to exercise _check_gate's own defence.
        unix_server = _UnixServer(
            sock_path,
            _Handler,
            "dummy-key",
            github_token="dummy",
            contract_envelope=envelope,
            contract_signing_secret=None,  # mismatch — _check_gate should deny
        )

        # Simulate calling _check_gate directly by creating a minimal _Handler instance.
        # We reach in via a unit-test shim rather than a live UDS round-trip.
        handler = _Handler.__new__(_Handler)
        handler.server = unix_server  # type: ignore[attr-defined]

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
        unix_server.socket.close()


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

    def test_patch_maps_to_gh_write_branch(self) -> None:
        from advocate.server import _github_capability

        assert _github_capability("PATCH", "/repos/org/repo/issues/1") == "gh.write_branch"

    def test_delete_via_uds_denied_by_default_contract(self) -> None:
        """DELETE under the default github contract → 403 (gh.delete not granted)."""
        from advocate import idempotency
        from advocate.server import AdvocateServer

        idempotency.clear()
        sock_path = _short_sock_path("cap_delete.sock")
        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg, operates_on="org/repo-X")
        secret = new_invocation_secret()
        envelope = sign_contract(c, secret)

        server = AdvocateServer(
            sock_path,
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
            with _uds_client(sock_path) as client:
                resp = client.post(
                    "http://localhost/v1/github",
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
        sock_path = _short_sock_path("cap_merge.sock")
        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg, operates_on="org/repo-X")
        secret = new_invocation_secret()
        envelope = sign_contract(c, secret)

        server = AdvocateServer(
            sock_path,
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
            with _uds_client(sock_path) as client:
                resp = client.post(
                    "http://localhost/v1/github",
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
        sock_path = _short_sock_path("cap_write.sock")
        cfg = _make_cfg(github_enabled=True)
        c = derive_contract(cfg, operates_on="org/repo-X")
        secret = new_invocation_secret()
        envelope = sign_contract(c, secret)

        server = AdvocateServer(
            sock_path,
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
                with _uds_client(sock_path) as client:
                    resp = client.post(
                        "http://localhost/v1/github",
                        json=payload,
                        headers={"content-type": "application/json"},
                    )
            assert resp.status_code == 201
        finally:
            server.stop()
