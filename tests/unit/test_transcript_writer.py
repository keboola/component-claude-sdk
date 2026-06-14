"""Unit tests for TranscriptWriter using a real ComponentBase data folder."""

import csv
import json
import os

from claude_agent_sdk import (
    AssistantMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

from claude_runner import ClaudeRunResult
from transcript_writer import RUNS_TABLE, SESSIONS_TABLE, TranscriptWriter


class FakeComponent:
    """Minimal component exposing the manifest/path machinery the writer uses."""

    def __init__(self, root):
        self.root = root
        self.tables_path = os.path.join(root, "out", "tables")
        self.files_path = os.path.join(root, "out", "files")
        os.makedirs(self.tables_path, exist_ok=True)
        os.makedirs(self.files_path, exist_ok=True)
        self.manifests = []

    def create_out_table_definition(self, name, schema=None, primary_key=None, incremental=None,
                                    write_always=False, has_header=None, **kwargs):
        return _TableDef(os.path.join(self.tables_path, name), name, schema, primary_key,
                         incremental, write_always, has_header)

    def create_out_file_definition(self, name, tags=None, **kwargs):
        return _FileDef(os.path.join(self.files_path, name), name, tags or [])

    def write_manifest(self, definition):
        self.manifests.append(definition)


class _TableDef:
    def __init__(self, full_path, name, schema, pk, incremental, write_always, has_header):
        self.full_path = full_path
        self.name = name
        self.schema = schema
        self.primary_key = pk
        self.incremental = incremental
        self.write_always = write_always
        self.has_header = has_header


class _FileDef:
    def __init__(self, full_path, name, tags):
        self.full_path = full_path
        self.name = name
        self.tags = tags
        self.write_always = False


def _run_writer(tmp_path, messages, result, **kwargs):
    comp = FakeComponent(str(tmp_path))
    writer = TranscriptWriter(
        component=comp,
        files_out_path=comp.files_path,
        sdk_version_resolved="0.2.101",
        plugins_resolved={"superpowers/sp": "latest"},
        **kwargs,
    )
    writer.begin_task(result.task_id)
    for m in messages:
        writer.on_message(m)
    writer.end_task(result)
    writer.flush()
    return comp, writer


def _messages():
    return [
        SystemMessage(subtype="init", data={"session_id": "sess-1"}),
        AssistantMessage(
            content=[
                TextBlock(text="thinking"),
                ToolUseBlock(id="tu1", name="Bash", input={"command": "ls"}),
            ],
            model="claude-opus-4-8",
            session_id="sess-1",
        ),
        AssistantMessage(
            content=[ToolResultBlock(tool_use_id="tu1", content="file.txt", is_error=False)],
            model="claude-opus-4-8",
            session_id="sess-1",
        ),
    ]


def _result():
    return ClaudeRunResult(
        task_id="t1", success=True, session_id="sess-1", subtype="success",
        is_error=False, result_text="all done", total_cost_usd=0.02, duration_ms=1500, num_turns=3,
    )


def test_jsonl_file_written_with_one_line_per_message(tmp_path):
    comp, _ = _run_writer(tmp_path, _messages(), _result())
    jsonl = os.path.join(comp.files_path, "claude_session_t1.jsonl")
    assert os.path.isfile(jsonl)
    lines = [json.loads(line) for line in open(jsonl, encoding="utf-8") if line.strip()]
    assert len(lines) == 3


def test_sessions_table_rows_match_stream(tmp_path):
    comp, _ = _run_writer(tmp_path, _messages(), _result())
    path = os.path.join(comp.tables_path, f"{SESSIONS_TABLE}.csv")
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    assert len(rows) == 3
    # the ToolUseBlock row carries the tool name + input
    tool_rows = [r for r in rows if r["tool_name"] == "Bash"]
    assert tool_rows and json.loads(tool_rows[0]["tool_input_json"]) == {"command": "ls"}
    assert all(r["task_id"] == "t1" for r in rows)


def test_runs_table_summary_row(tmp_path):
    comp, _ = _run_writer(tmp_path, _messages(), _result())
    path = os.path.join(comp.tables_path, f"{RUNS_TABLE}.csv")
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    assert len(rows) == 1
    assert rows[0]["success"] == "true"
    assert rows[0]["sdk_version_resolved"] == "0.2.101"
    assert json.loads(rows[0]["plugins_resolved"]) == {"superpowers/sp": "latest"}
    assert float(rows[0]["total_cost_usd"]) == 0.02


def test_manifests_have_write_always_and_has_header(tmp_path):
    comp, _ = _run_writer(tmp_path, _messages(), _result())
    table_manifests = [m for m in comp.manifests if isinstance(m, _TableDef)]
    assert table_manifests, "expected table manifests"
    for m in table_manifests:
        assert m.write_always is True
        assert m.has_header is True
        assert m.incremental is True
        assert m.primary_key  # non-empty PK
    file_manifests = [m for m in comp.manifests if isinstance(m, _FileDef)]
    assert file_manifests and all(m.write_always for m in file_manifests)
    assert "session-transcript" in file_manifests[0].tags


def test_secret_values_scrubbed_from_output(tmp_path):
    msgs = [
        SystemMessage(subtype="init", data={"session_id": "sess-1"}),
        AssistantMessage(content=[TextBlock(text="key is SECRET_ABC")], model="m", session_id="sess-1"),
    ]
    result = ClaudeRunResult(task_id="t1", success=True, session_id="sess-1", result_text="SECRET_ABC")
    comp, _ = _run_writer(tmp_path, msgs, result, secret_values=["SECRET_ABC"])
    jsonl = open(os.path.join(comp.files_path, "claude_session_t1.jsonl"), encoding="utf-8").read()
    sessions = open(os.path.join(comp.tables_path, f"{SESSIONS_TABLE}.csv"), encoding="utf-8").read()
    runs = open(os.path.join(comp.tables_path, f"{RUNS_TABLE}.csv"), encoding="utf-8").read()
    assert "SECRET_ABC" not in jsonl
    assert "SECRET_ABC" not in sessions
    assert "SECRET_ABC" not in runs
    assert "***" in jsonl


def test_failure_still_writes_transcript(tmp_path):
    msgs = [SystemMessage(subtype="init", data={"session_id": "s"})]
    result = ClaudeRunResult(task_id="t1", success=False, session_id="s", is_error=True, subtype="error")
    comp, _ = _run_writer(tmp_path, msgs, result)
    # write_always sinks present regardless of failure
    assert os.path.isfile(os.path.join(comp.tables_path, f"{RUNS_TABLE}.csv"))
    runs = list(csv.DictReader(open(os.path.join(comp.tables_path, f"{RUNS_TABLE}.csv"), encoding="utf-8")))
    assert runs[0]["success"] == "false"
