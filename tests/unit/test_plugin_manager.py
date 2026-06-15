"""Unit tests for PluginManager (claude CLI subprocess mocked)."""

import subprocess

import pytest
from keboola.component.exceptions import UserException

import plugin_manager
from configuration import PluginEntry
from plugin_manager import PluginManager, _resolve_claude_cli


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
    assert env["CLAUDE_CODE_PLUGIN_CACHE_DIR"] == "/tmp/claude-home/plugins/cache"
    # Finding 4: this flag breaks the marketplace clone/validate — must NOT be set.
    assert "CLAUDE_CODE_PLUGIN_KEEP_MARKETPLACE_ON_FAILURE" not in env


def test_latest_entry_adds_then_updates(monkeypatch):
    runner = FakeRunner()
    monkeypatch.setattr(subprocess, "run", runner)
    monkeypatch.setattr("os.makedirs", lambda *a, **k: None)

    entry = PluginEntry(source="superpowers", plugins=["sp"], version="latest")
    result = PluginManager().prepare([entry], {})
    strings = runner.arg_strings()
    # superpowers shorthand resolved to obra/superpowers
    assert "plugin marketplace add obra/superpowers" in strings
    # list --json empty -> falls back to the source-derived name "superpowers"
    assert "plugin marketplace update superpowers" in strings
    assert "plugin install sp@superpowers" in strings
    assert result.resolved == {"superpowers/sp": "latest"}


def test_uses_declared_marketplace_name_not_source_derived(monkeypatch):
    """Finding 4: the CLI registers a marketplace under the name DECLARED in its
    marketplace.json (e.g. obra/superpowers -> 'superpowers-dev'), not a name
    derived from the source. update/install/cache-path must use the discovered
    declared name + installLocation, or they silently fail on-platform."""
    declared = (
        '[{"name": "superpowers-dev", "repo": "obra/superpowers", '
        '"installLocation": "/tmp/claude-home/plugins/cache/marketplaces/superpowers-dev"}]'
    )
    runner = FakeRunner(list_json=declared)
    monkeypatch.setattr(subprocess, "run", runner)
    monkeypatch.setattr("os.makedirs", lambda *a, **k: None)

    entry = PluginEntry(source="superpowers", plugins=["sp"], version="latest")
    result = PluginManager().prepare([entry], {})
    strings = runner.arg_strings()
    # discovery picks the DECLARED name, not the source-derived "superpowers"
    assert "plugin marketplace update superpowers-dev" in strings
    assert "plugin install sp@superpowers-dev" in strings
    assert result.resolved == {"superpowers-dev/sp": "latest"}
    # the SDK local-plugin path is the discovered installLocation
    assert {
        "type": "local",
        "path": "/tmp/claude-home/plugins/cache/marketplaces/superpowers-dev",
    } in result.sdk_plugins


def test_wildcard_installs_declared_plugin_names_not_marketplace_name(tmp_path, monkeypatch):
    """Finding 8: plugins ['*'] (install all) must enumerate the marketplace's
    DECLARED plugin names from its marketplace.json and install each — never the
    marketplace name. The marketplace is NAMED 'superpowers-dev' but contains a
    plugin NAMED 'superpowers'; installing 'superpowers-dev' fails on-platform."""
    import json as _json

    install_loc = tmp_path / "marketplaces" / "superpowers-dev"
    (install_loc / ".claude-plugin").mkdir(parents=True)
    (install_loc / ".claude-plugin" / "marketplace.json").write_text(
        _json.dumps({"name": "superpowers-dev", "plugins": [{"name": "superpowers"}]}),
        encoding="utf-8",
    )
    declared = _json.dumps(
        [{"name": "superpowers-dev", "repo": "obra/superpowers", "installLocation": str(install_loc)}]
    )
    runner = FakeRunner(list_json=declared)
    monkeypatch.setattr(subprocess, "run", runner)
    monkeypatch.setattr("os.makedirs", lambda *a, **k: None)

    entry = PluginEntry(source="superpowers", plugins=["*"], version="latest")
    result = PluginManager().prepare([entry], {})
    strings = runner.arg_strings()
    # the DECLARED plugin name is installed, NOT the marketplace name or "*"
    assert "plugin install superpowers@superpowers-dev" in strings
    assert not any("install superpowers-dev@" in s for s in strings)
    assert not any(s.endswith("install *") or "install *@" in s for s in strings)
    assert result.resolved == {"superpowers-dev/superpowers": "latest"}


def test_empty_plugins_enumerates_declared_names(tmp_path, monkeypatch):
    """An empty plugins list behaves like ['*'] — enumerate declared names."""
    import json as _json

    install_loc = tmp_path / "marketplaces" / "kit"
    (install_loc / ".claude-plugin").mkdir(parents=True)
    (install_loc / ".claude-plugin" / "marketplace.json").write_text(
        _json.dumps({"name": "kit", "plugins": [{"name": "alpha"}, {"name": "beta"}]}),
        encoding="utf-8",
    )
    declared = _json.dumps([{"name": "kit", "repo": "acme/kit", "installLocation": str(install_loc)}])
    runner = FakeRunner(list_json=declared)
    monkeypatch.setattr(subprocess, "run", runner)
    monkeypatch.setattr("os.makedirs", lambda *a, **k: None)

    entry = PluginEntry(source="acme/kit", plugins=[], version="latest")
    result = PluginManager().prepare([entry], {})
    strings = runner.arg_strings()
    assert "plugin install alpha@kit" in strings
    assert "plugin install beta@kit" in strings
    assert result.resolved == {"kit/alpha": "latest", "kit/beta": "latest"}


def test_wildcard_with_no_declarable_plugins_raises(tmp_path, monkeypatch):
    """If '*' is given but no declared plugins can be enumerated, fail clearly
    rather than installing the (invalid) marketplace name."""
    # installLocation has no readable marketplace.json -> no declared names
    declared = '[{"name": "kit", "repo": "acme/kit", "installLocation": "/nonexistent/kit"}]'
    runner = FakeRunner(list_json=declared)
    monkeypatch.setattr(subprocess, "run", runner)
    monkeypatch.setattr("os.makedirs", lambda *a, **k: None)

    entry = PluginEntry(source="acme/kit", plugins=["*"], version="latest")
    with pytest.raises(UserException) as exc:
        PluginManager().prepare([entry], {})
    assert "no installable plugins" in str(exc.value) or "explicitly" in str(exc.value)


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


def test_cache_path_falls_back_to_convention_when_no_install_location(monkeypatch):
    """When list --json yields no installLocation (and no match), the cache path
    falls back to the conventional <cache>/<marketplace> location."""
    runner = FakeRunner()  # empty list -> fallback name "superpowers", no installLocation
    monkeypatch.setattr(subprocess, "run", runner)
    monkeypatch.setattr("os.makedirs", lambda *a, **k: None)

    entry = PluginEntry(source="superpowers", plugins=["sp"], version="latest")
    result = PluginManager().prepare([entry], {})
    assert {
        "type": "local",
        "path": "/tmp/claude-home/plugins/cache/superpowers",
    } in result.sdk_plugins


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


def test_resolves_bundled_cli_absolute_path_not_bare_claude():
    """The CLI must resolve to the SDK's bundled absolute path, never bare 'claude'
    (which is not on PATH in the slim image and would raise FileNotFoundError)."""
    _resolve_claude_cli.cache_clear()
    cli = _resolve_claude_cli()
    assert cli != "claude"
    assert cli.endswith("/_bundled/claude") or cli.endswith("\\_bundled\\claude.exe")
    assert "claude_agent_sdk" in cli


def test_run_invokes_resolved_cli_not_bare_claude(monkeypatch):
    """The subprocess command's argv[0] must be the resolved bundled path."""
    _resolve_claude_cli.cache_clear()
    runner = FakeRunner()
    monkeypatch.setattr(subprocess, "run", runner)
    monkeypatch.setattr("os.makedirs", lambda *a, **k: None)

    entry = PluginEntry(source="superpowers", plugins=["sp"], version="latest")
    PluginManager().prepare([entry], {})

    # FakeRunner records args (cmd[1:]); fetch the captured argv[0] via the run call.
    # Re-run a single command to capture cmd[0] directly.
    captured = {}

    def capture(cmd, capture_output, text, env):
        captured["argv0"] = cmd[0]
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", capture)
    PluginManager().prepare([entry], {})
    assert captured["argv0"] == _resolve_claude_cli()
    assert captured["argv0"] != "claude"


def test_cli_launch_failure_raises_user_exception(monkeypatch):
    """A failed launch (FileNotFoundError) must become a clean UserException (exit 1),
    not an unhandled OSError (exit 2)."""
    _resolve_claude_cli.cache_clear()
    monkeypatch.setattr(plugin_manager, "_resolve_claude_cli", lambda: "/nonexistent/claude")
    monkeypatch.setattr("os.makedirs", lambda *a, **k: None)

    def raise_fnf(cmd, capture_output, text, env):
        raise FileNotFoundError(2, "No such file or directory", cmd[0])

    monkeypatch.setattr(subprocess, "run", raise_fnf)
    entry = PluginEntry(source="acme/repo", plugins=["x"], version="latest")
    with pytest.raises(UserException) as exc:
        PluginManager().prepare([entry], {})
    assert "Claude CLI failed to launch" in str(exc.value)
    assert "acme/repo" in str(exc.value)


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
