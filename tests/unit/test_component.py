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
