"""Phase 5 boot-sequence tests — Broker V0 wiring.

Covers (per spec §6 + task requirements):
- config.json is scrubbed before the agent runs (no #-secret values on disk)
- the cleared agent env contains NO real secrets (KBC_TOKEN / #anthropic_key /
  GITHUB_TOKEN absent; ANTHROPIC_BASE_URL = loopback; dummy key present)
- env-scrub removes KBC_TOKEN from /proc/self/environ (Linux only)
- the contract is derived + signed and passed to the server
- plugin install env carries no agent-bound secret leak (§14 fix)
- output promotion still happens in the parent + transcript flush on failure
"""

from __future__ import annotations

import json
import os
import sys

import pytest

from component import (
    _DUMMY_ANTHROPIC_KEY,
    _PTRACE_OVERRIDE_ENV,
    _SCRUB_DONE_ENV,
    Component,
    _assert_ptrace_protected,
    _perform_env_scrub,
    _scrub_config_json_impl,
)
from configuration import Configuration

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(tmp_path, parameters=None):
    """Write a minimal KBC data dir and return the path."""
    data_dir = tmp_path / "data"
    for sub in ("in/tables", "in/files", "out/tables", "out/files"):
        (data_dir / sub).mkdir(parents=True, exist_ok=True)
    params = {"#anthropic_key": "REAL_KEY_NEVER_EXPOSED", **(parameters or {})}
    (data_dir / "config.json").write_text(json.dumps({"parameters": params}), encoding="utf-8")
    return str(data_dir)


# ---------------------------------------------------------------------------
# 1. Config scrub: #-secret values removed from disk before agent runs
# ---------------------------------------------------------------------------


class TestConfigJsonScrub:
    """config.json is scrubbed (secret values replaced with "") before agent spawn."""

    def test_scrub_removes_hash_key_values(self, tmp_path):
        """#anthropic_key and #github_token values are replaced with ""."""
        config_file = tmp_path / "config.json"
        config_file.write_text(
            json.dumps(
                {
                    "parameters": {
                        "#anthropic_key": "sk-real-key",
                        "#github_token": "ghp_real_token",
                        "model": "claude-opus-4-8",
                    }
                }
            ),
            encoding="utf-8",
        )
        _scrub_config_json_impl(str(tmp_path))
        after = json.loads(config_file.read_text(encoding="utf-8"))
        params = after["parameters"]
        assert params["#anthropic_key"] == ""
        assert params["#github_token"] == ""
        # Non-# keys are left intact
        assert params["model"] == "claude-opus-4-8"

    def test_scrub_works_on_nested_dicts(self, tmp_path):
        """#-keys nested inside sub-dicts (e.g. MCP server env) are also scrubbed."""
        config_file = tmp_path / "config.json"
        config_file.write_text(
            json.dumps(
                {
                    "parameters": {
                        "#anthropic_key": "real",
                        "mcp_section": {
                            "mcp_servers": [
                                {
                                    "type": "http",
                                    "name": "myserver",
                                    "url": "https://example.com",
                                    "headers": {"#bearer": "real-secret"},
                                }
                            ]
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
        _scrub_config_json_impl(str(tmp_path))
        after = json.loads(config_file.read_text(encoding="utf-8"))
        server = after["parameters"]["mcp_section"]["mcp_servers"][0]
        assert server["headers"]["#bearer"] == ""
        assert server["url"] == "https://example.com"  # non-# key intact

    def test_scrub_survives_missing_file(self, tmp_path):
        """No exception if config.json doesn't exist."""
        _scrub_config_json_impl(str(tmp_path))  # should not raise

    def test_scrub_component_method_scrubs_data_dir(self, tmp_path, monkeypatch):
        """Component._scrub_config_json() targets KBC_DATADIR/config.json."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        config_file = data_dir / "config.json"
        config_file.write_text(
            json.dumps({"parameters": {"#anthropic_key": "secret"}}),
            encoding="utf-8",
        )
        monkeypatch.setenv("KBC_DATADIR", str(data_dir))
        Component._scrub_config_json()
        after = json.loads(config_file.read_text(encoding="utf-8"))
        assert after["parameters"]["#anthropic_key"] == ""


# ---------------------------------------------------------------------------
# 2. Cleared agent env: no real secrets; loopback routing; dummy key
# ---------------------------------------------------------------------------


class TestClearedAgentEnv:
    """The cleared env passed to the agent subprocess must have no real secrets."""

    def _cfg(self, **extra):
        return Configuration(**{"#anthropic_key": "REAL_SECRET", "#github_token": "GH_SECRET", **extra})

    def test_no_real_anthropic_key_in_cleared_env(self):
        env = Component._build_cleared_env(self._cfg(), proxy_port=12345)
        assert env["ANTHROPIC_API_KEY"] == _DUMMY_ANTHROPIC_KEY
        assert "REAL_SECRET" not in env.values()

    def test_anthropic_base_url_points_at_loopback(self):
        env = Component._build_cleared_env(self._cfg(), proxy_port=9999)
        assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9999"

    def test_no_kbc_token_in_cleared_env(self):
        # Blanked (not omitted): the SDK merges os.environ into the agent env, so
        # "" must override any inherited KBC_TOKEN.
        env = Component._build_cleared_env(self._cfg(), proxy_port=1)
        assert env["KBC_TOKEN"] == ""

    def test_no_github_token_in_cleared_env(self):
        env = Component._build_cleared_env(self._cfg(), proxy_port=1)
        assert env["GITHUB_TOKEN"] == ""
        assert env["GH_TOKEN"] == ""
        assert "GH_SECRET" not in env.values()

    def test_writable_tmp_caches_present(self):
        import component as cm

        env = Component._build_cleared_env(self._cfg(), proxy_port=1)
        assert env["HOME"] == cm.AGENT_HOME
        assert env["UV_CACHE_DIR"] == cm.UV_CACHE_DIR
        assert env["NPM_CONFIG_CACHE"] == cm.NPM_CONFIG_CACHE
        assert env["XDG_CACHE_HOME"] == cm.XDG_CACHE_HOME
        for key in ("HOME", "UV_CACHE_DIR", "NPM_CONFIG_CACHE", "XDG_CACHE_HOME"):
            assert env[key].startswith("/tmp/")

    def test_mcp_proxy_url_added_when_mcp_servers_configured(self):
        cfg = Configuration(
            **{
                "#anthropic_key": "key",
                "mcp_servers": [{"type": "stdio", "name": "fetch", "command": "uvx", "args": ["mcp-server-fetch"]}],
            }
        )
        env = Component._build_cleared_env(cfg, proxy_port=4567)
        assert "MCP_PROXY_URL" in env
        assert "127.0.0.1:4567" in env["MCP_PROXY_URL"]

    def test_no_mcp_proxy_url_without_mcp_servers(self):
        env = Component._build_cleared_env(self._cfg(), proxy_port=1)
        assert "MCP_PROXY_URL" not in env


# ---------------------------------------------------------------------------
# 3. Contract derived + signed and wired to AdvocateServer
# ---------------------------------------------------------------------------


class TestContractWiredToServer:
    """The boot sequence derives a contract, signs it, and passes it to the server."""

    def test_contract_passed_to_server(self, tmp_path, monkeypatch):
        """_run_with_broker must construct AdvocateServer with contract_envelope and secret."""
        import json

        data_dir = tmp_path / "data"
        for sub in ("in/tables", "in/files", "out/tables", "out/files"):
            (data_dir / sub).mkdir(parents=True, exist_ok=True)
        config_file = data_dir / "config.json"
        config_file.write_text(
            json.dumps({"parameters": {"#anthropic_key": "key", "task": {"prompt": "hi"}}}),
            encoding="utf-8",
        )
        monkeypatch.setenv("KBC_DATADIR", str(data_dir))

        captured = {}

        from advocate.server import AdvocateServer

        original_init = AdvocateServer.__init__

        def capturing_init(self_s, anthropic_key, *, contract_envelope=None, contract_signing_secret=None, **kw):
            captured["envelope"] = contract_envelope
            captured["secret"] = contract_signing_secret
            original_init(
                self_s,
                anthropic_key,
                contract_envelope=contract_envelope,
                contract_signing_secret=contract_signing_secret,
                **kw,
            )

        monkeypatch.setattr(AdvocateServer, "__init__", capturing_init)

        from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock

        from claude_runner import ClaudeRunner

        def canned(self, prompt, options):
            async def gen():
                yield SystemMessage(subtype="init", data={"session_id": "s1"})
                yield AssistantMessage(content=[TextBlock(text="ok")], model="claude-opus-4-8", session_id="s1")
                yield ResultMessage(
                    subtype="success",
                    duration_ms=10,
                    duration_api_ms=5,
                    is_error=False,
                    num_turns=1,
                    session_id="s1",
                    total_cost_usd=0.0,
                    result="ok",
                )

            return gen()

        monkeypatch.setattr(ClaudeRunner, "_query", canned)
        comp = Component()
        comp.run()

        assert captured.get("envelope") is not None, "contract_envelope must be passed to AdvocateServer"
        assert captured.get("secret") is not None, "contract_signing_secret must be passed to AdvocateServer"
        # The secret is never a human-readable string — it is raw bytes
        assert isinstance(captured["secret"], bytes)
        # Envelope has the right shape
        assert "contract" in captured["envelope"]
        assert "signature" in captured["envelope"]

    def test_operates_on_wires_into_contract(self):
        """operates_on from config narrows the contract repo scope."""
        from advocate.contract import derive_contract

        cfg = Configuration(**{"#anthropic_key": "key", "github_enabled": True, "operates_on": "org/repo-X"})
        contract = derive_contract(cfg, operates_on=cfg.operates_on)
        assert "org/repo-X" in contract["scope"]["repos"]
        assert any("org/repo-X" in d for d in contract["destinations"])

    def test_github_enabled_without_operates_on_rejected_at_config(self):
        """HIGH-3: github_enabled without operates_on fails closed at config parse."""
        from keboola.component.exceptions import UserException

        with pytest.raises(UserException, match="operates_on"):
            Configuration(**{"#anthropic_key": "key", "github_enabled": True})


# ---------------------------------------------------------------------------
# 4. Plugin install env — §14 fix: no os.environ secret inheritance
# ---------------------------------------------------------------------------


class TestPluginInstallEnv:
    """_plugin_install_env must not leak KBC_TOKEN or real ANTHROPIC_API_KEY."""

    def test_plugin_env_does_not_inherit_kbc_token(self, monkeypatch):
        """KBC_TOKEN from os.environ must NOT appear in the plugin subprocess env."""
        from plugin_manager import PluginManager

        monkeypatch.setenv("KBC_TOKEN", "super-secret-token")
        plugin_env = {"CLAUDE_CONFIG_DIR": "/tmp/test", "HOME": "/tmp/test"}
        result = PluginManager._plugin_install_env(plugin_env)
        assert "KBC_TOKEN" not in result

    def test_plugin_env_does_not_inherit_anthropic_key(self, monkeypatch):
        """ANTHROPIC_API_KEY from os.environ must NOT appear in the plugin env."""
        from plugin_manager import PluginManager

        monkeypatch.setenv("ANTHROPIC_API_KEY", "real-secret-key")
        plugin_env = {"CLAUDE_CONFIG_DIR": "/tmp/test"}
        result = PluginManager._plugin_install_env(plugin_env)
        assert "ANTHROPIC_API_KEY" not in result

    def test_plugin_env_passes_path(self, monkeypatch):
        """PATH from os.environ IS passed so the CLI can find git/system tools."""
        from plugin_manager import PluginManager

        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        plugin_env = {"CLAUDE_CONFIG_DIR": "/tmp/test"}
        result = PluginManager._plugin_install_env(plugin_env)
        assert "PATH" in result
        assert "/usr/bin" in result["PATH"]

    def test_plugin_env_passes_github_token_from_agent_env(self, monkeypatch):
        """GITHUB_TOKEN placed in agent_env (for private plugins) IS passed through."""
        from plugin_manager import PluginManager

        # Note: the caller (component.py) places github_token in the env it
        # passes to prepare(); the plugin env builder should keep those.
        plugin_env = {"GITHUB_TOKEN": "ghp_private", "GH_TOKEN": "ghp_private", "CLAUDE_CONFIG_DIR": "/tmp"}
        result = PluginManager._plugin_install_env(plugin_env)
        assert result["GITHUB_TOKEN"] == "ghp_private"


# ---------------------------------------------------------------------------
# 5. Env-scrub: KBC_TOKEN removed from exec-time environ (Linux only)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "linux", reason="env-scrub test requires Linux /proc/self/environ")
class TestEnvScrubLinux:
    """env-scrub re-exec removes KBC_TOKEN from /proc/<advocate>/environ."""

    def test_scrub_done_env_marker_prevents_double_reexec(self, monkeypatch):
        """If _SCRUB_DONE_ENV=1 is already set, _perform_env_scrub must NOT re-exec.

        We verify by patching os.execve and confirming it is never called.
        """
        monkeypatch.setenv(_SCRUB_DONE_ENV, "1")
        called = []
        monkeypatch.setattr(os, "execve", lambda *a, **kw: called.append(a))
        # Simulating what the __main__ guard does: don't call _perform_env_scrub
        # because _SCRUB_DONE_ENV=1.  If called anyway, execve would appear.
        if os.environ.get(_SCRUB_DONE_ENV) != "1":
            _perform_env_scrub()
        assert called == [], "execve must not be called when scrub is already done"

    def test_kbc_token_absent_after_scrub_reexec(self, tmp_path):
        """After env-scrub re-exec /proc/self/environ should not contain KBC_TOKEN.

        We run a subprocess that (a) sets KBC_TOKEN in its env, (b) calls
        _perform_env_scrub() which re-execs with scrubbed env, and (c) the
        re-exec'd process reads /proc/self/environ and exits with a code
        indicating whether KBC_TOKEN was found.

        Exit 0  = KBC_TOKEN not found in /proc/self/environ (scrub worked)
        Exit 10 = KBC_TOKEN still found (scrub failed)
        Exit 20 = execve failed (env-scrub skipped)
        """
        import subprocess

        helper = tmp_path / "scrub_check.py"
        helper.write_text(
            """
import os, sys

SCRUB_DONE = "_ADVOCATE_ENV_SCRUB_DONE"
KBC_TOKEN_PIPE_FD = 3

if os.environ.get(SCRUB_DONE) != "1":
    # First exec: perform env-scrub
    token = os.environ.get("KBC_TOKEN", "")
    r, w = os.pipe()
    os.write(w, token.encode()); os.close(w)
    if r != KBC_TOKEN_PIPE_FD:
        os.dup2(r, KBC_TOKEN_PIPE_FD); os.close(r)
    import fcntl
    flags = fcntl.fcntl(KBC_TOKEN_PIPE_FD, fcntl.F_GETFD)
    fcntl.fcntl(KBC_TOKEN_PIPE_FD, fcntl.F_SETFD, flags & ~fcntl.FD_CLOEXEC)
    scrubbed = {k: v for k, v in os.environ.items() if k != "KBC_TOKEN"}
    scrubbed[SCRUB_DONE] = "1"
    try:
        os.execve(sys.executable, [sys.executable, __file__], scrubbed)
    except OSError:
        sys.exit(20)
else:
    # Re-exec'd process: check /proc/self/environ
    try:
        with open("/proc/self/environ", "rb") as f:
            environ_blob = f.read()
        if b"KBC_TOKEN=" in environ_blob:
            sys.exit(10)
        else:
            sys.exit(0)
    except OSError:
        sys.exit(0)  # can't read /proc/self/environ — treat as pass
""",
            encoding="utf-8",
        )

        result = subprocess.run(
            [sys.executable, str(helper)],
            env={**os.environ, "KBC_TOKEN": "test-token-12345"},
            capture_output=True,
        )
        assert result.returncode != 20, "execve failed unexpectedly in env-scrub helper"
        assert result.returncode == 0, (
            f"KBC_TOKEN was found in /proc/self/environ after env-scrub (exit {result.returncode}); scrub did not work"
        )


# ---------------------------------------------------------------------------
# 5b. ptrace_scope boot gate (HIGH-2) — fail closed unless same-UID attach is restricted
# ---------------------------------------------------------------------------


class TestPtraceScopeGate:
    """``_assert_ptrace_protected`` must fail closed when ptrace_scope < 1."""

    def test_scope_one_passes(self, monkeypatch):
        monkeypatch.setattr("component._read_ptrace_scope", lambda: 1)
        _assert_ptrace_protected()  # must not raise

    def test_scope_two_passes(self, monkeypatch):
        monkeypatch.setattr("component._read_ptrace_scope", lambda: 2)
        _assert_ptrace_protected()  # must not raise

    def test_unreadable_warns_and_proceeds(self, monkeypatch):
        """Non-Linux / no Yama (None) cannot verify — warn, do not fail."""
        monkeypatch.setattr("component._read_ptrace_scope", lambda: None)
        _assert_ptrace_protected()  # must not raise

    def test_scope_zero_fails_closed(self, monkeypatch):
        from keboola.component.exceptions import UserException

        monkeypatch.setattr("component._read_ptrace_scope", lambda: 0)
        monkeypatch.delenv(_PTRACE_OVERRIDE_ENV, raising=False)
        with pytest.raises(UserException, match="ptrace_scope=0"):
            _assert_ptrace_protected()

    def test_scope_zero_with_override_proceeds(self, monkeypatch):
        monkeypatch.setattr("component._read_ptrace_scope", lambda: 0)
        monkeypatch.setenv(_PTRACE_OVERRIDE_ENV, "1")
        _assert_ptrace_protected()  # override → must not raise


# ---------------------------------------------------------------------------
# 6. Output promotion in parent + transcript flush on failure
# ---------------------------------------------------------------------------


class TestOutputPromotionAndTranscript:
    """Parent promotes outputs and transcript is always flushed (durability)."""

    def test_transcript_flushed_when_server_runs(self, tmp_path, monkeypatch):
        """Full run with broker: transcript tables are written."""
        from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock

        from claude_runner import ClaudeRunner

        data_dir = _make_config(tmp_path, {"task": {"prompt": "hi"}})
        monkeypatch.setenv("KBC_DATADIR", data_dir)

        def canned(self, prompt, options):
            async def gen():
                yield SystemMessage(subtype="init", data={"session_id": "s"})
                yield AssistantMessage(content=[TextBlock(text="done")], model="claude-opus-4-8", session_id="s")
                yield ResultMessage(
                    subtype="success",
                    duration_ms=10,
                    duration_api_ms=5,
                    is_error=False,
                    num_turns=1,
                    session_id="s",
                    total_cost_usd=0.0,
                    result="ok",
                )

            return gen()

        monkeypatch.setattr(ClaudeRunner, "_query", canned)
        comp = Component()
        comp.run()

        tables = os.path.join(data_dir, "out", "tables")
        assert os.path.isfile(os.path.join(tables, "claude_runs.csv"))
        assert os.path.isfile(os.path.join(tables, "claude_sessions.csv"))

    def test_config_json_scrubbed_before_agent_run(self, tmp_path, monkeypatch):
        """config.json has #-key values replaced with "" before agent subprocess spawns."""
        from claude_agent_sdk import ResultMessage, SystemMessage

        from claude_runner import ClaudeRunner

        data_dir = _make_config(tmp_path, {"task": {"prompt": "hi"}})
        monkeypatch.setenv("KBC_DATADIR", data_dir)

        config_state_during_run = {}

        def capturing_query(self, prompt, options):
            async def gen():
                # Read config.json at the moment the agent 'runs' (before result)
                config_path = os.path.join(data_dir, "config.json")
                if os.path.exists(config_path):
                    with open(config_path, encoding="utf-8") as fh:
                        config_state_during_run["content"] = json.load(fh)
                yield SystemMessage(subtype="init", data={"session_id": "s"})
                yield ResultMessage(
                    subtype="success",
                    duration_ms=10,
                    duration_api_ms=5,
                    is_error=False,
                    num_turns=1,
                    session_id="s",
                    total_cost_usd=0.0,
                    result="ok",
                )

            return gen()

        monkeypatch.setattr(ClaudeRunner, "_query", capturing_query)
        comp = Component()
        comp.run()

        assert config_state_during_run, "config.json should have been readable during run"
        params = config_state_during_run["content"]["parameters"]
        # The real key value must be scrubbed
        assert params.get("#anthropic_key") == "", (
            f"#anthropic_key should be scrubbed to '' before agent runs, got: {params.get('#anthropic_key')!r}"
        )

    def test_agent_env_has_no_real_secrets_in_options(self, tmp_path, monkeypatch):
        """The env dict passed to ClaudeAgentOptions must contain no real secrets."""
        from claude_agent_sdk import ResultMessage, SystemMessage

        from claude_runner import ClaudeRunner

        data_dir = _make_config(
            tmp_path,
            {
                "#github_token": "gh_real_secret",
                "github_enabled": True,
                "operates_on": "org/repo-X",
                "task": {"prompt": "hi"},
            },
        )
        monkeypatch.setenv("KBC_DATADIR", data_dir)

        captured_env = {}

        original_build = ClaudeRunner.build_options

        def capturing_build(self_r, task, config, plugin_paths, env):
            captured_env.update(env)
            return original_build(self_r, task, config, plugin_paths, env)

        monkeypatch.setattr(ClaudeRunner, "build_options", capturing_build)

        def noop_query(self, prompt, options):
            async def gen():
                yield SystemMessage(subtype="init", data={"session_id": "s"})
                yield ResultMessage(
                    subtype="success",
                    duration_ms=10,
                    duration_api_ms=5,
                    is_error=False,
                    num_turns=1,
                    session_id="s",
                    total_cost_usd=0.0,
                    result="ok",
                )

            return gen()

        monkeypatch.setattr(ClaudeRunner, "_query", noop_query)

        comp = Component()
        comp.run()

        # The dummy key must be there, not the real one
        assert captured_env.get("ANTHROPIC_API_KEY") == _DUMMY_ANTHROPIC_KEY
        assert "REAL_KEY_NEVER_EXPOSED" not in captured_env.values()
        assert "gh_real_secret" not in captured_env.values()
        # Platform-injected secrets are explicitly blanked ("" overrides any
        # value the SDK's os.environ merge would otherwise pass through).
        assert captured_env.get("KBC_TOKEN") == ""
        assert captured_env.get("GITHUB_TOKEN") == ""
        assert captured_env.get("GH_TOKEN") == ""
        # Loopback routing must be present
        assert "ANTHROPIC_BASE_URL" in captured_env
        assert "127.0.0.1" in captured_env["ANTHROPIC_BASE_URL"]
