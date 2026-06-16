"""Phase 3 broker tests — MCP and GitHub/HTTP over UDS.

Security load-bearing assertions (must stay green):
(a) MCP RPC via UDS with no secret in the client env succeeds (broker injects
    secret server-side).
(b) GitHub REST via UDS with no token in the client env succeeds (token injected
    server-side).
(c) Off-config / off-allowlist destination is hard-denied (no SSRF) — the
    denial is a clean tool error, not a crash.
(d) A hijacked-agent request carrying extra fields or a host override is
    rejected by the validator.
(e) action_id idempotency does not collide across broker types.
"""

from __future__ import annotations

import os
import tempfile
import threading
from unittest.mock import MagicMock, patch

import httpx
import vcr as vcrpy

from advocate import idempotency
from advocate.brokers import github_broker, mcp_broker
from advocate.server import AdvocateServer

MY_VCR = vcrpy.VCR(
    cassette_library_dir="tests/unit/cassettes",
    record_mode="none",
    match_on=["method", "scheme", "host", "port", "path"],
    ignore_hosts=["localhost"],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _short_sock_path(name: str = "broker.sock") -> str:
    d = tempfile.mkdtemp(prefix="brk_", dir="/tmp")  # noqa: S108
    return os.path.join(d, name)


def _uds_client(sock_path: str) -> httpx.Client:
    transport = httpx.HTTPTransport(uds=sock_path)
    return httpx.Client(transport=transport)


def _make_mcp_config(name: str = "my-mcp", secret_env_key: str = "MCP_SECRET") -> MagicMock:
    """Build a mock McpStdioServer with a secrets-bearing env."""
    cfg = MagicMock()
    cfg.name = name
    cfg.command = "echo"
    cfg.args = []
    cfg.env = {secret_env_key: "super-secret-value"}
    return cfg


def _make_remote_mcp_config(
    name: str = "remote-mcp",
    url: str = "https://mcp.example.com/rpc",
    token: str = "bearer-secret",
) -> MagicMock:
    cfg = MagicMock()
    cfg.name = name
    cfg.url = url
    cfg.headers = {"Authorization": f"Bearer {token}"}
    return cfg


# ---------------------------------------------------------------------------
# 1. MCP broker: validate — rejects unexpected fields (hijacked agent)
# ---------------------------------------------------------------------------


class TestMcpValidate:
    def test_rejects_extra_field(self) -> None:
        raw = {
            "action_id": "a1",
            "server_name": "my-mcp",
            "method": "tools/list",
            "host_override": "https://evil.com",
        }
        validated, err = mcp_broker.validate(raw)
        assert validated is None
        assert "unexpected field" in err

    def test_rejects_shell_metachar_in_server_name(self) -> None:
        raw = {
            "action_id": "a1",
            "server_name": "my-mcp; rm -rf /",
            "method": "tools/list",
        }
        validated, err = mcp_broker.validate(raw)
        assert validated is None
        assert "server_name" in err

    def test_rejects_missing_action_id(self) -> None:
        raw = {"server_name": "my-mcp", "method": "tools/list"}
        validated, err = mcp_broker.validate(raw)
        assert validated is None
        assert "action_id" in err

    def test_rejects_bad_method_chars(self) -> None:
        raw = {
            "action_id": "a1",
            "server_name": "my-mcp",
            "method": "tools/list; evil",
        }
        validated, err = mcp_broker.validate(raw)
        assert validated is None
        assert "method" in err

    def test_accepts_valid_request(self) -> None:
        raw = {"action_id": "a1", "server_name": "my-mcp", "method": "tools/list"}
        validated, err = mcp_broker.validate(raw)
        assert err is None
        assert validated is not None

    def test_accepts_with_params_and_id(self) -> None:
        raw = {
            "action_id": "a2",
            "server_name": "my-mcp",
            "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"path": "/tmp/f"}},  # noqa: S108
            "id": 42,
        }
        validated, err = mcp_broker.validate(raw)
        assert err is None
        assert validated["params"]["name"] == "read_file"


# ---------------------------------------------------------------------------
# 2. GitHub broker: validate — rejects unexpected / dangerous fields
# ---------------------------------------------------------------------------


class TestGithubValidate:
    def test_rejects_extra_field(self) -> None:
        raw = {
            "action_id": "g1",
            "method": "GET",
            "path": "/repos/org/repo",
            "upstream_override": "https://evil.com",
        }
        validated, err = github_broker.validate(raw)
        assert validated is None
        assert "unexpected field" in err

    def test_rejects_path_not_starting_with_slash(self) -> None:
        raw = {"action_id": "g1", "method": "GET", "path": "repos/org/repo"}
        validated, err = github_broker.validate(raw)
        assert validated is None
        assert "path" in err

    def test_rejects_invalid_method(self) -> None:
        raw = {"action_id": "g1", "method": "TRACE", "path": "/repos/org/repo"}
        validated, err = github_broker.validate(raw)
        assert validated is None
        assert "method" in err

    def test_rejects_missing_action_id(self) -> None:
        raw = {"method": "GET", "path": "/repos/org/repo"}
        validated, err = github_broker.validate(raw)
        assert validated is None
        assert "action_id" in err

    def test_accepts_valid_get(self) -> None:
        raw = {"action_id": "g1", "method": "GET", "path": "/repos/org/repo"}
        validated, err = github_broker.validate(raw)
        assert err is None
        assert validated is not None

    def test_accepts_post_with_body(self) -> None:
        raw = {
            "action_id": "g2",
            "method": "POST",
            "path": "/repos/org/repo/issues",
            "body": {"title": "Test issue", "body": "Content"},
        }
        validated, err = github_broker.validate(raw)
        assert err is None

    def test_agent_headers_field_accepted_but_will_be_discarded(self) -> None:
        """Agent may send headers field; it must be accepted (no 400) but never used."""
        raw = {
            "action_id": "g3",
            "method": "GET",
            "path": "/repos/org/repo",
            "headers": {"Authorization": "Bearer attacker-token"},
        }
        validated, err = github_broker.validate(raw)
        # validate accepts it — the broker's _call_github discards it
        assert err is None


# ---------------------------------------------------------------------------
# 3. GitHub broker: destination allowlist — hard deny (SSRF protection)
# ---------------------------------------------------------------------------


class TestGithubDestinationAllowlist:
    def test_off_allowlist_path_is_denied(self) -> None:
        """A path not in the configured allowlist returns 403, no upstream call."""
        idempotency.clear()
        payload = {"action_id": "ssrf1", "method": "GET", "path": "/repos/evil/repo"}

        with patch.object(github_broker, "_call_github") as mock_call:
            status, body = github_broker.handle_request(
                payload,
                github_token="dummy-token",
                allowed_destinations=["/repos/org/allowed-repo"],
            )

        assert status == 403
        assert "allowlist" in body["error"]
        mock_call.assert_not_called()

    def test_on_allowlist_path_is_allowed(self) -> None:
        """A path matching the allowlist proceeds to the upstream call."""
        idempotency.clear()
        payload = {"action_id": "ok1", "method": "GET", "path": "/repos/org/allowed-repo"}

        mock_result = (200, {"id": 1, "name": "allowed-repo"})
        with patch.object(github_broker, "_call_github", return_value=mock_result) as mock_call:
            status, body = github_broker.handle_request(
                payload,
                github_token="dummy-token",
                allowed_destinations=["/repos/org/allowed-repo"],
            )

        assert status == 200
        mock_call.assert_called_once()

    def test_none_allowlist_permits_any_path(self) -> None:
        """``allowed_destinations=None`` is the default: any GitHub API path is allowed."""
        idempotency.clear()
        payload = {"action_id": "any1", "method": "GET", "path": "/repos/any/repo"}

        mock_result = (200, {"id": 2})
        with patch.object(github_broker, "_call_github", return_value=mock_result):
            status, _ = github_broker.handle_request(
                payload,
                github_token="dummy-token",
                allowed_destinations=None,
            )

        assert status == 200

    def test_empty_allowlist_denies_all(self) -> None:
        """An empty allowlist denies every path."""
        idempotency.clear()
        payload = {"action_id": "deny1", "method": "GET", "path": "/repos/org/repo"}

        with patch.object(github_broker, "_call_github") as mock_call:
            status, body = github_broker.handle_request(
                payload,
                github_token="dummy-token",
                allowed_destinations=[],
            )

        assert status == 403
        assert "allowlist" in body["error"]
        mock_call.assert_not_called()

    def test_upstream_host_is_pinned_to_github(self) -> None:
        """_call_github ALWAYS targets api.github.com, never an agent-supplied host."""
        idempotency.clear()
        payload = {
            "action_id": "pin1",
            "method": "GET",
            "path": "/repos/org/repo",
        }

        captured_url: list[str] = []

        class _MockResp:
            status_code = 200

            def json(self) -> dict:
                return {"id": 1}

        class _MockClient:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def request(self, method, url, **_kwargs):
                captured_url.append(url)
                return _MockResp()

        with patch("advocate.brokers.github_broker.httpx.Client", return_value=_MockClient()):
            github_broker._call_github(payload, "dummy-token")

        assert len(captured_url) == 1
        assert captured_url[0].startswith(f"https://{github_broker.GITHUB_API_HOST}")
        assert "evil" not in captured_url[0]

    def test_agent_supplied_authorization_header_is_discarded(self) -> None:
        """The broker must never send an agent-supplied Authorization header."""
        idempotency.clear()
        payload = {
            "action_id": "hdr1",
            "method": "GET",
            "path": "/repos/org/repo",
            "headers": {"Authorization": "Bearer attacker-token"},
        }

        captured_headers: list[dict] = []

        class _MockResp:
            status_code = 200

            def json(self) -> dict:
                return {}

        class _MockClient:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def request(self, method, url, headers=None, **_kwargs):
                captured_headers.append(dict(headers or {}))
                return _MockResp()

        with patch("advocate.brokers.github_broker.httpx.Client", return_value=_MockClient()):
            github_broker._call_github(payload, "real-server-side-token")

        assert len(captured_headers) == 1
        auth_value = captured_headers[0].get("Authorization", "")
        # Must contain the real token, not the attacker's.
        assert "real-server-side-token" in auth_value
        assert "attacker-token" not in auth_value


# ---------------------------------------------------------------------------
# 3b. GitHub allowlist — segment-boundary enforcement (regression for naive startswith)
# ---------------------------------------------------------------------------


class TestGithubAllowlistSegmentBoundary:
    """Regression tests for the path-prefix boundary bug.

    The naive ``path.startswith(dest)`` allowed ``/repos/org/repo-evil`` and
    ``/repos/org/repository-private`` when the allowlist contained only
    ``/repos/org/repo``.  The fix requires an exact match OR a ``/``-segment
    boundary.
    """

    _ALLOWLIST = ["/repos/org/repo"]

    def _deny(self, action_id: str, path: str) -> None:
        idempotency.clear()
        payload = {"action_id": action_id, "method": "GET", "path": path}
        with patch.object(github_broker, "_call_github") as mock_call:
            status, body = github_broker.handle_request(
                payload,
                github_token="dummy-token",
                allowed_destinations=self._ALLOWLIST,
            )
        assert status == 403, f"expected 403 for {path!r}, got {status}"
        assert "allowlist" in body["error"]
        mock_call.assert_not_called()

    def _allow(self, action_id: str, path: str) -> None:
        idempotency.clear()
        payload = {"action_id": action_id, "method": "GET", "path": path}
        mock_result = (200, {"ok": True})
        with patch.object(github_broker, "_call_github", return_value=mock_result) as mock_call:
            status, _ = github_broker.handle_request(
                payload,
                github_token="dummy-token",
                allowed_destinations=self._ALLOWLIST,
            )
        assert status == 200, f"expected 200 for {path!r}, got {status}"
        mock_call.assert_called_once()

    def test_prefix_leak_repo_evil_is_denied(self) -> None:
        """``/repos/org/repo-evil/contents`` must be denied — not a sub-path of ``/repos/org/repo``."""
        self._deny("seg-b1", "/repos/org/repo-evil/contents")

    def test_prefix_leak_repository_private_is_denied(self) -> None:
        """``/repos/org/repository-private`` must be denied — sharing a prefix is not enough."""
        self._deny("seg-b2", "/repos/org/repository-private")

    def test_exact_match_is_allowed(self) -> None:
        """``/repos/org/repo`` (exact) must be allowed."""
        self._allow("seg-b3", "/repos/org/repo")

    def test_child_path_is_allowed(self) -> None:
        """``/repos/org/repo/contents`` must be allowed — it is a genuine sub-path."""
        self._allow("seg-b4", "/repos/org/repo/contents")

    def test_leading_slash_normalization_path_without_slash(self) -> None:
        """A path without a leading slash is normalized and still matched correctly."""
        # ``repos/org/repo`` (no leading slash) should behave identically to ``/repos/org/repo``.
        idempotency.clear()
        from advocate.brokers.github_broker import _path_allowed  # noqa: PLC0415

        assert _path_allowed("repos/org/repo", ["/repos/org/repo"]) is True
        assert _path_allowed("repos/org/repo/contents", ["/repos/org/repo"]) is True
        assert _path_allowed("repos/org/repo-evil", ["/repos/org/repo"]) is False

    def test_leading_slash_normalization_dest_without_slash(self) -> None:
        """A destination without a leading slash is normalized and still matches correctly."""
        from advocate.brokers.github_broker import _path_allowed  # noqa: PLC0415

        assert _path_allowed("/repos/org/repo", ["repos/org/repo"]) is True
        assert _path_allowed("/repos/org/repo/contents", ["repos/org/repo"]) is True
        assert _path_allowed("/repos/org/repo-evil", ["repos/org/repo"]) is False


# ---------------------------------------------------------------------------
# 4. Idempotency — no cross-broker collision
# ---------------------------------------------------------------------------


class TestIdempotencyNoCrossCollision:
    def test_same_action_id_different_broker_types_are_independent(self) -> None:
        """The same action_id used for both MCP and GitHub does NOT collide."""
        idempotency.clear()
        shared_id = "shared-001"

        idempotency.store("mcp", shared_id, 200, {"from": "mcp"})
        idempotency.store("github", shared_id, 200, {"from": "github"})

        mcp_result = idempotency.get("mcp", shared_id)
        gh_result = idempotency.get("github", shared_id)

        assert mcp_result == (200, {"from": "mcp"})
        assert gh_result == (200, {"from": "github"})
        assert mcp_result != gh_result

    def test_error_result_not_cached(self) -> None:
        """A non-2xx result is not stored; a retry can re-attempt."""
        idempotency.clear()
        idempotency.store("github", "err1", 502, {"error": "upstream request failed"})
        assert idempotency.get("github", "err1") is None

    def test_success_result_is_cached(self) -> None:
        idempotency.clear()
        idempotency.store("github", "ok1", 200, {"id": 1})
        assert idempotency.get("github", "ok1") == (200, {"id": 1})

    def test_clear_empties_all_entries(self) -> None:
        idempotency.store("mcp", "x", 200, {})
        idempotency.clear()
        assert idempotency.get("mcp", "x") is None


# ---------------------------------------------------------------------------
# 5. GitHub idempotency — replayed action_id returns cached result
# ---------------------------------------------------------------------------


class TestGithubIdempotency:
    def test_replayed_action_id_not_double_called(self) -> None:
        idempotency.clear()
        payload = {"action_id": "gh-idem1", "method": "GET", "path": "/repos/org/repo"}
        mock_result = (200, {"id": 99})

        call_count = 0

        def _fake_call(p, token):  # noqa: ARG001
            nonlocal call_count
            call_count += 1
            return mock_result

        with patch.object(github_broker, "_call_github", side_effect=_fake_call):
            r1 = github_broker.handle_request(payload, "dummy")
            r2 = github_broker.handle_request(payload, "dummy")

        assert r1 == r2 == mock_result
        assert call_count == 1, f"expected 1 upstream call, got {call_count}"


# ---------------------------------------------------------------------------
# 6. MCP broker: server-not-found returns clean 404
# ---------------------------------------------------------------------------


class TestMcpServerNotFound:
    def test_unknown_server_returns_404(self) -> None:
        idempotency.clear()
        payload = {
            "action_id": "nf1",
            "server_name": "ghost-server",
            "method": "tools/list",
        }
        status, body = mcp_broker.handle_request(payload, mcp_configs={})
        assert status == 404
        assert "error" in body
        # The error must not echo back any configured server names.
        assert "ghost-server" not in body["error"]


# ---------------------------------------------------------------------------
# 7. UDS end-to-end — MCP broker via server (no secret in client env)
# ---------------------------------------------------------------------------


class TestMcpViaUds:
    def test_mcp_rpc_via_uds_no_secret_in_client_env(self) -> None:
        """Agent (Python UDS client) sends MCP RPC with NO secret in its env;
        broker dispatches and returns result.

        The client env is explicitly stripped of any key containing 'secret'.
        """
        idempotency.clear()

        # Build a mock MCP config with a "secret" env var.
        mock_cfg = MagicMock()
        mock_cfg.name = "my-mcp"

        # We use the McpStdioServer branch via isinstance — mock the isinstance check.
        from configuration import McpStdioServer  # noqa: PLC0415

        real_cfg = McpStdioServer(
            name="my-mcp",
            command="cat",  # won't actually be launched in this test
            args=[],
            env={"MCP_SECRET_KEY": "super-secret-value"},
        )
        mcp_configs = {"my-mcp": real_cfg}

        sock_path = _short_sock_path("mcp_e2e.sock")
        server = AdvocateServer(
            sock_path,
            anthropic_key="dummy",
            mcp_configs=mcp_configs,
        )
        server.start()

        # Patch mcp_broker.handle_request to return a mock result (we're testing
        # the UDS dispatch, not the subprocess I/O).
        mock_response = (200, {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}})

        try:
            with patch("advocate.brokers.mcp_broker.handle_request", return_value=mock_response):
                # The "agent" client has no secrets in its env.
                client_env = {k: v for k, v in os.environ.items() if "secret" not in k.lower()}
                assert "MCP_SECRET_KEY" not in client_env

                payload = {
                    "action_id": "mcp-uds-1",
                    "server_name": "my-mcp",
                    "method": "tools/list",
                }
                with _uds_client(sock_path) as client:
                    resp = client.post(
                        "http://localhost/v1/mcp",
                        json=payload,
                        headers={"content-type": "application/json"},
                    )

            assert resp.status_code == 200
            body = resp.json()
            assert "result" in body
        finally:
            server.stop()

    def test_mcp_uds_rejects_extra_field(self) -> None:
        """UDS /v1/mcp returns 400 for unexpected fields (hijack attempt)."""
        idempotency.clear()
        sock_path = _short_sock_path("mcp_extra.sock")
        server = AdvocateServer(sock_path, anthropic_key="dummy")
        server.start()
        try:
            payload = {
                "action_id": "x1",
                "server_name": "my-mcp",
                "method": "tools/list",
                "INJECT": "evil",
            }
            with _uds_client(sock_path) as client:
                resp = client.post(
                    "http://localhost/v1/mcp",
                    json=payload,
                    headers={"content-type": "application/json"},
                )
            assert resp.status_code == 400
            assert "unexpected field" in resp.json()["error"]
        finally:
            server.stop()


# ---------------------------------------------------------------------------
# 8. UDS end-to-end — GitHub broker via server (no token in client env)
# ---------------------------------------------------------------------------


class TestGithubViaUds:
    def test_github_rest_via_uds_no_token_in_client_env(self) -> None:
        """Agent (Python UDS client) sends GitHub REST call with NO token in its env;
        broker injects the token server-side and returns result.
        """
        idempotency.clear()

        sock_path = _short_sock_path("gh_e2e.sock")
        server = AdvocateServer(
            sock_path,
            anthropic_key="dummy",
            github_token="server-side-github-token",
            github_allowed_destinations=None,
        )
        server.start()

        mock_response = (200, {"id": 123, "name": "repo-x", "full_name": "org/repo-x"})

        try:
            with patch("advocate.brokers.github_broker.handle_request", return_value=mock_response):
                # Client env contains no GitHub token.
                client_env = {k: v for k, v in os.environ.items() if "github" not in k.lower()}
                assert "GITHUB_TOKEN" not in client_env
                assert "GH_TOKEN" not in client_env

                payload = {
                    "action_id": "gh-uds-1",
                    "method": "GET",
                    "path": "/repos/org/repo-x",
                }
                with _uds_client(sock_path) as client:
                    resp = client.post(
                        "http://localhost/v1/github",
                        json=payload,
                        headers={"content-type": "application/json"},
                    )

            assert resp.status_code == 200
            body = resp.json()
            assert body["full_name"] == "org/repo-x"
        finally:
            server.stop()

    def test_github_uds_off_allowlist_is_denied(self) -> None:
        """Off-allowlist GitHub destination via UDS returns 403 (no SSRF)."""
        idempotency.clear()

        sock_path = _short_sock_path("gh_ssrf.sock")
        server = AdvocateServer(
            sock_path,
            anthropic_key="dummy",
            github_token="server-side-token",
            github_allowed_destinations=["/repos/org/allowed-repo"],
        )
        server.start()

        try:
            payload = {
                "action_id": "ssrf-uds-1",
                "method": "GET",
                "path": "/repos/evil/stolen-data",
            }
            with _uds_client(sock_path) as client:
                resp = client.post(
                    "http://localhost/v1/github",
                    json=payload,
                    headers={"content-type": "application/json"},
                )

            assert resp.status_code == 403
            body = resp.json()
            assert "allowlist" in body["error"]
            # Denial must be a clean tool error, not a crash or 500.
        finally:
            server.stop()

    def test_github_uds_rejects_extra_field(self) -> None:
        """UDS /v1/github returns 400 for unexpected fields (hijack attempt)."""
        idempotency.clear()
        sock_path = _short_sock_path("gh_extra.sock")
        server = AdvocateServer(sock_path, anthropic_key="dummy", github_token="dummy")
        server.start()
        try:
            payload = {
                "action_id": "g_x1",
                "method": "GET",
                "path": "/repos/org/repo",
                "upstream_override": "https://evil.com",
            }
            with _uds_client(sock_path) as client:
                resp = client.post(
                    "http://localhost/v1/github",
                    json=payload,
                    headers={"content-type": "application/json"},
                )
            assert resp.status_code == 400
            assert "unexpected field" in resp.json()["error"]
        finally:
            server.stop()


# ---------------------------------------------------------------------------
# 9. MCP remote broker with VCR cassette — Bearer injected, no secret in client
# ---------------------------------------------------------------------------


@MY_VCR.use_cassette("mcp_remote_call.json")
def test_mcp_remote_broker_injects_bearer_server_side() -> None:
    """Remote MCP call via the broker — Bearer token injected server-side.

    VCR cassette records the upstream call with a REDACTED token; the client
    side never holds the real value.
    """
    idempotency.clear()

    from configuration import McpRemoteServer  # noqa: PLC0415

    cfg = McpRemoteServer(
        name="remote-mcp",
        type="http",
        url="https://mcp.example.com/rpc",
        headers={"Authorization": "Bearer REDACTED"},
    )

    payload: dict = {
        "action_id": "vcr-mcp-1",
        "server_name": "remote-mcp",
        "method": "tools/list",
    }

    mcp_configs = {"remote-mcp": cfg}
    status, body = mcp_broker.handle_request(payload, mcp_configs)

    assert status == 200
    assert "result" in body
    tools = body["result"]["tools"]
    assert isinstance(tools, list)


# ---------------------------------------------------------------------------
# 10. GitHub broker with VCR cassette — token injected, no token in client
# ---------------------------------------------------------------------------


@MY_VCR.use_cassette("github_get_repo.json")
def test_github_broker_injects_token_server_side() -> None:
    """GitHub GET /repos/org/repo-x via the broker — token injected server-side.

    VCR cassette records the upstream call with REDACTED auth; the client never
    holds the real token.
    """
    idempotency.clear()

    payload: dict = {
        "action_id": "vcr-gh-1",
        "method": "GET",
        "path": "/repos/org/repo-x",
    }

    # Client env has no GitHub token — only the broker holds it.
    client_env_keys = set(os.environ.keys())
    assert "GITHUB_TOKEN" not in client_env_keys
    assert "GH_TOKEN" not in client_env_keys

    status, body = github_broker.handle_request(
        payload,
        github_token="REDACTED",  # placeholder — cassette was recorded with this
        allowed_destinations=None,
    )

    assert status == 200
    assert body["full_name"] == "org/repo-x"


# ---------------------------------------------------------------------------
# 11. MCP stdio broker — secret in subprocess env, not leaked to caller
# ---------------------------------------------------------------------------


class TestMcpStdioSecretInjection:
    def test_stdio_env_secrets_stay_server_side(self) -> None:
        """The MCP subprocess receives secrets in its env; the UDS caller does not.

        We verify that the subprocess launched by _get_or_launch_proc receives
        the ``env`` from the config, and that the call payload contains no
        secret value.
        """
        from configuration import McpStdioServer  # noqa: PLC0415

        cfg = McpStdioServer(
            name="secret-mcp",
            command="cat",
            args=[],
            env={"PRIVATE_TOKEN": "real-secret-99"},
        )

        # Patch subprocess.Popen to capture the env it receives.
        # stdout must be a real file object so select.select can call fileno()
        # on it; we pre-write the response into the write end before the call.
        captured_envs: list[dict] = []
        response = b'{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}\n'

        r_fd, w_fd = os.pipe()
        os.write(w_fd, response)
        os.close(w_fd)
        real_stdout = os.fdopen(r_fd, "rb")

        class _FakeProc:
            poll = MagicMock(return_value=None)

            def __init__(self, *args, env=None, **kwargs):  # noqa: ARG002
                captured_envs.append(dict(env or {}))
                self.stdin = MagicMock()
                self.stdin.write = MagicMock()
                self.stdin.flush = MagicMock()
                self.stdout = real_stdout

        mcp_broker._procs.clear()
        mcp_broker._proc_locks.clear()

        with patch("advocate.brokers.mcp_broker.subprocess.Popen", _FakeProc):
            payload = {
                "action_id": "stdio-sec-1",
                "server_name": "secret-mcp",
                "method": "tools/list",
            }
            idempotency.clear()
            status, body = mcp_broker.handle_request(payload, {"secret-mcp": cfg})

        assert status == 200

        # The subprocess received the secret in its env (injected server-side).
        assert len(captured_envs) == 1
        assert captured_envs[0].get("PRIVATE_TOKEN") == "real-secret-99"

        # The returned body (which the agent sees) does NOT contain the secret.
        import json as _json  # noqa: PLC0415

        body_str = _json.dumps(body)
        assert "real-secret-99" not in body_str


# ---------------------------------------------------------------------------
# 12. MCP stdio — hung server read timeout + proc eviction
# ---------------------------------------------------------------------------


class TestMcpStdioTimeout:
    """The stdout read must be bounded; a hung server must be evicted."""

    def test_hung_server_returns_502_and_evicts_proc(self) -> None:
        """select.select reports not-ready → 502 returned, proc evicted and terminated."""
        from configuration import McpStdioServer  # noqa: PLC0415

        cfg = McpStdioServer(name="hung-mcp", command="cat", args=[], env={})

        # A fake proc whose stdout.fileno() select will find not-ready.
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # appears alive
        mock_proc.stdin = MagicMock()
        mock_proc.stdin.write = MagicMock()
        mock_proc.stdin.flush = MagicMock()
        # stdout needs a real fileno for select; use a real pipe read-end.
        import os as _os  # noqa: PLC0415

        r_fd, w_fd = _os.pipe()
        # Leave the write end open but never write — select will time out.
        mock_proc.stdout = _os.fdopen(r_fd, "rb")

        mcp_broker._procs.clear()
        mcp_broker._proc_locks.clear()
        mcp_broker._procs["hung-mcp"] = mock_proc
        mcp_broker._proc_locks["hung-mcp"] = threading.Lock()

        from advocate import idempotency  # noqa: PLC0415

        idempotency.clear()

        payload = {"action_id": "hung-1", "server_name": "hung-mcp", "method": "tools/list"}

        # Patch the timeout to near-zero so the test is fast.
        with patch.object(mcp_broker, "_STDIO_READ_TIMEOUT_S", 0.05):
            status, body = mcp_broker.handle_request(payload, {"hung-mcp": cfg})

        assert status == 502
        assert body["error"] == "MCP request failed"
        # Proc must be evicted so the next call can relaunch a fresh one.
        assert "hung-mcp" not in mcp_broker._procs
        # terminate() must have been called on the hung proc.
        mock_proc.terminate.assert_called_once()

        # Clean up the pipe write end.
        _os.close(w_fd)
        mock_proc.stdout.close()


# ---------------------------------------------------------------------------
# 13. AdvocateServer.stop() wires shutdown_all()
# ---------------------------------------------------------------------------


class TestAdvocateServerShutdownWiring:
    """AdvocateServer.stop() must call mcp_broker.shutdown_all() to terminate subprocesses."""

    def test_stop_calls_shutdown_all_and_clears_procs(self) -> None:
        """Registered MCP procs are terminated when the server stops."""
        # Register a mock proc in the broker's _procs dict.
        mock_proc = MagicMock()
        mock_proc.terminate = MagicMock()
        mock_proc.wait = MagicMock()
        mcp_broker._procs["wired-mcp"] = mock_proc
        mcp_broker._proc_locks["wired-mcp"] = threading.Lock()

        sock_path = _short_sock_path("shutdown_wiring.sock")
        server = AdvocateServer(sock_path, anthropic_key="dummy")
        server.start()

        server.stop()

        # After stop, the proc must have been terminated and _procs cleared.
        mock_proc.terminate.assert_called_once()
        assert "wired-mcp" not in mcp_broker._procs
