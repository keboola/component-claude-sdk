"""Unit tests for TranscriptWriter using the REAL keboola.component library.

The writer goes through a real ``ComponentBase`` so manifests are produced by
the genuine ``keboola.component.dao`` machinery — no hand-rolled fake that could
accept attributes the library silently drops (e.g. ``write_always`` on a
``FileDefinition``).
"""

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
from component import Component
from transcript_writer import RUNS_TABLE, SESSIONS_TABLE, TranscriptWriter


def _component(tmp_path, monkeypatch):
    """A real Component (ComponentBase subclass) rooted at a temp KBC data dir.

    Using the genuine library machinery so manifests are produced exactly as in
    production (no fake that could accept attributes the library drops).
    """
    data_dir = tmp_path / "data"
    (data_dir / "out" / "tables").mkdir(parents=True)
    (data_dir / "out" / "files").mkdir(parents=True)
    (data_dir / "config.json").write_text(json.dumps({"parameters": {}}), encoding="utf-8")
    monkeypatch.setenv("KBC_DATADIR", str(data_dir))
    return Component(), str(data_dir)


def _read_manifest(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _run_writer(component, files_path, messages, result, **kwargs):
    writer = TranscriptWriter(
        component=component,
        files_out_path=files_path,
        sdk_version_resolved="0.2.101",
        plugins_resolved={"superpowers/sp": "latest"},
        **kwargs,
    )
    writer.begin_task(result.task_id)
    for m in messages:
        writer.on_message(m)
    writer.end_task(result)
    writer.flush()


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


def test_jsonl_file_written_with_one_line_per_message(tmp_path, monkeypatch):
    comp, data_dir = _component(tmp_path, monkeypatch)
    files_path = os.path.join(data_dir, "out", "files")
    _run_writer(comp, files_path, _messages(), _result())
    jsonl = os.path.join(files_path, "claude_session_t1.jsonl")
    assert os.path.isfile(jsonl)
    lines = [json.loads(line) for line in open(jsonl, encoding="utf-8") if line.strip()]
    assert len(lines) == 3


def test_sessions_table_rows_match_stream(tmp_path, monkeypatch):
    comp, data_dir = _component(tmp_path, monkeypatch)
    files_path = os.path.join(data_dir, "out", "files")
    _run_writer(comp, files_path, _messages(), _result())
    path = os.path.join(data_dir, "out", "tables", f"{SESSIONS_TABLE}.csv")
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    assert len(rows) == 3
    tool_rows = [r for r in rows if r["tool_name"] == "Bash"]
    assert tool_rows and json.loads(tool_rows[0]["tool_input_json"]) == {"command": "ls"}
    assert all(r["task_id"] == "t1" for r in rows)


def test_sessions_table_preserves_raw_jsonl_verbatim(tmp_path, monkeypatch):
    """The TABLE sink is the durable transcript-of-record: every JSONL line is
    preserved verbatim in raw_json (so it survives even when the file is not)."""
    comp, data_dir = _component(tmp_path, monkeypatch)
    files_path = os.path.join(data_dir, "out", "files")
    _run_writer(comp, files_path, _messages(), _result())

    jsonl = os.path.join(files_path, "claude_session_t1.jsonl")
    file_lines = [line.strip() for line in open(jsonl, encoding="utf-8") if line.strip()]

    sessions = os.path.join(data_dir, "out", "tables", f"{SESSIONS_TABLE}.csv")
    table_raw = [r["raw_json"] for r in csv.DictReader(open(sessions, encoding="utf-8"))]

    # one raw_json cell per JSONL line, byte-for-byte the same content
    assert table_raw == file_lines


def test_runs_table_summary_row(tmp_path, monkeypatch):
    comp, data_dir = _component(tmp_path, monkeypatch)
    files_path = os.path.join(data_dir, "out", "files")
    _run_writer(comp, files_path, _messages(), _result())
    path = os.path.join(data_dir, "out", "tables", f"{RUNS_TABLE}.csv")
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    assert len(rows) == 1
    assert rows[0]["success"] == "true"
    assert rows[0]["sdk_version_resolved"] == "0.2.101"
    assert json.loads(rows[0]["plugins_resolved"]) == {"superpowers/sp": "latest"}
    assert float(rows[0]["total_cost_usd"]) == 0.02


def test_table_manifests_are_write_always_file_manifest_is_not(tmp_path, monkeypatch):
    """Table manifests carry a REAL write_always; the file manifest does NOT
    (Keboola file output mapping has no such attribute — library-verified)."""
    comp, data_dir = _component(tmp_path, monkeypatch)
    tables_path = os.path.join(data_dir, "out", "tables")
    files_path = os.path.join(data_dir, "out", "files")
    _run_writer(comp, files_path, _messages(), _result())

    for table in (SESSIONS_TABLE, RUNS_TABLE):
        manifest = _read_manifest(os.path.join(tables_path, f"{table}.csv.manifest"))
        assert manifest.get("write_always") is True, f"{table} manifest must be write_always"
        assert manifest.get("incremental") is True
        # authoritative schema present with PK columns
        assert "schema" in manifest

    file_manifest = _read_manifest(os.path.join(files_path, "claude_session_t1.jsonl.manifest"))
    assert "write_always" not in file_manifest  # library drops it on FileDefinition
    assert "session-transcript" in file_manifest.get("tags", [])


def test_runs_optional_numeric_columns_are_nullable_when_none(tmp_path, monkeypatch):
    """When total_cost_usd / api_error_status are None the cell is empty AND the
    authoritative schema marks the column nullable, so the load gets NULL (not a
    type error). PK columns stay non-nullable."""
    comp, data_dir = _component(tmp_path, monkeypatch)
    tables_path = os.path.join(data_dir, "out", "tables")
    files_path = os.path.join(data_dir, "out", "files")
    msgs = [SystemMessage(subtype="init", data={"session_id": "s"})]
    # total_cost_usd and api_error_status default to None on this result
    result = ClaudeRunResult(task_id="t1", success=True, session_id="s")
    _run_writer(comp, files_path, msgs, result)

    runs_csv = os.path.join(tables_path, f"{RUNS_TABLE}.csv")
    row = next(csv.DictReader(open(runs_csv, encoding="utf-8")))
    assert row["total_cost_usd"] == ""  # genuinely empty cell -> NULL
    assert row["api_error_status"] == ""

    manifest = _read_manifest(runs_csv + ".manifest")
    by_name = {c["name"]: c for c in manifest["schema"]}
    assert by_name["total_cost_usd"]["nullable"] is True
    assert by_name["api_error_status"]["nullable"] is True
    assert by_name["total_cost_usd"]["data_type"]["base"]["type"] == "FLOAT"
    # PK column is NOT nullable
    assert by_name["task_id"].get("nullable") in (None, False)


def test_secret_values_scrubbed_from_output(tmp_path, monkeypatch):
    comp, data_dir = _component(tmp_path, monkeypatch)
    files_path = os.path.join(data_dir, "out", "files")
    tables_path = os.path.join(data_dir, "out", "tables")
    msgs = [
        SystemMessage(subtype="init", data={"session_id": "sess-1"}),
        AssistantMessage(content=[TextBlock(text="key is SECRET_ABC")], model="m", session_id="sess-1"),
    ]
    result = ClaudeRunResult(task_id="t1", success=True, session_id="sess-1", result_text="SECRET_ABC")
    _run_writer(comp, files_path, msgs, result, secret_values=["SECRET_ABC"])

    jsonl = open(os.path.join(files_path, "claude_session_t1.jsonl"), encoding="utf-8").read()
    sessions = open(os.path.join(tables_path, f"{SESSIONS_TABLE}.csv"), encoding="utf-8").read()
    runs = open(os.path.join(tables_path, f"{RUNS_TABLE}.csv"), encoding="utf-8").read()
    assert "SECRET_ABC" not in jsonl
    assert "SECRET_ABC" not in sessions
    assert "SECRET_ABC" not in runs
    assert "***" in jsonl


def test_failure_still_writes_transcript_table(tmp_path, monkeypatch):
    """On failure the TABLE sink (write_always) is the durability guarantee."""
    comp, data_dir = _component(tmp_path, monkeypatch)
    tables_path = os.path.join(data_dir, "out", "tables")
    files_path = os.path.join(data_dir, "out", "files")
    msgs = [SystemMessage(subtype="init", data={"session_id": "s"})]
    result = ClaudeRunResult(task_id="t1", success=False, session_id="s", is_error=True, subtype="error")
    _run_writer(comp, files_path, msgs, result)

    runs_manifest = _read_manifest(os.path.join(tables_path, f"{RUNS_TABLE}.csv.manifest"))
    sessions_manifest = _read_manifest(os.path.join(tables_path, f"{SESSIONS_TABLE}.csv.manifest"))
    assert runs_manifest["write_always"] is True
    assert sessions_manifest["write_always"] is True
    runs = list(csv.DictReader(open(os.path.join(tables_path, f"{RUNS_TABLE}.csv"), encoding="utf-8")))
    assert runs[0]["success"] == "false"
