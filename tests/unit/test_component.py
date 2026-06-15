"""End-to-end orchestrator test with the SDK boundary mocked (no network)."""

import csv
import json
import os

from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock

from component import Component


def _make_datadir(tmp_path, parameters):
    """Build a minimal KBC data dir with a config.json."""
    data_dir = tmp_path / "data"
    (data_dir / "in" / "tables").mkdir(parents=True)
    (data_dir / "in" / "files").mkdir(parents=True)
    (data_dir / "out" / "tables").mkdir(parents=True)
    (data_dir / "out" / "files").mkdir(parents=True)
    (data_dir / "config.json").write_text(json.dumps({"parameters": parameters}), encoding="utf-8")
    return str(data_dir)


def _canned_stream(result_subtype="success", is_error=False):
    async def gen(prompt, options):
        yield SystemMessage(subtype="init", data={"session_id": "sess-1"})
        yield AssistantMessage(content=[TextBlock(text="done")], model="claude-opus-4-8", session_id="sess-1")
        yield ResultMessage(
            subtype=result_subtype,
            duration_ms=100,
            duration_api_ms=80,
            is_error=is_error,
            num_turns=1,
            session_id="sess-1",
            total_cost_usd=0.001,
            result="ok",
        )

    return gen


def _patch_sdk(monkeypatch, comp, stream):
    # Mock the SDK seam so no subprocess/network is touched.
    monkeypatch.setattr(comp._runner, "_query", stream)
    # build_options imports ClaudeAgentOptions lazily; let it run for real but
    # avoid touching the actual SDK runtime — the canned stream ignores options.


def test_config_prompt_run_writes_transcript_tables(tmp_path, monkeypatch):
    data_dir = _make_datadir(
        tmp_path,
        {"#anthropic_key": "KEY_NAME_ONLY", "task": {"prompt": "summarise"}},
    )
    monkeypatch.setenv("KBC_DATADIR", data_dir)
    comp = Component()
    _patch_sdk(monkeypatch, comp, _canned_stream())

    comp.run()

    tables = os.path.join(data_dir, "out", "tables")
    runs = os.path.join(tables, "claude_runs.csv")
    sessions = os.path.join(tables, "claude_sessions.csv")
    assert os.path.isfile(runs)
    assert os.path.isfile(sessions)
    rows = list(csv.DictReader(open(runs, encoding="utf-8")))
    assert rows[0]["success"] == "true"
    assert rows[0]["task_id"] == "config-task"
    # the JSONL file artifact is present
    files = os.listdir(os.path.join(data_dir, "out", "files"))
    assert any(f.endswith(".jsonl") for f in files)


def test_agent_output_table_promoted(tmp_path, monkeypatch):
    data_dir = _make_datadir(
        tmp_path,
        {"#anthropic_key": "KEY_NAME_ONLY", "task": {"prompt": "make a table"}},
    )
    monkeypatch.setenv("KBC_DATADIR", data_dir)
    comp = Component()

    # The agent "writes" a CSV to /tmp/outputs during the run; emulate that by
    # creating it through the output writer's dir before finalize runs.
    def stream_then_write(prompt, options):
        async def gen():
            yield SystemMessage(subtype="init", data={"session_id": "s"})
            # simulate the agent producing an output file mid-run
            out_dir = comp._output_writer.agent_output_dir
            os.makedirs(out_dir, exist_ok=True)
            with open(os.path.join(out_dir, "result.csv"), "w", encoding="utf-8", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["id", "label"])
                w.writerow(["1", "x"])
            yield ResultMessage(
                subtype="success", duration_ms=10, duration_api_ms=5, is_error=False,
                num_turns=1, session_id="s", total_cost_usd=0.0, result="ok",
            )

        return gen()

    monkeypatch.setattr(comp._runner, "_query", stream_then_write)
    comp.run()

    promoted = os.path.join(data_dir, "out", "tables", "result.csv")
    assert os.path.isfile(promoted)
    rows = list(csv.DictReader(open(promoted, encoding="utf-8")))
    assert rows == [{"id": "1", "label": "x"}]


def test_failed_task_raises_user_exception(tmp_path, monkeypatch):
    from keboola.component.exceptions import UserException

    data_dir = _make_datadir(
        tmp_path,
        {"#anthropic_key": "KEY_NAME_ONLY", "task": {"prompt": "x"}},
    )
    monkeypatch.setenv("KBC_DATADIR", data_dir)
    comp = Component()
    _patch_sdk(monkeypatch, comp, _canned_stream(result_subtype="error", is_error=True))

    import pytest

    with pytest.raises(UserException):
        comp.run()
    # transcript still written (write_always) despite the failure
    assert os.path.isfile(os.path.join(data_dir, "out", "tables", "claude_runs.csv"))


def test_workspace_input_files_staged_into_workspace(tmp_path, monkeypatch):
    import component as component_module

    data_dir = _make_datadir(
        tmp_path,
        {"#anthropic_key": "KEY_NAME_ONLY", "workspace_input_files": True, "task": {"prompt": "read it"}},
    )
    # an uploaded input file (+ a manifest that must NOT be staged)
    in_files = os.path.join(data_dir, "in", "files")
    with open(os.path.join(in_files, "doc.txt"), "w", encoding="utf-8") as fh:
        fh.write("hello")
    with open(os.path.join(in_files, "doc.txt.manifest"), "w", encoding="utf-8") as fh:
        fh.write("{}")

    workspace = str(tmp_path / "ws")
    monkeypatch.setattr(component_module, "WORKSPACE_DIR", workspace)
    monkeypatch.setenv("KBC_DATADIR", data_dir)
    comp = Component()
    comp._runner._workspace_dir = workspace  # keep runner cwd aligned with the patched workspace
    _patch_sdk(monkeypatch, comp, _canned_stream())

    comp.run()

    assert os.path.isfile(os.path.join(workspace, "doc.txt"))
    assert open(os.path.join(workspace, "doc.txt"), encoding="utf-8").read() == "hello"
    # the manifest sidecar is not staged
    assert not os.path.exists(os.path.join(workspace, "doc.txt.manifest"))


def test_workspace_input_files_off_does_not_stage(tmp_path, monkeypatch):
    import component as component_module

    data_dir = _make_datadir(
        tmp_path,
        {"#anthropic_key": "KEY_NAME_ONLY", "task": {"prompt": "x"}},  # toggle off (default)
    )
    with open(os.path.join(data_dir, "in", "files", "doc.txt"), "w", encoding="utf-8") as fh:
        fh.write("hello")

    workspace = str(tmp_path / "ws2")
    monkeypatch.setattr(component_module, "WORKSPACE_DIR", workspace)
    monkeypatch.setenv("KBC_DATADIR", data_dir)
    comp = Component()
    comp._runner._workspace_dir = workspace
    _patch_sdk(monkeypatch, comp, _canned_stream())

    comp.run()
    assert not os.path.exists(os.path.join(workspace, "doc.txt"))


def test_transcript_flushed_when_promote_raises(tmp_path, monkeypatch):
    """If promote() raises (e.g. incremental-without-PK) the write_always
    claude_sessions/claude_runs tables MUST still be written (flush in finally)."""
    import pytest
    from keboola.component.exceptions import UserException

    data_dir = _make_datadir(
        tmp_path,
        {"#anthropic_key": "KEY_NAME_ONLY", "task": {"prompt": "x"}},
    )
    monkeypatch.setenv("KBC_DATADIR", data_dir)
    comp = Component()
    _patch_sdk(monkeypatch, comp, _canned_stream())
    # force promote() to raise
    monkeypatch.setattr(comp._output_writer, "promote", lambda **kw: (_ for _ in ()).throw(UserException("boom")))

    with pytest.raises(UserException) as exc:
        comp.run()
    assert "boom" in str(exc.value)
    # durable transcript written despite the promote failure
    assert os.path.isfile(os.path.join(data_dir, "out", "tables", "claude_sessions.csv"))
    assert os.path.isfile(os.path.join(data_dir, "out", "tables", "claude_runs.csv"))


def test_transcript_flushed_when_run_task_raises(tmp_path, monkeypatch):
    """If the SDK query loop raises, the buffered transcript rows from before the
    raise must still be flushed (finally), and it surfaces as exit-1 UserException."""
    import pytest
    from keboola.component.exceptions import UserException

    data_dir = _make_datadir(
        tmp_path,
        {"#anthropic_key": "KEY_NAME_ONLY", "task": {"prompt": "x"}},
    )
    monkeypatch.setenv("KBC_DATADIR", data_dir)
    comp = Component()

    from claude_agent_sdk import ProcessError, SystemMessage

    def raising_stream(prompt, options):
        async def gen():
            # one message is teed (buffered) before the loop raises
            yield SystemMessage(subtype="init", data={"session_id": "s"})
            raise ProcessError("CLI exited with 401 invalid api key", exit_code=1)

        return gen()

    monkeypatch.setattr(comp._runner, "_query", raising_stream)

    with pytest.raises(UserException) as exc:
        comp.run()
    # auth-classified message (exit 1, not opaque exit 2)
    assert "#anthropic_key" in str(exc.value)
    # the buffered session row was still flushed
    assert os.path.isfile(os.path.join(data_dir, "out", "tables", "claude_sessions.csv"))


def test_memory_warning_fires_for_mcp_servers(caplog):
    """Finding 6: configuring MCP servers must log a non-blocking memory WARNING
    (the default 256 MB backend OOMs subprocess-spawning workloads)."""
    import logging

    from configuration import Configuration

    cfg = Configuration(
        **{
            "#anthropic_key": "KEY_NAME_ONLY",
            "mcp_servers": [{"type": "stdio", "name": "fetch", "command": "uvx", "args": ["mcp-server-fetch"]}],
        }
    )
    with caplog.at_level(logging.WARNING):
        Component._warn_if_memory_intensive(cfg)
    assert any("runtime.backend.type" in r.message for r in caplog.records)


def test_memory_warning_fires_for_plugins(caplog):
    import logging

    from configuration import Configuration

    cfg = Configuration(**{"#anthropic_key": "KEY_NAME_ONLY", "plugins": [{"source": "superpowers"}]})
    with caplog.at_level(logging.WARNING):
        Component._warn_if_memory_intensive(cfg)
    assert any("runtime.backend.type" in r.message for r in caplog.records)


def test_memory_warning_silent_without_mcp_or_plugins(caplog):
    """A lightweight setup (no MCP/plugins) must NOT emit the memory warning."""
    import logging

    from configuration import Configuration

    cfg = Configuration(**{"#anthropic_key": "KEY_NAME_ONLY", "task": {"prompt": "hi"}})
    with caplog.at_level(logging.WARNING):
        Component._warn_if_memory_intensive(cfg)
    assert not any("runtime.backend.type" in r.message for r in caplog.records)


def test_build_env_sets_writable_caches_for_read_only_image():
    """uvx/npx MCP launchers default their cache + HOME to the read-only image
    root and die before the server starts (Finding 5). _build_env must redirect
    HOME and the uv/npm/xdg caches to the writable /tmp."""
    import component as component_module
    from configuration import Configuration

    cfg = Configuration(**{"#anthropic_key": "KEY_NAME_ONLY"})
    env = Component._build_env(cfg)
    assert env["HOME"] == component_module.AGENT_HOME
    assert env["UV_CACHE_DIR"] == component_module.UV_CACHE_DIR
    assert env["NPM_CONFIG_CACHE"] == component_module.NPM_CONFIG_CACHE
    assert env["XDG_CACHE_HOME"] == component_module.XDG_CACHE_HOME
    # every redirected path is under the writable /tmp
    for key in ("HOME", "UV_CACHE_DIR", "NPM_CONFIG_CACHE", "XDG_CACHE_HOME"):
        assert env[key].startswith("/tmp/")
    # secrets still threaded
    assert env["ANTHROPIC_API_KEY"] == "KEY_NAME_ONLY"


def test_run_task_launch_failure_is_user_exception(tmp_path, monkeypatch):
    """A CLI-not-found / connection failure surfaces as a clear exit-1 message."""
    import pytest
    from keboola.component.exceptions import UserException

    data_dir = _make_datadir(
        tmp_path,
        {"#anthropic_key": "KEY_NAME_ONLY", "task": {"prompt": "x"}},
    )
    monkeypatch.setenv("KBC_DATADIR", data_dir)
    comp = Component()

    from claude_agent_sdk import CLINotFoundError

    def raising_stream(prompt, options):
        async def gen():
            raise CLINotFoundError("claude binary not found")
            yield  # pragma: no cover

        return gen()

    monkeypatch.setattr(comp._runner, "_query", raising_stream)

    with pytest.raises(UserException) as exc:
        comp.run()
    assert "CLI/MCP failed to launch" in str(exc.value)
