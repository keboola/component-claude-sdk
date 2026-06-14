"""Unit tests for ClaudeRunner — options mapping and the _query seam."""

import asyncio

from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock

from claude_runner import ClaudeRunner, ClaudeRunResult
from configuration import Configuration
from tasks import Task


def _config(**overrides):
    data = {"#anthropic_key": "KEY_NAME_ONLY"}
    data.update(overrides)
    return Configuration(**data)


def _task(**overrides):
    base = {"task_id": "t1", "prompt": "hello"}
    base.update(overrides)
    return Task(**base)


def test_build_options_basic_mapping():
    cfg = _config(max_turns=7, max_budget_usd=8.0)
    runner = ClaudeRunner(workspace_dir="/tmp/ws")
    opts = runner.build_options(_task(), cfg, [], {"ANTHROPIC_API_KEY": "x"})
    assert opts.model == "claude-opus-4-8"
    assert opts.max_turns == 7
    assert opts.max_budget_usd == 8.0
    assert opts.permission_mode == "dontAsk"
    assert opts.cwd == "/tmp/ws"
    assert opts.env == {"ANTHROPIC_API_KEY": "x"}


def test_build_options_budget_clamped_to_config_ceiling():
    cfg = _config(max_budget_usd=10.0)
    runner = ClaudeRunner(workspace_dir="/tmp/ws")
    opts = runner.build_options(_task(max_budget_usd=50.0), cfg, [], {})
    assert opts.max_budget_usd == 10.0


def test_build_options_per_task_overrides():
    cfg = _config()
    runner = ClaudeRunner(workspace_dir="/tmp/ws")
    opts = runner.build_options(
        _task(model="claude-haiku-4-5", max_turns=3, system_prompt="be brief"), cfg, [], {}
    )
    assert opts.model == "claude-haiku-4-5"
    assert opts.max_turns == 3
    assert opts.system_prompt == "be brief"


def test_build_options_github_tools_added():
    cfg = _config(github_enabled=True)
    runner = ClaudeRunner(workspace_dir="/tmp/ws")
    opts = runner.build_options(_task(), cfg, [], {})
    assert "Bash" in opts.allowed_tools
    assert "Bash(gh *)" in opts.allowed_tools
    assert "Bash(git *)" in opts.allowed_tools


def test_build_options_mcp_stdio_and_http():
    cfg = _config(
        mcp_servers=[
            {"type": "stdio", "name": "kbc", "command": "uvx", "args": ["keboola-mcp-server"], "env": {"T": "v"}},
            {"type": "http", "name": "remote", "url": "https://x/mcp", "headers": {"Authorization": "Bearer v"}},
        ]
    )
    runner = ClaudeRunner(workspace_dir="/tmp/ws")
    opts = runner.build_options(_task(), cfg, [], {})
    assert opts.mcp_servers["kbc"]["command"] == "uvx"
    assert opts.mcp_servers["kbc"]["env"] == {"T": "v"}
    assert opts.mcp_servers["remote"]["url"] == "https://x/mcp"
    assert opts.mcp_servers["remote"]["headers"] == {"Authorization": "Bearer v"}


def test_build_options_plugins_passed_through():
    cfg = _config()
    runner = ClaudeRunner(workspace_dir="/tmp/ws")
    plugins = [{"type": "local", "path": "/tmp/claude-home/plugins/cache/sp"}]
    opts = runner.build_options(_task(), cfg, plugins, {})
    assert opts.plugins == plugins


def test_settings_json_object_written_to_file_and_path_passed(tmp_path):
    import json

    home = str(tmp_path / "home")
    cfg = _config(settings_json={"permissions": {"allow": ["Bash"]}})
    runner = ClaudeRunner(workspace_dir="/tmp/ws")
    opts = runner.build_options(_task(), cfg, [], {"CLAUDE_CONFIG_DIR": home})
    assert opts.settings == f"{home}/settings.json"
    written = json.load(open(opts.settings, encoding="utf-8"))
    assert written == {"permissions": {"allow": ["Bash"]}}


def test_settings_json_string_written_verbatim(tmp_path):
    home = str(tmp_path / "home")
    cfg = _config(settings_json='{"raw": true}')
    runner = ClaudeRunner(workspace_dir="/tmp/ws")
    opts = runner.build_options(_task(), cfg, [], {"CLAUDE_CONFIG_DIR": home})
    assert open(opts.settings, encoding="utf-8").read() == '{"raw": true}'


def test_settings_json_absent_no_settings_option(tmp_path):
    cfg = _config()  # no settings_json
    runner = ClaudeRunner(workspace_dir="/tmp/ws")
    opts = runner.build_options(_task(), cfg, [], {"CLAUDE_CONFIG_DIR": str(tmp_path)})
    assert opts.settings is None


def _make_stream(result_message):
    async def gen(prompt, options):
        yield SystemMessage(subtype="init", data={"session_id": "sess-1"})
        yield AssistantMessage(content=[TextBlock(text="working")], model="claude-opus-4-8")
        yield result_message

    return gen


def _run(runner, task, on_message):
    return asyncio.run(runner.run_task(task, options=None, on_message=on_message))


def test_run_task_happy_path(monkeypatch):
    runner = ClaudeRunner(workspace_dir="/tmp/ws")
    result_msg = ResultMessage(
        subtype="success",
        duration_ms=1200,
        duration_api_ms=900,
        is_error=False,
        num_turns=2,
        session_id="sess-1",
        total_cost_usd=0.01,
        result="done",
    )
    monkeypatch.setattr(runner, "_query", _make_stream(result_msg))

    seen = []
    res: ClaudeRunResult = _run(runner, _task(), lambda m: seen.append(m))
    assert res.success is True
    assert res.session_id == "sess-1"
    assert res.result_text == "done"
    assert res.total_cost_usd == 0.01
    assert len(seen) == 3  # system + assistant + result teed


def test_run_task_budget_cap_marks_failure(monkeypatch):
    runner = ClaudeRunner(workspace_dir="/tmp/ws")
    result_msg = ResultMessage(
        subtype="error_max_budget_usd",
        duration_ms=500,
        duration_api_ms=400,
        is_error=True,
        num_turns=4,
        session_id="sess-2",
    )
    monkeypatch.setattr(runner, "_query", _make_stream(result_msg))
    res = _run(runner, _task(), lambda m: None)
    assert res.success is False
    assert "cap" in (res.error_message or "")


def test_run_task_no_result_message(monkeypatch):
    runner = ClaudeRunner(workspace_dir="/tmp/ws")

    async def empty(prompt, options):
        if False:
            yield None

    monkeypatch.setattr(runner, "_query", empty)
    res = _run(runner, _task(), lambda m: None)
    assert res.success is False
    assert "No result message" in (res.error_message or "")
