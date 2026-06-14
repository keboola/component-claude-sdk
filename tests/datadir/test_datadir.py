"""Datadir functional tests — config parsing + output mapping/manifests.

End-to-end through the Keboola ``/data`` contract with the Claude SDK boundary
(``ClaudeRunner._query``) mocked to a canned typed message stream (spec §7).

Covered (per the Phase 5 plan):
- Success: config-prompt mode; tasks-table mode; agent→table promotion from the
  agent output dir; the always-on transcript tables (``claude_sessions`` /
  ``claude_runs``) + JSONL file artifact.
- Manifest correctness: authoritative ``schema``, ``has_header``,
  ``primary_key`` / ``incremental``, ``write_always`` on the transcript tables.
- Failure (each → exit 1 via ``UserException``): ``task_id_filter`` no match;
  a prompting ``permission_mode``; agent-declared incremental-without-PK; a
  missing required ``tasks`` column; missing ``#anthropic_key``.
"""

from __future__ import annotations

import os

import pytest
from keboola.component.exceptions import UserException

from .conftest import canned_stream, install


def _pk_columns(manifest: dict) -> list[str]:
    """Primary-key column names from an authoritative-schema manifest."""
    return [col["name"] for col in manifest["schema"] if col.get("primary_key")]


# --------------------------------------------------------------------------- #
# Success cases
# --------------------------------------------------------------------------- #


def test_config_prompt_mode_writes_transcript_tables(datadir, monkeypatch):
    """Config-prompt mode (no input table): one run, transcript tables + JSONL."""
    datadir.config({"#anthropic_key": "KEY_NAME_ONLY", "task": {"prompt": "summarise"}})
    install(monkeypatch, canned_stream(blocks=("hello",)))

    datadir.build_component().run()

    runs = datadir.read_csv("claude_runs.csv")
    assert len(runs) == 1
    assert runs[0]["task_id"] == "config-task"
    assert runs[0]["success"] == "true"
    # 'pinned' resolves to the actual baked package version recorded for traceability
    assert runs[0]["sdk_version_resolved"] == "0.2.101"
    assert runs[0]["model"] == "claude-opus-4-8"

    sessions = datadir.read_csv("claude_sessions.csv")
    # init + one assistant text + result == 3 events, all with verbatim raw_json
    assert len(sessions) == 3
    assert all(row["raw_json"] for row in sessions)
    assert any(row["text"] == "hello" for row in sessions)

    # JSONL file artifact + its manifest are present (success path)
    files = os.listdir(datadir.out_files_dir)
    assert any(f.endswith(".jsonl") for f in files)
    assert any(f.endswith(".jsonl.manifest") for f in files)


def test_transcript_tables_are_write_always_with_pk(datadir, monkeypatch):
    """The transcript tables carry write_always + incremental + the spec PKs."""
    datadir.config({"#anthropic_key": "KEY_NAME_ONLY", "task": {"prompt": "x"}})
    install(monkeypatch, canned_stream())

    datadir.build_component().run()

    runs_manifest = datadir.read_manifest("claude_runs.csv")
    sessions_manifest = datadir.read_manifest("claude_sessions.csv")
    for manifest in (runs_manifest, sessions_manifest):
        assert manifest["write_always"] is True
        assert manifest["incremental"] is True
        assert "schema" in manifest  # authoritative native types
    # PKs live per-column in the authoritative schema (primary_key: true)
    assert _pk_columns(runs_manifest) == ["task_id", "session_id"]
    assert _pk_columns(sessions_manifest) == ["task_id", "session_id", "seq"]


def test_tasks_table_mode_one_run_per_row(datadir, monkeypatch):
    """Tasks-table mode: one agent run per CSV row, one claude_runs row each."""
    datadir.config({"#anthropic_key": "KEY_NAME_ONLY"})
    datadir.input_table(
        "tasks",
        ["task_id", "prompt"],
        [["a", "do A"], ["b", "do B"], ["c", "do C"]],
    )
    install(monkeypatch, canned_stream())

    datadir.build_component().run()

    runs = datadir.read_csv("claude_runs.csv")
    assert sorted(r["task_id"] for r in runs) == ["a", "b", "c"]
    assert all(r["success"] == "true" for r in runs)


def test_task_id_filter_selects_subset(datadir, monkeypatch):
    """task_id_filter narrows a shared tasks table to the owned row(s)."""
    datadir.config({"#anthropic_key": "KEY_NAME_ONLY", "task_id_filter": "b"})
    datadir.input_table("tasks", ["task_id", "prompt"], [["a", "A"], ["b", "B"], ["c", "C"]])
    install(monkeypatch, canned_stream())

    datadir.build_component().run()

    runs = datadir.read_csv("claude_runs.csv")
    assert [r["task_id"] for r in runs] == ["b"]


def test_agent_output_table_promoted_with_manifest(datadir, monkeypatch):
    """An agent-produced CSV in the scratch dir lands as a manifested output table."""
    datadir.config({"#anthropic_key": "KEY_NAME_ONLY", "task": {"prompt": "make a table"}})
    install(
        monkeypatch,
        canned_stream(
            write_outputs={"result.csv": (["id", "label"], [["1", "x"], ["2", "y"]])},
            agent_out_dir=datadir.agent_out,
        ),
    )

    datadir.build_component().run()

    rows = datadir.read_csv("result.csv")
    assert rows == [{"id": "1", "label": "x"}, {"id": "2", "label": "y"}]
    manifest = datadir.read_manifest("result.csv")
    assert manifest["has_header"] is True
    assert manifest["incremental"] is False  # default overwrite, the safe re-run default
    assert "schema" in manifest  # authoritative all-STRING schema for agent tables
    # destination must NOT be set — defaultBucket overrides it (spec §2.6)
    assert "destination" not in manifest or not manifest["destination"]


def test_agent_output_incremental_with_pk_from_meta(datadir, monkeypatch):
    """A .meta.json declaring incremental + primary_key is honoured in the manifest."""
    datadir.config({"#anthropic_key": "KEY_NAME_ONLY", "task": {"prompt": "make a table"}})
    install(
        monkeypatch,
        canned_stream(
            write_outputs={
                "orders.csv": (["id", "amount"], [["1", "10"]], {"incremental": True, "primary_key": ["id"]})
            },
            agent_out_dir=datadir.agent_out,
        ),
    )

    datadir.build_component().run()

    manifest = datadir.read_manifest("orders.csv")
    assert manifest["incremental"] is True
    assert _pk_columns(manifest) == ["id"]


# --------------------------------------------------------------------------- #
# Failure cases — every one is exit 1 (UserException), transcript still durable
# --------------------------------------------------------------------------- #


def test_task_id_filter_no_match_fails(datadir, monkeypatch):
    """A task_id_filter matching no row → exit 1 naming the available ids."""
    datadir.config({"#anthropic_key": "KEY_NAME_ONLY", "task_id_filter": "nope"})
    datadir.input_table("tasks", ["task_id", "prompt"], [["a", "A"], ["b", "B"]])
    install(monkeypatch, canned_stream())

    with pytest.raises(UserException) as exc:
        datadir.build_component().run()
    assert "nope" in str(exc.value)
    assert "a" in str(exc.value) and "b" in str(exc.value)


def test_prompting_permission_mode_rejected(datadir, monkeypatch):
    """A prompting permission_mode (would hang headless) → exit 1 at config parse."""
    datadir.config(
        {"#anthropic_key": "KEY_NAME_ONLY", "permission_mode": "acceptEdits", "task": {"prompt": "x"}}
    )
    install(monkeypatch, canned_stream())

    with pytest.raises(UserException) as exc:
        datadir.build_component().run()
    assert "permission_mode" in str(exc.value)


def test_incremental_without_pk_fails(datadir, monkeypatch):
    """Agent output marked incremental but with no primary_key → exit 1."""
    datadir.config({"#anthropic_key": "KEY_NAME_ONLY", "task": {"prompt": "x"}})
    install(
        monkeypatch,
        canned_stream(
            write_outputs={"bad.csv": (["id"], [["1"]], {"incremental": True})},
            agent_out_dir=datadir.agent_out,
        ),
    )

    with pytest.raises(UserException) as exc:
        datadir.build_component().run()
    assert "primary_key" in str(exc.value)
    # transcript tables are write_always — still flushed despite the promote failure
    assert (datadir.out_tables_dir / "claude_runs.csv").exists()
    assert (datadir.out_tables_dir / "claude_sessions.csv").exists()


def test_tasks_table_missing_required_column_fails(datadir, monkeypatch):
    """A tasks table missing the required 'prompt' column → exit 1 naming it."""
    datadir.config({"#anthropic_key": "KEY_NAME_ONLY"})
    datadir.input_table("tasks", ["task_id"], [["a"], ["b"]])
    install(monkeypatch, canned_stream())

    with pytest.raises(UserException) as exc:
        datadir.build_component().run()
    assert "prompt" in str(exc.value)


def test_missing_anthropic_key_fails(datadir, monkeypatch):
    """Missing #anthropic_key → exit 1 at config validation."""
    datadir.config({"task": {"prompt": "x"}})
    install(monkeypatch, canned_stream())

    with pytest.raises(UserException) as exc:
        datadir.build_component().run()
    assert "anthropic_key" in str(exc.value)
