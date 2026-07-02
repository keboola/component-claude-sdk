"""Unit tests for the TaskSource (config-prompt and tasks-table modes)."""

from dataclasses import dataclass

import pytest
from keboola.component.exceptions import UserException

from configuration import Configuration
from tasks import TaskSource


@dataclass
class FakeTable:
    """Minimal stand-in for keboola TableDefinition (name + full_path)."""

    name: str
    full_path: str


def _config(**overrides):
    data = {"#anthropic_key": "KEY_NAME_ONLY"}
    data.update(overrides)
    return Configuration(**data)


def _write_csv(tmp_path, name, header, rows):
    path = tmp_path / name
    lines = [",".join(header)]
    lines += [",".join(r) for r in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return FakeTable(name=name.replace(".csv", ""), full_path=str(path))


def test_config_prompt_mode():
    cfg = _config(task={"prompt": "Summarise the data"})
    tasks = TaskSource(cfg).load([])
    assert len(tasks) == 1
    assert tasks[0].task_id == "config-task"
    assert tasks[0].prompt == "Summarise the data"


def test_config_prompt_mode_empty_prompt_raises():
    cfg = _config()
    with pytest.raises(UserException):
        TaskSource(cfg).load([])


def test_tasks_table_mode_one_task_per_row(tmp_path):
    table = _write_csv(
        tmp_path,
        "tasks.csv",
        ["task_id", "prompt"],
        [["t1", "do a"], ["t2", "do b"]],
    )
    tasks = TaskSource(_config()).load([table])
    assert [t.task_id for t in tasks] == ["t1", "t2"]
    assert tasks[0].prompt == "do a"


def test_tasks_table_missing_required_column_raises(tmp_path):
    table = _write_csv(tmp_path, "tasks.csv", ["task_id"], [["t1"]])
    with pytest.raises(UserException) as exc:
        TaskSource(_config()).load([table])
    assert "prompt" in str(exc.value)


def test_tasks_table_empty_prompt_raises(tmp_path):
    table = _write_csv(tmp_path, "tasks.csv", ["task_id", "prompt"], [["t1", ""]])
    with pytest.raises(UserException) as exc:
        TaskSource(_config()).load([table])
    assert "t1" in str(exc.value)


def test_tasks_table_duplicate_id_raises(tmp_path):
    table = _write_csv(tmp_path, "tasks.csv", ["task_id", "prompt"], [["t1", "a"], ["t1", "b"]])
    with pytest.raises(UserException) as exc:
        TaskSource(_config()).load([table])
    assert "Duplicate" in str(exc.value)


def test_unknown_columns_go_to_extra(tmp_path):
    table = _write_csv(
        tmp_path,
        "tasks.csv",
        ["task_id", "prompt", "region"],
        [["t1", "a", "EU"]],
    )
    tasks = TaskSource(_config()).load([table])
    assert tasks[0].extra == {"region": "EU"}


def test_per_task_overrides_parsed(tmp_path):
    table = _write_csv(
        tmp_path,
        "tasks.csv",
        ["task_id", "prompt", "model", "max_turns", "max_budget_usd"],
        [["t1", "a", "claude-haiku-4-5", "5", "2.5"]],
    )
    task = TaskSource(_config()).load([table])[0]
    assert task.model == "claude-haiku-4-5"
    assert task.max_turns == 5
    assert task.max_budget_usd == 2.5


def test_invalid_int_override_raises(tmp_path):
    table = _write_csv(tmp_path, "tasks.csv", ["task_id", "prompt", "max_turns"], [["t1", "a", "notanint"]])
    with pytest.raises(UserException):
        TaskSource(_config()).load([table])


def test_task_id_filter_selects_subset(tmp_path):
    table = _write_csv(
        tmp_path,
        "tasks.csv",
        ["task_id", "prompt"],
        [["t1", "a"], ["t2", "b"], ["t3", "c"]],
    )
    cfg = _config(task_id_filter=["t1", "t3"])
    tasks = TaskSource(cfg).load([table])
    assert [t.task_id for t in tasks] == ["t1", "t3"]


def test_task_id_filter_no_match_raises_with_available(tmp_path):
    table = _write_csv(tmp_path, "tasks.csv", ["task_id", "prompt"], [["t1", "a"]])
    cfg = _config(task_id_filter="nope")
    with pytest.raises(UserException) as exc:
        TaskSource(cfg).load([table])
    assert "nope" in str(exc.value)
    assert "t1" in str(exc.value)


def test_sole_table_accepted_by_convention(tmp_path):
    table = _write_csv(tmp_path, "my_tasks.csv", ["task_id", "prompt"], [["t1", "a"]])
    tasks = TaskSource(_config()).load([table])
    assert tasks[0].task_id == "t1"


def test_multiple_tables_none_named_tasks_raises(tmp_path):
    t1 = _write_csv(tmp_path, "a.csv", ["task_id", "prompt"], [["x", "y"]])
    t2 = _write_csv(tmp_path, "b.csv", ["task_id", "prompt"], [["x", "y"]])
    with pytest.raises(UserException):
        TaskSource(_config()).load([t1, t2])
