"""Unit tests for PluginManager (claude CLI subprocess mocked)."""

import subprocess

import pytest
from keboola.component.exceptions import UserException

from configuration import PluginEntry
from plugin_manager import PluginManager


class FakeRunner:
    """Records claude CLI invocations and returns canned responses."""

    def __init__(self, list_json="[]"):
        self.calls: list[list[str]] = []
        self._list_json = list_json

    def __call__(self, cmd, capture_output, text, env):
        # cmd is ["claude", ...args]
        args = cmd[1:]
        self.calls.append(args)
        stdout = self._list_json if args[:3] == ["plugin", "marketplace", "list"] else "ok"
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    def arg_strings(self):
        return [" ".join(a) for a in self.calls]


def test_no_plugins_is_noop_but_sets_env(monkeypatch):
    env: dict[str, str] = {}
    result = PluginManager().prepare([], env)
    assert result.sdk_plugins == []
    assert result.resolved == {}
    assert env["CLAUDE_CONFIG_DIR"] == "/tmp/claude-home"
    assert env["CLAUDE_CODE_PLUGIN_KEEP_MARKETPLACE_ON_FAILURE"] == "1"


def test_latest_entry_adds_then_updates(monkeypatch):
    runner = FakeRunner()
    monkeypatch.setattr(subprocess, "run", runner)
    monkeypatch.setattr("os.makedirs", lambda *a, **k: None)

    entry = PluginEntry(source="superpowers", plugins=["sp"], version="latest")
    result = PluginManager().prepare([entry], {})
    strings = runner.arg_strings()
    # superpowers shorthand resolved to obra/superpowers
    assert "plugin marketplace add obra/superpowers" in strings
    assert "plugin marketplace update superpowers" in strings
    assert "plugin install sp@superpowers" in strings
    assert result.resolved == {"superpowers/sp": "latest"}


def test_pinned_entry_adds_with_ref_no_update(monkeypatch):
    runner = FakeRunner()
    monkeypatch.setattr(subprocess, "run", runner)
    monkeypatch.setattr("os.makedirs", lambda *a, **k: None)

    entry = PluginEntry(source="acme/marketplace", plugins=["tool"], version="v1.2.0")
    PluginManager().prepare([entry], {})
    strings = runner.arg_strings()
    assert "plugin marketplace add acme/marketplace@v1.2.0" in strings
    assert not any("marketplace update" in s for s in strings)
    assert "plugin install tool@marketplace" in strings


def test_pinned_git_url_uses_hash_ref(monkeypatch):
    runner = FakeRunner()
    monkeypatch.setattr(subprocess, "run", runner)
    monkeypatch.setattr("os.makedirs", lambda *a, **k: None)

    entry = PluginEntry(
        source="https://github.com/acme/repo.git", plugins=["t"], version="abc123"
    )
    PluginManager().prepare([entry], {})
    strings = runner.arg_strings()
    assert "plugin marketplace add https://github.com/acme/repo.git#abc123" in strings


def test_private_without_token_raises():
    entry = PluginEntry(source="keboola/private-kit", private=True, plugins=["x"])
    with pytest.raises(UserException) as exc:
        PluginManager().prepare([entry], {}, github_token="")
    assert "github_token" in str(exc.value)


def test_private_with_token_ok(monkeypatch):
    runner = FakeRunner()
    monkeypatch.setattr(subprocess, "run", runner)
    monkeypatch.setattr("os.makedirs", lambda *a, **k: None)

    entry = PluginEntry(source="keboola/private-kit", private=True, plugins=["x"], version="v1.0")
    result = PluginManager().prepare([entry], {}, github_token="GH_VALUE")
    assert result.resolved == {"private-kit/x": "v1.0"}


def test_unknown_shorthand_raises(monkeypatch):
    monkeypatch.setattr("os.makedirs", lambda *a, **k: None)
    entry = PluginEntry(source="notreal", plugins=["x"])
    with pytest.raises(UserException) as exc:
        PluginManager().prepare([entry], {})
    assert "notreal" in str(exc.value)


def test_failed_command_raises_with_source(monkeypatch):
    def fail_run(cmd, capture_output, text, env):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", fail_run)
    monkeypatch.setattr("os.makedirs", lambda *a, **k: None)
    entry = PluginEntry(source="acme/repo", plugins=["x"], version="latest")
    with pytest.raises(UserException) as exc:
        PluginManager().prepare([entry], {})
    assert "acme/repo" in str(exc.value)


def test_cache_path_from_list_json(monkeypatch):
    runner = FakeRunner(list_json='[{"name": "superpowers", "path": "/tmp/claude-home/plugins/cache/superpowers"}]')
    monkeypatch.setattr(subprocess, "run", runner)
    monkeypatch.setattr("os.makedirs", lambda *a, **k: None)

    entry = PluginEntry(source="superpowers", plugins=["sp"], version="latest")
    result = PluginManager().prepare([entry], {})
    assert {"type": "local", "path": "/tmp/claude-home/plugins/cache/superpowers"} in result.sdk_plugins


def test_token_scrubbed_from_logs(monkeypatch, caplog):
    def run_with_token_in_err(cmd, capture_output, text, env):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="auth failed for GH_SECRET_VALUE")

    monkeypatch.setattr(subprocess, "run", run_with_token_in_err)
    monkeypatch.setattr("os.makedirs", lambda *a, **k: None)
    entry = PluginEntry(source="keboola/private", private=True, plugins=["x"], version="latest")
    with pytest.raises(UserException) as exc:
        PluginManager().prepare([entry], {}, github_token="GH_SECRET_VALUE")
    assert "GH_SECRET_VALUE" not in str(exc.value)
    assert "***" in str(exc.value)


def test_full_secret_set_scrubbed_not_just_github_token(monkeypatch):
    """The CLI env also carries ANTHROPIC_API_KEY / MCP secrets; the scrub must
    redact the FULL secret set, not only the github_token."""

    def run_leaking_anthropic_key(cmd, capture_output, text, env):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom leaked ANTHROPIC_VALUE and MCP_VALUE")

    monkeypatch.setattr(subprocess, "run", run_leaking_anthropic_key)
    monkeypatch.setattr("os.makedirs", lambda *a, **k: None)
    entry = PluginEntry(source="acme/repo", plugins=["x"], version="latest")
    with pytest.raises(UserException) as exc:
        PluginManager().prepare(
            [entry], {}, github_token="GH_VALUE", secret_values=["ANTHROPIC_VALUE", "MCP_VALUE", "GH_VALUE"]
        )
    msg = str(exc.value)
    assert "ANTHROPIC_VALUE" not in msg
    assert "MCP_VALUE" not in msg
    assert "***" in msg
